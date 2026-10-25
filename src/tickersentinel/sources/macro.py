"""宏观变量：长端美债利率、原油、黄金白银、美股大盘、VIX、美元等，附在异动推送里，也可以用 /macro 随时查询。

数据来源（2026-10-21 实测）：
- 盘中走势：yfinance 的 1 分钟线（含盘前盘后）。利率用 Cboe 的收益率指数（^TNX = 10 年、^TYX = 30 年，数值就是收益率 %），
  原油 / 黄金 / 白银 / 股指期货用 CME 连续合约（CL=F、GC=F、SI=F、ES=F、NQ=F），现货指数用 ^GSPC、^IXIC。
  期货与利率在 Yahoo 上约延迟 10–15 分钟，指数、汇率、比特币接近实时。
- 官方收盘：美国财政部每日收益率曲线（home.treasury.gov 的 CSV，不需要 key），补 2 年期和 2s10s 利差，每天一个数。
  （FRED 的 fredgraph.csv 现在有反爬验证，不带 key 取不到，所以没用它。）

涨跌口径只用分钟线算，不用日线：日线和分钟线在期货换月时是不同合约（实测 CL=F 日线 101.79、分钟线约 98，
用日线算“较昨收”会凭空多出 -4%），外汇 / 比特币的日线日期也各有各的切法。所以：
- ref=close（利率、现货指数、VIX 等有固定交易时段的）：较上一个美股收盘（16:00 ET，提前收盘日自动处理）时的价格；
  美股休市时段换成期货（off_hours_symbol）显示，口径是“较美股上一收盘时的期货价”，即收盘以来期货走了多少。
- ref=24h（几乎全天交易的期货、外汇、比特币）：较 24 小时前。
另给“近 1 小时”变化（异动前后利率 / 油价有没有同时跳），市场没在交易时不给。
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import pandas as pd

from tickersentinel.sources.base import SourceError
from tickersentinel.timeutil import utcnow

if TYPE_CHECKING:
    from tickersentinel.config import MacroIndicator, Settings
    from tickersentinel.market_calendar import MarketCalendar
    from tickersentinel.sources.prices_yfinance import YFinancePriceSource

log = logging.getLogger(__name__)

TREASURY_URL = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
                "{year}/all?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv")
HISTORY = timedelta(days=5)  # 周一早上的“24 小时前”落在周日，要能往回找到周五
ONE_HOUR_SLACK = timedelta(minutes=10)  # 1 小时前那根 K 线最多可以再早 10 分钟（稀疏成交），更早说明当时没在交易


@dataclass
class MacroQuote:
    key: str
    name: str
    symbol: str
    kind: str  # yield：数值是收益率（%），变化用 bp；price / level：变化用 %
    group: str
    value: float
    ts: datetime  # 最新一根 K 线（UTC）
    change: float | None  # yield 为 bp，其他为 %
    change_label: str  # “较昨收” / “24h” / “较美股收盘”
    change_1h: float | None
    off_hours: bool = False  # True：美股休市，显示的是期货
    theme: str | None = None  # 板块指标所属的主题（config/themes/ 的 indicators）；基础宏观变量为 None

    def fmt_value(self) -> str:
        if self.kind == "yield":
            return f"{self.value:.2f}%"
        v = self.value
        num = f"{v:,.0f}" if v >= 1000 else f"{v:,.2f}"
        return f"${num}" if self.kind == "price" else num

    def fmt_change(self, x: float | None) -> str:
        if x is None:
            return "—"
        if self.kind == "yield":
            return f"{x:+.0f}bp" if abs(x) >= 0.5 else "±0bp"
        return f"{x:+.1f}%" if abs(x) >= 0.05 else "±0.0%"


@dataclass
class TreasuryCurve:
    day: date
    yields: dict[str, float]  # "2Y" / "10Y" / "30Y" … → %

    @property
    def spread_2s10s_bp(self) -> float | None:
        a, b = self.yields.get("2Y"), self.yields.get("10Y")
        return (b - a) * 100 if a is not None and b is not None else None


@dataclass
class MacroSnapshot:
    at: datetime
    quotes: list[MacroQuote] = field(default_factory=list)
    curve: TreasuryCurve | None = None
    errors: dict[str, str] = field(default_factory=dict)

    def get(self, key: str) -> MacroQuote | None:
        return next((q for q in self.quotes if q.key == key), None)

    def to_json(self) -> dict[str, Any]:
        """入库用（alerts.macro_json）：异动当时的宏观环境，事后做事件研究时能直接用。"""
        out: dict[str, Any] = {"at": self.at.isoformat()}
        for q in self.quotes:
            d = asdict(q)
            d["ts"] = q.ts.isoformat()
            key = f"{q.theme}.{q.key}" if q.theme else q.key  # 板块指标带上主题名，避免和基础变量重名
            out[key] = {k: d[k] for k in ("symbol", "value", "change", "change_label", "change_1h", "ts", "off_hours")}
        if self.curve:
            out["treasury"] = {"day": self.curve.day.isoformat(), **self.curve.yields}
        return out


def asof(close: pd.Series, t: datetime) -> tuple[float, datetime] | None:
    """t 时刻（含）之前最后一根 K 线的收盘价。"""
    sub = close[close.index <= pd.Timestamp(t)]
    if sub.empty:
        return None
    return float(sub.iloc[-1]), sub.index[-1].to_pydatetime()


def last_us_close_before(calendar: "MarketCalendar", ts: datetime) -> datetime:
    """ts 之前最近一个美股收盘时刻（提前收盘日按实际收盘）。"""
    day = calendar.exchange_date(ts)
    s = calendar.session(day)
    if s is not None and s.close <= ts:
        return s.close
    return calendar.previous_session(day).close


def prev_day_end(calendar: "MarketCalendar", ts: datetime) -> datetime:
    """ts 所在日期之前最近一个美股交易日的当天结束时刻（美东 24:00）。"""
    prev = calendar.previous_session(calendar.exchange_date(ts)).day
    return datetime.combine(prev + timedelta(days=1), datetime.min.time(), tzinfo=calendar.tz) - timedelta(seconds=1)


def build_quote(ind: "MacroIndicator", close: pd.Series, calendar: "MarketCalendar | None", *,
                symbol: str, off_hours: bool) -> MacroQuote | None:
    close = close.dropna()
    close = close[close > 0] if ind.kind != "yield" else close
    if close.empty:
        return None
    value = float(close.iloc[-1])
    ts = close.index[-1].to_pydatetime()
    ref_mode = "24h" if ind.ref == "24h" and not off_hours else "close"
    ref = None
    if ref_mode == "24h":
        ref = asof(close, ts - timedelta(hours=24))
        label = "24h"
    elif calendar is not None and off_hours:
        ref = asof(close, last_us_close_before(calendar, ts))  # 期货：美股收盘那一刻的价格
        label = "较美股收盘"
    elif calendar is not None:
        ref = asof(close, prev_day_end(calendar, ts))  # 上一交易日最后一根（VIX 16:15 收盘，不能截在 16:00）
        label = "较昨收"
    else:
        label = "较昨收"
    hour = asof(close, ts - timedelta(hours=1))
    if hour is not None and hour[1] < ts - timedelta(hours=1) - ONE_HOUR_SLACK:
        hour = None  # 一小时前没在交易（开盘不足一小时）
    return MacroQuote(key=ind.key, name=(ind.off_hours_name or ind.name) if off_hours else ind.name, symbol=symbol,
                      kind=ind.kind, group=ind.group, value=value, ts=ts,
                      change=_change(ind.kind, value, ref[0] if ref else None), change_label=label,
                      change_1h=_change(ind.kind, value, hour[0] if hour else None), off_hours=off_hours)


def _change(kind: str, value: float, ref: float | None) -> float | None:
    if ref is None or not math.isfinite(ref):
        return None
    if kind == "yield":
        return (value - ref) * 100  # 百分点 → bp
    return (value / ref - 1) * 100 if ref > 0 else None


def parse_treasury_csv(text: str) -> TreasuryCurve | None:
    """财政部每日收益率曲线 CSV：第一行是表头（Date,"1 Mo",…,"2 Yr",…,"10 Yr",…,"30 Yr"），按日期倒序。"""
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or "Date" not in rows[0]:
        return None
    row = rows[0]
    try:
        day = datetime.strptime(row["Date"], "%m/%d/%Y").date()
    except ValueError:
        return None
    yields: dict[str, float] = {}
    for col, label in (("2 Yr", "2Y"), ("5 Yr", "5Y"), ("10 Yr", "10Y"), ("30 Yr", "30Y")):
        try:
            yields[label] = float(row[col])
        except (KeyError, TypeError, ValueError):
            continue
    return TreasuryCurve(day, yields) if yields else None


class MacroSource:
    """取一份宏观快照：基础宏观变量（settings.yaml 的 macro.indicators）+ 生效主题的板块指标（config/themes/ 的 indicators），
    一次下载。带缓存（默认 60 秒）：同一分钟里几只股票同时异动只下载一次。"""

    def __init__(self, settings: "Settings", calendar: "MarketCalendar | None" = None, *,
                 yf: "YFinancePriceSource | None" = None, http: httpx.AsyncClient | None = None):
        from tickersentinel.sources.prices_yfinance import YFinancePriceSource

        self.settings = settings
        self.cfg = settings.macro
        self.calendar = calendar
        self.yf = yf or YFinancePriceSource()
        self._http = http
        self._own_http = http is None
        self._cache: tuple[float, MacroSnapshot] | None = None
        self._curve: tuple[float, TreasuryCurve | None] | None = None
        self._lock = asyncio.Lock()

    def off_hours(self, now: datetime) -> bool:
        return self.calendar is not None and self.calendar.session_at(now) is None

    async def snapshot(self, now: datetime | None = None, *, use_cache: bool = True) -> MacroSnapshot:
        async with self._lock:
            if use_cache and self._cache and time.monotonic() - self._cache[0] < self.cfg.cache_seconds:
                return self._cache[1]
            snap = await self._fetch(now or utcnow())
            self._cache = (time.monotonic(), snap)
            return snap

    async def _fetch(self, now: datetime) -> MacroSnapshot:
        snap = MacroSnapshot(at=now)
        off = self.off_hours(now)
        wanted = [(None, ind) for ind in self.cfg.indicators]
        wanted += [(th.key, ind) for th in self.settings.universe.active_themes for ind in th.indicators]
        chosen = [(theme, ind, (ind.off_hours_symbol if off and ind.off_hours_symbol else ind.symbol)) for theme, ind in wanted]
        symbols = list(dict.fromkeys(sym for _, _, sym in chosen))
        bars_task = self.yf.intraday_bars(symbols, start=now - HISTORY, end=now + timedelta(minutes=1), prepost=True)
        curve_task = self._treasury(now) if self.cfg.treasury_curve else _none()
        bars, curve = await asyncio.gather(bars_task, curve_task, return_exceptions=True)
        if isinstance(bars, BaseException):
            snap.errors["yfinance"] = f"{type(bars).__name__}: {bars}"
            bars = {}
        if isinstance(curve, BaseException):
            snap.errors["treasury"] = f"{type(curve).__name__}: {curve}"
            curve = None
        snap.curve = curve
        for theme, ind, sym in chosen:
            df = bars.get(sym)
            label = f"{theme}.{ind.key}" if theme else ind.key
            if df is None or df.empty:
                snap.errors.setdefault(label, f"{sym} 没有数据")
                continue
            q = build_quote(ind, df["close"], self.calendar, symbol=sym, off_hours=off and sym != ind.symbol)
            if q is not None:
                q.theme = theme
                snap.quotes.append(q)
        if not snap.quotes and snap.curve is None:
            raise SourceError("宏观数据全部取不到：" + "；".join(f"{k}: {v}" for k, v in snap.errors.items()))
        return snap

    async def _treasury(self, now: datetime) -> TreasuryCurve | None:
        if self._curve and time.monotonic() - self._curve[0] < 3 * 3600:
            return self._curve[1]
        client = self._http or httpx.AsyncClient(timeout=self.settings.sources.http_timeout_seconds)
        try:
            curve = None
            for year in (now.year, now.year - 1):  # 一月初当年的文件可能还是空的
                resp = await client.get(TREASURY_URL.format(year=year), headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code != 200:
                    raise SourceError(f"[treasury] HTTP {resp.status_code}")
                curve = parse_treasury_csv(resp.text)
                if curve is not None:
                    break
        except httpx.HTTPError as exc:
            raise SourceError(f"[treasury] {type(exc).__name__}: {exc}") from exc
        finally:
            if self._http is None:
                await client.aclose()
        self._curve = (time.monotonic(), curve)
        return curve

    async def aclose(self) -> None:
        if self._http is not None and self._own_http:
            await self._http.aclose()


async def _none() -> None:
    return None
