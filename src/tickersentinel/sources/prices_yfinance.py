"""yfinance 行情（Yahoo Finance 非官方接口）。

优点：零配置、覆盖美股/韩股/日股/汇率。缺点：非官方、可能限流、1 分钟数据只能取最近约 30 天且单次请求跨度 ≤ 8 天。
定位：实时分钟线与回放的主源，海外代码（.KS/.T/汇率）的唯一数据源；Alpaca 只做兜底和日线官方收盘价（prices_routing.py，决策 18）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import pandas as pd

from tickersentinel.config import is_us_symbol
from tickersentinel.models import Quote
from tickersentinel.sources.base import PriceSource, SourceError
from tickersentinel.timeutil import ET, UTC, utcnow

log = logging.getLogger(__name__)

_COLS = {"Open": "open", "High": "high", "Low": "low", "Close": "close", "Adj Close": "adj_close", "Volume": "volume"}


def split_by_symbol(df: pd.DataFrame | None, symbols: list[str]) -> dict[str, pd.DataFrame]:
    """把 yf.download 的结果拆成 {symbol: DataFrame}，兼容 (Ticker, Price) 与 (Price, Ticker) 两种列层级。"""
    if df is None or df.empty:
        return {}
    out: dict[str, pd.DataFrame] = {}
    if isinstance(df.columns, pd.MultiIndex):
        level = 0 if any(s in set(df.columns.get_level_values(0)) for s in symbols) else 1
        present = set(df.columns.get_level_values(level))
        for s in symbols:
            if s in present:
                out[s] = df.xs(s, axis=1, level=level)
    elif len(symbols) == 1:
        out[symbols[0]] = df
    result = {}
    for s, sub in out.items():
        sub = sub.rename(columns=_COLS)
        keep = [c for c in ("open", "high", "low", "close", "adj_close", "volume") if c in sub.columns]
        sub = sub[keep].dropna(subset=["close"])
        if not sub.empty:
            result[s] = sub
    return result


def _to_utc_index(df: pd.DataFrame) -> pd.DataFrame:
    idx = pd.DatetimeIndex(df.index)
    idx = idx.tz_localize(ET) if idx.tz is None else idx
    out = df.copy()
    out.index = idx.tz_convert(UTC)
    return out.sort_index()


def _to_date_index(df: pd.DataFrame) -> pd.DataFrame:
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    out = df.copy()
    out.index = idx.normalize()
    return out[~out.index.duplicated(keep="last")].sort_index()


class YFinancePriceSource(PriceSource):
    name = "yfinance"
    MAX_SPAN = timedelta(days=7)

    def _download(self, symbols: list[str], **kw) -> pd.DataFrame | None:
        import yfinance as yf

        try:
            return yf.download(
                tickers=symbols,
                group_by="ticker",
                auto_adjust=False,
                progress=False,
                threads=True,
                multi_level_index=True,
                **kw,
            )
        except Exception as exc:  # yfinance 内部异常类型不稳定
            raise SourceError(f"[yfinance] 下载失败：{type(exc).__name__}: {exc}") from exc

    async def intraday_bars(self, symbols, start, end=None, *, prepost: bool = False):
        return await asyncio.to_thread(self._intraday_sync, list(symbols), start, end, prepost)

    def _intraday_sync(self, symbols, start: datetime, end: datetime | None, prepost: bool):
        end = end or (utcnow() + timedelta(minutes=1))
        parts: dict[str, list[pd.DataFrame]] = {s: [] for s in symbols}
        chunk_start = start
        while chunk_start < end:
            chunk_end = min(chunk_start + self.MAX_SPAN, end)
            raw = self._download(symbols, start=chunk_start, end=chunk_end, interval="1m", prepost=prepost)
            for sym, sub in split_by_symbol(raw, symbols).items():
                parts[sym].append(sub)
            chunk_start = chunk_end
        out = {}
        for sym, frames in parts.items():
            if frames:
                df = _to_utc_index(pd.concat(frames))
                out[sym] = df[~df.index.duplicated(keep="last")]
        if not out:
            raise SourceError(f"[yfinance] 分钟线没有返回任何数据（{', '.join(symbols)}）：网络、限流或时间范围超出最近 30 天")
        return out

    async def daily_bars(self, symbols, lookback_days):
        return await asyncio.to_thread(self._daily_sync, list(symbols), lookback_days)

    def _daily_sync(self, symbols, lookback_days):
        start = (utcnow() - timedelta(days=int(lookback_days * 1.6) + 10)).date()
        raw = self._download(symbols, start=start.isoformat(), interval="1d")
        out = {s: _to_date_index(df) for s, df in split_by_symbol(raw, symbols).items()}
        if not out:
            raise SourceError(f"[yfinance] 日线没有返回任何数据（{', '.join(symbols)}）：网络或限流")
        return out

    async def latest_quotes(self, symbols):
        daily = await self.daily_bars(symbols, lookback_days=10)
        intraday: dict[str, pd.DataFrame] = {}
        us = [s for s in symbols if s in daily and is_us_symbol(s)]
        if us:
            try:
                intraday = await self.intraday_bars(us, start=utcnow() - timedelta(hours=20), prepost=True)
            except SourceError as exc:
                log.warning("取盘前/盘中最新价失败，改用日线：%s", exc)
        return build_quotes(daily, intraday, now=utcnow())


def build_quotes(daily: dict[str, pd.DataFrame], intraday: dict[str, pd.DataFrame], now: datetime) -> dict[str, Quote]:
    """由日线与（含盘前盘后的）分钟线拼出 Quote：price=最新成交价，prev_close=最新那根分钟线所在交易日之前的收盘。

    “之前”按最新分钟线的美东日期判断，不按现在的日期：否则过了美东午夜（23:00 CT）、周末和节假日上午，
    盘后价会改成和当天收盘比，同一个价格的涨跌幅口径变来变去（2026-10-21 审查发现）。now 保留给调用方，不参与计算。
    """
    out: dict[str, Quote] = {}
    for sym, df in daily.items():
        closes = df["close"].dropna()
        if closes.empty:
            continue
        bars = intraday.get(sym)
        if bars is not None and not bars.empty:
            # 美股：最新分钟价 vs 今日之前的最后一个收盘
            last_ts = bars.index[-1].to_pydatetime()
            bar_day = last_ts.astimezone(ET).date()
            before = closes[closes.index.date < bar_day]
            prev = float(before.iloc[-1]) if not before.empty else None
            out[sym] = Quote(sym, float(bars["close"].iloc[-1]), prev, ts=last_ts, session=_us_session(last_ts))
        elif len(closes) >= 2:
            # 海外/无分钟数据：最近收盘 vs 前一收盘
            out[sym] = Quote(sym, float(closes.iloc[-1]), float(closes.iloc[-2]), session="closed",
                             day=closes.index[-1].date())
    return out


def _us_session(ts: datetime) -> str:
    t = ts.astimezone(ET)
    minutes = t.hour * 60 + t.minute
    if minutes < 9 * 60 + 30:
        return "pre"
    if minutes < 16 * 60:
        return "regular"
    return "post"
