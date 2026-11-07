"""美股夜盘（美东 20:00–次日 04:00，周日晚到周五凌晨）的分钟线：Hyperliquid 上 trade.xyz 的股票永续合约（HIP-3，dex = xyz）。

为什么用它（2026-10-30 实测 10-13～10-30，MU / SNDK / SKHY）：
- 24 小时交易，工作日夜里每分钟都有成交（MU 每分钟约 30 笔、2.7 万美元）；
- 美股开着时和纳斯达克的价差中位 3–5 个基点，1 分钟收益相关 0.96；
- 夜里的涨跌和次日盘前开盘跳空的相关 0.99，差距中位 0.13–0.23 个点：工作日夜里它的参考价取自 Blue Ocean ATS
  （正股的夜盘成交），见 https://docs.trade.xyz/perpetuals/markets/stocks/us.md；
- 公开接口，不要 key、不要账号（只读行情；平台条款不允许美国居民在上面交易，我们也不交易）。
没选的：Pyth 也有 Blue Ocean 的夜盘价，但美股数据要付费账号（免费接口返回 401）；代币化股票（Ondo 的 MUon 等）流动性小得多；
半导体 ETF（xyz:SMH）夜里几乎没有成交，所以夜盘不做“板块 / 个股”拆分。
限制：1 分钟线只能取最近 5000 根（约 3.5 天），更早的用 5 分钟线（约 17 天）摊到每分钟——估计波动用的是 5 分钟收益，够用；
回放更早的夜盘时急涨急跌只能按 5 分钟的精度。周末（周五 20:00 到周日 20:05）合约自己定价、没有正股成交，这里只返回夜盘时段的数据。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

import pandas as pd

from tickersentinel.market_calendar import MarketCalendar, night_windows
from tickersentinel.models import Quote
from tickersentinel.sources.base import PriceSource, SourceError
from tickersentinel.sources.http import make_client, post_json
from tickersentinel.timeutil import UTC, utcnow

log = logging.getLogger(__name__)

URL = "https://api.hyperliquid.xyz/info"
MARKETS_TTL = 6 * 3600  # 合约列表缓存 6 小时（新上架的当天就能用上）
MAX_CANDLES = 5000  # 接口每种周期只保留最近 5000 根
MIN_GAP = 0.15  # 两次请求至少隔这么久（接口按 IP 限每分钟 1200 权重，一次 K 线请求约 20–100）


class HyperliquidPriceSource(PriceSource):
    """Hyperliquid 的公开行情（info 接口）。代码 → 合约：默认同名（MU → xyz:MU），aliases 可以改；没上架的代码不返回。"""

    name = "hyperliquid"

    def __init__(self, dex: str = "xyz", aliases: dict[str, str] | None = None, timeout: float = 15.0, client=None):
        self.dex = dex
        self.aliases = {k.upper(): v for k, v in (aliases or {}).items()}
        self._client = client or make_client(timeout=timeout)
        self._markets: set[str] = set()
        self._markets_at = 0.0
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def _post(self, payload: dict):
        async with self._lock:  # 串行、留间隔：几只股票每分钟一起取也不会撞限流
            wait = MIN_GAP - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await post_json(self._client, URL, payload, source=self.name)
            finally:
                self._last = time.monotonic()

    async def markets(self) -> set[str]:
        if not self._markets or time.monotonic() - self._markets_at > MARKETS_TTL:
            data = await self._post({"type": "meta", "dex": self.dex})
            if not isinstance(data, dict) or "universe" not in data:
                raise SourceError(f"[{self.name}] 合约列表格式不对：{str(data)[:200]}")
            self._markets = {u["name"] for u in data["universe"] if not u.get("isDelisted")}
            self._markets_at = time.monotonic()
        return self._markets

    async def coin(self, symbol: str) -> str | None:
        name = self.aliases.get(symbol.upper(), symbol.upper())
        coin = name if ":" in name else f"{self.dex}:{name}"
        return coin if coin in await self.markets() else None

    async def candles(self, coin: str, interval: str, start: datetime, end: datetime) -> list[dict]:
        out: list[dict] = []
        t, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        while t < end_ms:
            data = await self._post({"type": "candleSnapshot",
                                     "req": {"coin": coin, "interval": interval, "startTime": t, "endTime": end_ms}})
            if not isinstance(data, list):
                raise SourceError(f"[{self.name}] K 线格式不对（{coin}）：{str(data)[:200]}")
            if not data:
                break
            out += data
            nxt = int(data[-1]["t"]) + 60_000
            if nxt <= t or len(data) < 2:
                break
            t = nxt
        return out

    async def intraday_bars(self, symbols: list[str], start: datetime, end: datetime | None = None, *,
                            prepost: bool = False) -> dict[str, pd.DataFrame]:
        end = end or utcnow()
        out: dict[str, pd.DataFrame] = {}
        for sym in symbols:
            coin = await self.coin(sym)
            if coin is None:
                continue
            rows = to_frame(await self.candles(coin, "1m", start, end))
            first = rows.index[0] if len(rows) else end
            if first - start > timedelta(minutes=5):  # 更早的部分 1 分钟线已经取不到：用 5 分钟线补
                older = to_frame(await self.candles(coin, "5m", start, first), minutes=5)
                rows = pd.concat([older[older.index < first], rows]) if len(older) else rows
            if len(rows):
                out[sym] = rows[(rows.index >= start) & (rows.index < end)]
        return out

    async def daily_bars(self, symbols: list[str], lookback_days: int) -> dict[str, pd.DataFrame]:
        return {}  # 日线（β、昨收）用正股的

    async def aclose(self) -> None:
        await self._client.aclose()


def to_frame(candles: list[dict], minutes: int = 1) -> pd.DataFrame:
    """接口的 K 线 → DataFrame（index = 那一分钟的起点，UTC）。5 分钟线摊到每分钟：前 4 分钟记开盘价、最后一分钟记收盘价，
    成交量平均分（不会提前看到收盘价；5 分钟一取的收益和原来一样）。"""
    cols = ["open", "high", "low", "close", "volume"]
    if not candles:
        return pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], tz=UTC))
    recs = []
    for c in candles:
        t0 = pd.Timestamp(int(c["t"]), unit="ms", tz=UTC)
        o, h, lo, cl, v = (float(c[k]) for k in ("o", "h", "l", "c", "v"))
        if minutes == 1:
            recs.append((t0, o, h, lo, cl, v))
            continue
        for k in range(minutes):
            px = cl if k == minutes - 1 else o
            recs.append((t0 + pd.Timedelta(minutes=k), px, px, px, px, v / minutes))
    df = pd.DataFrame(recs, columns=["t", *cols]).set_index("t")
    df.index = pd.DatetimeIndex(df.index)
    return df[~df.index.duplicated(keep="last")].sort_index()


class OvernightPriceSource(PriceSource):
    """白天（含盘前盘后）照用原来的行情源；overnight_bars 取夜盘时段的合约行情（只保留夜盘时段，周末不给）。"""

    def __init__(self, inner: PriceSource, night: PriceSource, calendar: MarketCalendar | None = None):
        self.inner = inner
        self.night = night
        self.calendar = calendar or MarketCalendar("XNYS")
        self.name = f"{inner.name}（夜盘 {night.name}）"

    async def intraday_bars(self, symbols, start, end=None, *, prepost=False):
        return await self.inner.intraday_bars(symbols, start, end, prepost=prepost)

    async def daily_bars(self, symbols, lookback_days):
        return await self.inner.daily_bars(symbols, lookback_days)

    async def latest_quotes(self, symbols) -> dict[str, Quote]:
        return await self.inner.latest_quotes(symbols)

    async def overnight_bars(self, symbols, start, end=None):
        end = end or utcnow()
        windows = night_windows(self.calendar, start, end)
        if not windows or not symbols:
            return {}
        lo, hi = max(start, windows[0][0]), min(end, windows[-1][1])
        bars = await self.night.intraday_bars(symbols, start=lo, end=hi)
        out = {}
        for sym, df in bars.items():
            keep = pd.Series(False, index=df.index)
            for a, b in windows:
                keep |= (df.index >= a) & (df.index < b)
            if keep.any():
                out[sym] = df[keep.to_numpy()]
        return out

    async def aclose(self) -> None:
        await self.inner.aclose()
        await self.night.aclose()


__all__ = ["HyperliquidPriceSource", "OvernightPriceSource", "to_frame"]
