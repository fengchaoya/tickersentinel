"""yfinance 行情（Yahoo Finance 非官方接口）。

优点：零配置、覆盖美股/韩股/日股/汇率。缺点：非官方、可能限流、1 分钟数据只能取最近约 30 天且单次请求跨度 ≤ 8 天。
定位：实时分钟线与回放的主源，海外代码（.KS/.T/汇率）的唯一数据源；Alpaca 只做兜底和日线官方收盘价（prices_routing.py，决策 18）。
"""

from __future__ import annotations

import asyncio
import gc
import logging
from datetime import datetime, timedelta

import pandas as pd

from tickersentinel.config import is_us_symbol
from tickersentinel.models import Quote
from tickersentinel.sources.base import NoDataError, PriceSource, SourceError
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


def release_download_threads() -> None:
    """收掉 yfinance 每次下载（threads=True，单个代码也一样）留下的两样东西（2026-10-31 实测）：

    1. 时区缓存库的连接：每个下载线程各开一个 tkr-tz.db 连接（peewee 按线程建连接），线程结束后连接落在循环引用里，
       只有完整的垃圾回收才关。常驻进程里完整回收很少触发，一次下载漏 2 个句柄 × 线程数：分钟线采集每 2 分钟批量下载
       约 26 个代码，四十多分钟就把 macOS 默认的 256 个句柄用完，网页再也接不进新连接（Too many open files）。
    2. 线程对象：yfinance 用 multitasking 开线程，multitasking 把建过的每个线程都记在 config["TASKS"] 里、从不删除，
       每个约 8 KB 内存、16 个要垃圾回收追踪的对象——一个交易日两三万个线程，每天多占一两百 MB，完整回收也越来越慢。
       只留还在跑的（别的下载正在用的线程不动；yfinance 自己等结果不靠这个列表）。

    完整回收一次 5–40 毫秒，每次下载后做一次。
    """
    try:
        import multitasking

        tasks = multitasking.config.get("TASKS")
        if isinstance(tasks, list) and tasks:
            tasks[:] = [t for t in tasks if t.is_alive()]
    except Exception:  # pragma: no cover - multitasking 改了内部结构：只少了这一项清理
        log.debug("清理 multitasking 的线程列表失败", exc_info=True)
    gc.collect()


def raise_yfinance_errors() -> None:
    """让 yfinance 把请求出错（超时、断网、DNS、Yahoo 5xx）抛出来，而不是吞掉后报成“没有价格数据”。

    yfinance 1.7.0 默认 yf.config.debug.hide_exceptions = True：history 的请求出了任何异常（除了 429 限流）都被吞掉，
    data 为空，最后记成和休市一模一样的 “possibly delisted; no price data found”——断网、睡眠刚醒 DNS 没好时，采集会当成休市、
    把“查到哪一刻”推到现在，那段数据就永久缺了，也不报失败（2026-10-31 复审实测）。设成 False 后，yf.download 照样不抛：
    每个代码的异常记进 _DownloadCtx.errors（repr），休市是 YFPricesMissingError、代码不存在是 YFTzMissingError，
    网络错是 curl 的 Timeout / ConnectionError，is_no_data 据此分开。

    这是进程级设置：项目里别处用 yfinance 的地方（财报日程、联动候选）都已把异常转成 SourceError，不受影响。
    """
    try:
        import yfinance as yf

        yf.config.debug.hide_exceptions = False
    except Exception:  # pragma: no cover - 旧版本没有这个设置：只是分不出网络错
        log.debug("设置 yfinance 抛出请求错误失败", exc_info=True)


# yfinance 记在每个代码上的“没数据”：休市、这段时间没成交、代码不存在或已退市、超出 1 分钟线的 30 天范围。这些不是数据源出错。
# 按消息文字认，不按异常类名：Yahoo 给了原因时 YFPricesMissingError 的消息就是那个原因——404 的
# “No data found, symbol may be delisted” 是没数据，但服务器出错时同样可能包成 YFPricesMissingError('$MU: Internal Server Error')。
# 不认识的一律算取数失败：误判成失败只是下一轮多取一次；误判成没数据，那段就永久缺了。
_NO_DATA_MARKERS = ("no price data found", "no timezone found", "no data found", "may be delisted", "data doesn't exist",
                    "not available for starttime", "must be within the last")


def is_no_data(error: str) -> bool:
    """yfinance 给某个代码记的错误是不是“本来就没数据”（否则是限流、网络超时、Yahoo 宕机之类的取数失败）。"""
    e = error.lower()
    return any(m in e for m in _NO_DATA_MARKERS)


class Bars(dict):
    """intraday_bars 的结果：{代码: 分钟线}，另带 failed = {代码: 错误}——取数失败（限流、网络）而不是没数据的代码。
    分钟线采集据此不把这些代码的“查到哪一刻”往前推（恢复后从断掉的地方补），并计入失败（jobs/collect.py）。"""

    failed: dict[str, str]

    def __init__(self, *args, failed: dict[str, str] | None = None, **kw):
        super().__init__(*args, **kw)
        self.failed = dict(failed or {})


class YFinancePriceSource(PriceSource):
    name = "yfinance"
    MAX_SPAN = timedelta(days=7)

    def _download(self, symbols: list[str], **kw) -> tuple[pd.DataFrame | None, dict[str, str]]:
        """下载一批代码。返回（数据，{代码: 取数失败的原因}）——只列限流、网络这类失败，休市没数据的不算。

        yf.download 从不因为某个代码失败而抛异常：失败只记在它内部的 _DownloadCtx.errors 里、打一行日志。
        所以直接调 yfinance.multi._download_impl 并传入自己的 ctx，读出每个代码的错误；这两个是私有接口，
        yfinance 改了内部结构时退回 yf.download（那样就分不出失败的代码，和以前一样都当没数据）。
        """
        import yfinance as yf

        raise_yfinance_errors()
        opts = dict(group_by="ticker", auto_adjust=False, progress=False, threads=True, multi_level_index=True, **kw)
        try:
            from yfinance import multi as _multi
        except ImportError:  # pragma: no cover
            _multi = None
        impl, ctx_cls = getattr(_multi, "_download_impl", None), getattr(_multi, "_DownloadCtx", None)
        ctx = ctx_cls() if impl is not None and ctx_cls is not None else None
        try:
            data = impl(ctx, symbols, **opts) if ctx is not None else yf.download(tickers=symbols, **opts)
        except Exception as exc:  # yfinance 内部异常类型不稳定
            raise SourceError(f"[yfinance] 下载失败：{type(exc).__name__}: {exc}") from exc
        finally:
            release_download_threads()
        errors = dict(getattr(ctx, "errors", None) or {}) if ctx is not None else {}
        by_upper = {s.upper(): s for s in symbols}  # yfinance 内部把代码转成大写
        failed = {by_upper.get(k, k): str(v) for k, v in errors.items() if not is_no_data(str(v))}
        return data, failed

    async def intraday_bars(self, symbols, start, end=None, *, prepost: bool = False):
        return await asyncio.to_thread(self._intraday_sync, list(symbols), start, end, prepost)

    def _intraday_sync(self, symbols, start: datetime, end: datetime | None, prepost: bool):
        end = end or (utcnow() + timedelta(minutes=1))
        parts: dict[str, list[pd.DataFrame]] = {s: [] for s in symbols}
        failed: dict[str, str] = {}
        live = list(symbols)
        chunk_start = start
        while chunk_start < end and live:
            chunk_end = min(chunk_start + self.MAX_SPAN, end)
            raw, bad = self._download(live, start=chunk_start, end=chunk_end, interval="1m", prepost=prepost)
            failed.update(bad)
            for sym, sub in split_by_symbol(raw, live).items():
                if sym not in bad:
                    parts[sym].append(sub)
            # 从旧到新一段段取：某个代码这一段失败了，就不再取它更新的段——返回的数据停在断掉的地方，
            # 分钟线采集下一轮从库里最后一根接着取，不会在中间留下一个永远补不上的空洞
            live = [s for s in live if s not in bad]
            chunk_start = chunk_end
        out = Bars(failed=failed)
        for sym, frames in parts.items():
            if frames:
                df = _to_utc_index(pd.concat(frames))
                out[sym] = df[~df.index.duplicated(keep="last")]
        if not out:
            if failed:  # 不是休市：限流、网络出错（监控那边会换兜底的数据源，采集计入失败）
                first = next(iter(failed.values()))
                raise SourceError(f"[yfinance] 分钟线取数失败（{len(failed)}/{len(symbols)} 个代码，如 {next(iter(failed))}）："
                                  f"{first[:200]}")
            raise NoDataError(f"[yfinance] 分钟线没有返回任何数据（{', '.join(symbols)}）：休市或时间范围超出最近 30 天")
        return out

    async def daily_bars(self, symbols, lookback_days):
        return await asyncio.to_thread(self._daily_sync, list(symbols), lookback_days)

    def _daily_sync(self, symbols, lookback_days):
        start = (utcnow() - timedelta(days=int(lookback_days * 1.6) + 10)).date()
        raw, _ = self._download(symbols, start=start.isoformat(), interval="1d")
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
