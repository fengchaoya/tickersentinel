"""Alpaca 行情。免费账户：IEX 实时、历史调用 200 次/分钟；SIP 全市场数据仅限 15 分钟以前。
默认用法（见 sources/prices_routing.py）：yfinance 为主，Alpaca 兜底（实时用 IEX、历史用 SIP），日线收盘价优先用 SIP。

注意：IEX 只是一个交易所，成交量约占全市场的几个百分点，冷门分钟可能没有 K 线（检测模块会前向填充）。
成交量只用于“相对历史同时段”的比较，绝对值偏小不影响。

接口：GET https://data.alpaca.markets/v2/stocks/bars
      参数 symbols, timeframe(1Min/1Day), start, end, limit(≤10000), feed, adjustment, sort, page_token
      请求头 APCA-API-KEY-ID / APCA-API-SECRET-KEY
响应：{"bars": {"MU": [{"t": "...Z", "o":..,"h":..,"l":..,"c":..,"v":..,"n":..,"vw":..}, ...]}, "next_page_token": ...}
（2026-10-23 用免费账户联网核实。）
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

import httpx
import pandas as pd

from tickersentinel.config import is_us_symbol
from tickersentinel.models import Quote
from tickersentinel.sources.base import PriceSource, SourceError
from tickersentinel.sources.http import get_json, make_client
from tickersentinel.sources.prices_yfinance import build_quotes
from tickersentinel.timeutil import ET, UTC, utcnow

BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
# 免费账户的 SIP（全市场）数据只能取 15 分钟以前的，查得更近会报 “subscription does not permit querying recent SIP data”
SIP_DELAY = timedelta(minutes=16)


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def rows_to_frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "n": "trades", "vw": "vwap"})
    df.index = pd.to_datetime(df.pop("t"), utc=True)
    df.index.name = None
    return df.sort_index()


class AlpacaPriceSource(PriceSource):
    name = "alpaca"

    def __init__(self, key_id: str | None, secret: str | None, feed: str = "iex", timeout: float = 12.0,
                 client: httpx.AsyncClient | None = None):
        if not key_id or not secret:
            raise SourceError("使用 Alpaca 需要在 .env 中填写 ALPACA_API_KEY_ID 和 ALPACA_API_SECRET_KEY")
        self.feed = feed
        self._auth = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret}
        self._client = client or make_client(timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _bars(self, symbols: list[str], timeframe: str, start: datetime, end: datetime | None) -> dict[str, pd.DataFrame]:
        if self.feed == "sip":  # 只取 15 分钟以前的
            cutoff = utcnow() - SIP_DELAY
            end = cutoff if end is None else min(end, cutoff)
            if start >= end:
                return {}
        params: dict[str, str | int] = {
            "symbols": ",".join(symbols),
            "timeframe": timeframe,
            "start": _iso(start),
            "limit": 10000,
            "feed": self.feed,
            "adjustment": "raw",
            "sort": "asc",
        }
        if end is not None:
            params["end"] = _iso(end)
        rows: dict[str, list[dict]] = defaultdict(list)
        for _ in range(50):  # 分页上限，防止死循环
            data = await get_json(self._client, BARS_URL, params=params, headers=self._auth, source="alpaca")
            for sym, bars in (data.get("bars") or {}).items():
                rows[sym].extend(bars or [])
            token = data.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        return {s: rows_to_frame(r) for s, r in rows.items() if r}

    async def intraday_bars(self, symbols, start, end=None, *, prepost: bool = False):
        # 分钟线本身就含盘前盘后（有成交的分钟才有 K 线），prepost 不需要额外处理。2026-10-23 实测：
        # IEX 的盘前盘后只有美东约 08:00–17:00，盘中 MU 每天 387–390 根、SKHY 317–375、SNDK 314–371（满额 390）；
        # SIP 04:00–20:00 完整、价格与 yfinance 逐分钟一致，但成交量口径不同（yfinance 盘前成交量全为 0）。
        return await self._bars(list(symbols), "1Min", start, end)

    async def daily_bars(self, symbols, lookback_days):
        start = utcnow() - timedelta(days=int(lookback_days * 1.6) + 10)
        frames = await self._bars(list(symbols), "1Day", start, None)
        out = {}
        for sym, df in frames.items():
            # 日 K 的时间戳是美东 0 点（UTC 04:00/05:00），转回交易日日期
            df.index = df.index.tz_convert(ET).tz_localize(None).normalize()
            out[sym] = df
        return out

    async def latest_quotes(self, symbols):
        daily = await self.daily_bars(symbols, lookback_days=10)
        intraday = await self.intraday_bars(symbols, start=utcnow() - timedelta(hours=20))
        return build_quotes(daily, intraday, now=utcnow())


def leaves(src: PriceSource) -> list[PriceSource]:
    """组合数据源里实际持有连接的那些（去重），关闭时用。"""
    kids = getattr(src, "children", None)
    if not kids:
        return [src]
    out: list[PriceSource] = []
    for k in kids():
        for leaf in leaves(k):
            if all(leaf is not x for x in out):
                out.append(leaf)
    return out


class RoutedPriceSource(PriceSource):
    """按代码路由：美股走主数据源（yfinance 或 Alpaca），海外代码（.KS/.T/汇率）固定走 yfinance。"""

    def __init__(self, us: PriceSource, foreign: PriceSource):
        self.us = us
        self.foreign = foreign
        self.name = us.name if us is foreign else f"{us.name}+{foreign.name}"

    @staticmethod
    def _split(symbols: list[str]) -> tuple[list[str], list[str]]:
        us = [s for s in symbols if is_us_symbol(s)]
        return us, [s for s in symbols if not is_us_symbol(s)]

    async def _route(self, method: str, symbols: list[str], *args, **kw) -> dict:
        us, foreign = self._split(list(symbols))
        if self.us is self.foreign:
            return await getattr(self.us, method)(list(symbols), *args, **kw)
        out: dict = {}
        if us:
            out.update(await getattr(self.us, method)(us, *args, **kw))
        if foreign:
            out.update(await getattr(self.foreign, method)(foreign, *args, **kw))
        return out

    async def intraday_bars(self, symbols, start, end=None, *, prepost: bool = False):
        return await self._route("intraday_bars", symbols, start, end, prepost=prepost)

    async def daily_bars(self, symbols, lookback_days):
        return await self._route("daily_bars", symbols, lookback_days)

    async def latest_quotes(self, symbols) -> dict[str, Quote]:
        return await self._route("latest_quotes", symbols)

    def children(self) -> list[PriceSource]:
        return [self.us, self.foreign]

    async def aclose(self) -> None:
        for leaf in leaves(self):
            await leaf.aclose()
