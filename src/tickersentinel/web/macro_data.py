"""宏观页的数据（决策 21）：从本地分钟线库（barstore.py）读宏观变量，换成“较窗口起点的变化”，给趋势小图和“谁动了”排行用。

口径：
- 单位和推送里的宏观快照一致（sources/macro.py）：收益率（kind = yield）用 bp，其他用 %；
- 股指（有 off_hours_symbol 的）：窗口整个落在美股盘中时用现货指数，否则用期货（期货 24 小时连续，现货夜里断开），图上注明；
- 起点：窗口开始那一刻之前最后一根（三天内），没有就用窗口里第一根；
- 休市（周末、节假日）或库里的数据没更新时，预设窗口的终点移到非 24 小时品种最后一根（market_end）：周六看“24 小时”
  看的是周五收盘前的 24 小时，比特币也画同一段，横向才能比；
- 收益率没数据的时段（^TNX / ^TYX 在 Yahoo 上只有美东 08:20–15:00）用国债期货折算（splice）：Δ收益率 = β · Δln 期货价，
  β 用两边都有数据的 5 分钟变化回归（实测 10 年 ≈ −14bp / 1%，相关约 −0.95）；折算的点单独返回（fill），图上画虚线；
- 降采样：按窗口长短取 1 / 5 / 15 / 30 / 60 / 240 分钟一个点（每桶最后一根），每条线不超过 MAX_POINTS 个点，没成交的时段留空；
- 小图的纵轴是实际值（原油 104、VIX 19.4、收益率 4.93%），起点画一条虚线；变化（bp / %）在标题和十字线上显示，叠加视图才按变化画
  （2026-10-31 用户要求：熟悉的价位本身就有信息量，“跌 4%”反而丢了信息）；
- 持仓（assets.yaml 里 holding 的资产）也是一组（HOLDING_GROUP）：和宏观变量一起画、一起排“谁动了”（标 holding），
  “最近收盘”也算上它们（美股盘后到美东 20:00）；
- 对比图的“几倍日波动”：变化 ÷ sigma_day（平常的日波动 = 5 分钟变化的稳健标准差 × √(每天有数据的 5 分钟个数)，
  和 z 值同一思路），单位不同的利率、价格、持仓都能放在一根纵轴上比；
- 只在交易时段有数据的变量（VIX、KOSPI：近 CLASSIFY_DAYS 天工作日每天有数据的小时数中位数 < SESSION_HOURS）标 compress，
  小图把各段直接拼起来（前一段收盘接下一段开盘），不和其他图共用时间格；
- “谁动了”（movers）：一段时间内的变化 ÷ 这个变量平常的波动 = z 值。平常的波动用这段之前 SIGMA_DAYS 天里
  不重叠 5 分钟变化的 MAD（× 1.4826），按 √(这段里有成交的分钟数 / 5) 放大——和检测的 σ 同一思路（决策 3），
  利率、原油、VIX 的涨跌就能放在一起比。
返回的图表时间都加上了本地时区偏移（web/alerts_chart.shift），图表库按 UTC 显示时看到的就是本地墙上时间。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from tickersentinel.config import _infer_class, is_pseudo
from tickersentinel.timeutil import UTC
from tickersentinel.web.alerts_chart import drop_repeated_hour, shift

if TYPE_CHECKING:
    from tickersentinel.barstore import BarStore
    from tickersentinel.config import MacroIndicator, Settings
    from tickersentinel.market_calendar import MarketCalendar

MAX_POINTS = 800
STEPS = (1, 5, 15, 30, 60, 240)  # 分钟
REF_LOOKBACK = timedelta(days=3)  # 起点往回找多久（周一早上的窗口找得到周五收盘）
SIGMA_DAYS = 14  # 估计“平常的波动”用多少个自然日（约 10 个交易日）
MAD_SCALE = 1.4826
WINDOWS = {"today": "今天", "24h": "24 小时", "5d": "5 天", "1m": "1 个月"}
WINDOW_SPANS = {"24h": timedelta(hours=24), "5d": timedelta(days=5), "1m": timedelta(days=30)}
ALERT_BEFORE = timedelta(hours=3)
ALERT_AFTER = timedelta(minutes=90)
MARKET_GAP = timedelta(hours=2)  # 非 24 小时品种最后一根早于这么久：算休市（CME 每天只停 1 小时）
PROXY_GAP = timedelta(minutes=10)  # 收益率相邻两根相隔超过这么久才用期货补
PROXY_ANCHOR = timedelta(minutes=30)  # 锚点时刻的期货价最多往前找多久
BETA_MIN_N = 60  # 估计 β 至少要这么多个 5 分钟变化
SPARK_MAX_AGE = timedelta(days=7)  # 小走势线：最后一根早于这么久就不画
SESSION_HOURS = 18.0  # 工作日每天有数据的小时数（中位数）低于这个：只在交易时段有数据（期货 ≈ 23、VIX ≈ 13、KOSPI ≈ 6.5）
CLASSIFY_DAYS = 10  # 判断“只在交易时段有数据”看多少天
MIN_CLASSIFY_DAYS = 3  # 去掉头尾两天后至少要有这么多个工作日才判断
ET = "America/New_York"
HOLDING_GROUP = "持仓"


@dataclass
class Indicator:
    key: str  # 板块指标为 <主题>.<key>
    name: str
    group: str
    kind: str
    symbol: str
    off_hours_symbol: str | None = None
    off_hours_name: str | None = None
    theme: str | None = None  # 板块指标所属主题的名称
    proxy: str | None = None  # 收益率没数据时用来折算的国债期货
    holding: bool = False  # 持仓（assets.yaml），不是宏观变量

    @property
    def unit(self) -> str:
        return "bp" if self.kind == "yield" else "%"


def indicators(settings: "Settings", *, holdings: bool = True) -> list[Indicator]:
    """基础宏观变量（settings.yaml 的 macro.indicators）+ 生效主题的板块指标（顺序同 /macro）+ 持仓（holdings=False 时不要）。
    持仓的 key 是 hold.<代码>；和宏观变量同一个代码的持仓不重复列。"""
    out = [_ind(i) for i in settings.macro.indicators]
    for th in settings.universe.active_themes:
        for i in th.indicators:
            ind = _ind(i)
            ind.key, ind.theme, ind.group = f"{th.key}.{i.key}", th.name, f"板块指标 · {th.name}"
            out.append(ind)
    if holdings:
        seen = {i.symbol for i in out}
        for inst in settings.universe.holdings:
            if inst.symbol in seen or is_pseudo(inst.symbol):
                continue
            seen.add(inst.symbol)
            out.append(Indicator(f"hold.{inst.symbol}", inst.name or inst.symbol, HOLDING_GROUP, "price", inst.symbol,
                                 holding=True))
    return out


def _ind(i: "MacroIndicator") -> Indicator:
    return Indicator(i.key, i.name, i.group, i.kind, i.symbol, i.off_hours_symbol, i.off_hours_name, proxy=i.proxy_symbol)


def all_symbols(settings: "Settings") -> list[str]:
    out: list[str] = []
    for ind in indicators(settings):
        for s in (ind.symbol, ind.off_hours_symbol, ind.proxy):
            if s and s not in out:
                out.append(s)
    return out


# ───────── 时间窗口 ─────────


def market_end(store: "BarStore", settings: "Settings", now: datetime) -> datetime | None:
    """休市（周末、节假日）或库里的数据没更新时，预设窗口该截到哪：基础宏观变量和持仓里非 24 小时品种（期货、利率、外汇、
    股指、个股）最后一根的结束时刻——周五是美股盘后结束的美东 20:00（期货 17:00 就停了）；最后一根离现在不到 MARKET_GAP
    （正常交易）或库里没有数据时 None。"""
    last: datetime | None = None
    syms = [(i.symbol, i.off_hours_symbol) for i in settings.macro.indicators]
    syms += [(i.symbol, None) for i in settings.universe.holdings if not is_pseudo(i.symbol)]
    for pair in syms:
        for s in pair:
            if not s or _infer_class(s) == "crypto":
                continue
            t = store.last_ts(s)
            if t is not None and (last is None or t > last):
                last = t
    if last is None or now - last <= MARKET_GAP:
        return None
    return last + timedelta(minutes=1)


def resolve_window(window: str | None, now: datetime, tz, *, start: datetime | None = None, end: datetime | None = None,
                   alert_ts: datetime | None = None, anchor: datetime | None = None) -> tuple[datetime, datetime, str]:
    """(start, end, 名称)。优先级：异动（前 3 小时、后 90 分钟）> 自定义起止 > 预设窗口（默认 24 小时）。
    anchor（market_end 的结果）：预设窗口截到这一刻，不截到现在（“今天”= anchor 那一天）。"""
    if alert_ts is not None:
        return alert_ts - ALERT_BEFORE, min(alert_ts + ALERT_AFTER, now), "异动前后"
    if start is not None:
        e = min(end or now, now)
        if e <= start:
            raise ValueError("结束时间要晚于开始时间")
        return start, e, "自定义"
    w = window if window in WINDOWS else "24h"
    if anchor is not None and anchor < now:
        now = anchor
    if w == "today":
        local = now.astimezone(tz)
        midnight = datetime(local.year, local.month, local.day, tzinfo=tz)
        return midnight.astimezone(UTC), now, WINDOWS[w]
    return now - WINDOW_SPANS[w], now, WINDOWS[w]


def step_minutes(start: datetime, end: datetime) -> int:
    span = (end - start).total_seconds() / 60
    return next((s for s in STEPS if span / s <= MAX_POINTS), STEPS[-1])


def sub_tag(symbol: str) -> str:
    """盘外替代品的标签：期货（ES=F）写“期货”，其他（SOXX 这类盘前盘后也交易的 ETF）写“ETF”。"""
    return "期货" if _infer_class(symbol) == "future" else "ETF"


def pick_symbol(ind: Indicator, calendar: "MarketCalendar | None", start: datetime, end: datetime) -> tuple[str, bool]:
    """(代码, 是否换成了盘外替代品)。窗口整个落在同一个美股盘中时用现货指数，否则用 off_hours_symbol
    （期货，或盘前盘后也交易的 ETF）。"""
    if not ind.off_hours_symbol or calendar is None:
        return ind.symbol, False
    try:
        a, b = calendar.session_at(start), calendar.session_at(end)
    except Exception:  # 交易日历超出范围
        a = b = None
    if a is not None and b is not None and a.day == b.day:
        return ind.symbol, False
    return ind.off_hours_symbol, True


# ───────── 换算 ─────────


def change(kind: str, value: float, ref: float) -> float | None:
    if not (math.isfinite(value) and math.isfinite(ref)):
        return None
    if kind == "yield":
        return (value - ref) * 100
    return (value / ref - 1) * 100 if ref > 0 else None


def base_value(close: pd.Series, start: datetime) -> float | None:
    """窗口起点的参照值：start 之前（REF_LOOKBACK 内）最后一根，没有就用窗口里第一根。
    严格早于 start：从 start 开始的那根 K 线，收盘已经是一分钟之后的价格。"""
    ts = pd.Timestamp(start)
    before = close[(close.index < ts) & (close.index >= ts - pd.Timedelta(REF_LOOKBACK))]
    if len(before):
        return float(before.iloc[-1])
    after = close[close.index >= ts]
    return float(after.iloc[0]) if len(after) else None


def downsample(close: pd.Series, step: int) -> pd.Series:
    """每 step 分钟一个点（桶内最后一根，时间记在桶的开头）；没有成交的桶不出现。"""
    if step <= 1 or close.empty:
        return close
    return close.resample(f"{step}min", label="left", closed="left").last().dropna()


def proxy_beta(y: pd.Series, p: pd.Series) -> float | None:
    """收益率（%）对期货对数价格的回归系数 β：Δy = β · Δln p（数值上 = 期货涨 1% 时收益率变几个 bp）。
    用两边都有数据的相邻 5 分钟桶；样本太少、方向不对（应为负）或相关太弱（> −0.5）时 None，不折算。"""
    if y.empty or p.empty:
        return None
    a = y.resample("5min", label="left", closed="left").last()
    b = p[p > 0].resample("5min", label="left", closed="left").last()
    df = pd.concat({"y": a, "p": b}, axis=1).dropna()
    if len(df) <= BETA_MIN_N:
        return None
    consec = (df.index.to_series().diff() == pd.Timedelta(minutes=5)).to_numpy()
    dy = np.diff(df["y"].to_numpy(dtype=float))[consec[1:]]
    dp = np.diff(np.log(df["p"].to_numpy(dtype=float)))[consec[1:]]
    ok = np.isfinite(dy) & np.isfinite(dp)
    dy, dp = dy[ok], dp[ok]
    if len(dy) < BETA_MIN_N or float(np.var(dp)) <= 0 or float(np.var(dy)) <= 0:
        return None
    corr = float(np.corrcoef(dp, dy)[0, 1])
    beta = float(np.cov(dp, dy)[0, 1] / np.var(dp, ddof=1))
    if not (math.isfinite(beta) and math.isfinite(corr)) or beta >= 0 or corr > -0.5:
        return None
    return beta


def splice(y: pd.Series, p: pd.Series, beta: float) -> tuple[pd.Series, pd.Series]:
    """收益率 y 的空档用期货 p 折算补上，返回 (补好的序列, 是否折算的标记)。
    空档：期货有数据、收益率没有，而且前后两根真实数据相隔超过 PROXY_GAP（最后一根之后、第一根之前的也算，
    只要离那一根超过 PROXY_GAP——最新的几分钟收益率只是还没到）。
    折算：y(t) = y(锚点) + β·(ln p(t) − ln p(锚点))；锚点 = 空档前最后一根真实数据（第一根之前的空档用之后第一根），
    锚点时刻的期货价取它之前最近一根（PROXY_ANCHOR 内），取不到的那段不补。"""
    flag = pd.Series(False, index=y.index)
    p = p[p > 0]
    if y.empty or p.empty:
        return y, flag
    yt = y.index.as_unit("ns").asi8  # pandas 3 的索引可能是微秒：统一成纳秒再比较
    yv = y.to_numpy(dtype=float)
    pt = p.index.as_unit("ns").asi8
    lp = np.log(p.to_numpy(dtype=float))
    gap = pd.Timedelta(PROXY_GAP).value
    n = len(yt)
    pos = np.searchsorted(yt, pt, side="left")  # 第一根 >= t 的真实数据
    exact = (pos < n) & (yt[np.minimum(pos, n - 1)] == pt)
    has_prev, has_next = pos > 0, pos < n
    prev_t = np.where(has_prev, yt[np.maximum(pos - 1, 0)], 0)
    next_t = np.where(has_next, yt[np.minimum(pos, n - 1)], 0)
    in_gap = ~exact & np.where(
        has_prev & has_next, (next_t - prev_t) > gap,
        np.where(has_prev, (pt - prev_t) > gap, (next_t - pt) > gap))
    if not in_gap.any():
        return y, flag
    anchor_i = np.where(has_prev, pos - 1, pos)[in_gap]
    anchor_t = yt[anchor_i]
    j = np.searchsorted(pt, anchor_t, side="right") - 1  # 锚点时刻（或之前最近）的期货价
    ok = (j >= 0) & (anchor_t - pt[np.maximum(j, 0)] <= pd.Timedelta(PROXY_ANCHOR).value)
    if not ok.any():
        return y, flag
    t_fill = pt[in_gap][ok]
    v_fill = yv[anchor_i[ok]] + beta * (lp[in_gap][ok] - lp[j[ok]])
    idx = pd.to_datetime(t_fill, unit="ns", utc=y.index.tz is not None)
    if y.index.tz is not None:
        idx = idx.tz_convert(y.index.tz)
    filled = pd.Series(v_fill, index=idx.as_unit(y.index.unit))
    out = pd.concat([y, filled]).sort_index()
    mark = pd.concat([flag, pd.Series(True, index=filled.index)]).sort_index()
    return out, mark


def _closes(ind: Indicator, sym: str, data: dict[str, pd.DataFrame], *,
            beta_from: datetime | None = None) -> tuple[pd.Series | None, pd.Series | None, float | None]:
    """(收盘价序列, 是否折算的标记, β)。收益率有期货可用时补上空档；β 用 beta_from 之后的数据估计（默认全部）。"""
    df = data.get(sym)
    close = df["close"].dropna() if df is not None and not df.empty else None
    pdf = data.get(ind.proxy) if ind.proxy and sym == ind.symbol else None
    if pdf is None or pdf.empty:
        return close, None, None
    pc = pdf["close"].dropna()
    if close is None or close.empty:  # 收益率库里一根都没有：没有锚点，没法折算
        return close, None, None
    lo = pd.Timestamp(beta_from) if beta_from is not None else None
    beta = proxy_beta(close if lo is None else close[close.index >= lo], pc if lo is None else pc[pc.index >= lo])
    if beta is None:
        return close, None, None
    spliced, mark = splice(close, pc, beta)
    return spliced, mark, beta


def _fill_runs(pts: list[tuple[int, float, bool]]) -> tuple[list[list[float]], list[list[float]]]:
    """降采样后的点（时间, 值, 是否折算）→ (真实点, 虚线点)。虚线的每一段带上前后相邻的真实点，和实线接得上。"""
    real = [[t, v] for t, v, f in pts if not f]
    fill: list[list[float]] = []
    for i, (t, v, f) in enumerate(pts):
        if not f:
            continue
        if (i == 0 or not pts[i - 1][2]) and i > 0:
            fill.append([pts[i - 1][0], pts[i - 1][1]])
        fill.append([t, v])
        if i + 1 < len(pts) and not pts[i + 1][2]:
            fill.append([pts[i + 1][0], pts[i + 1][1]])
    return real, fill


def hours_per_day(close: pd.Series) -> float | None:
    """工作日（美东日期）每天有数据的小时数的中位数（按有数据的 5 分钟桶数算）。
    去掉头尾两天（取数的起止点切在一天中间，必然不完整）；剩下不到 MIN_CLASSIFY_DAYS 天（新加的代码、刚开始补历史）时 None——
    不判断，按 24 小时的画，免得半天的数据把期货、外汇误判成“只在交易时段有数据”。
    用中位数：提前收盘日不影响判断。首尔时段跨美东午夜，每个美东日期各算半段，照样远低于 SESSION_HOURS。"""
    if close.empty:
        return None
    b = pd.DatetimeIndex(close.index.floor("5min").unique()).tz_convert(ET)
    b = b[b.weekday < 5]
    if not len(b):
        return None
    per_day = (pd.Series(1, index=b).groupby(b.date).size() * 5 / 60).sort_index().iloc[1:-1]
    if len(per_day) < MIN_CLASSIFY_DAYS:
        return None
    return float(per_day.median())


def sigma_day(close: pd.Series, kind: str, hpd: float | None) -> float | None:
    """平常的日波动（bp 或 %）：5 分钟变化的稳健标准差 × √(每天有数据的 5 分钟个数)。对比图“几倍日波动”的分母。
    hpd（每天有数据的小时数）不知道时按这段数据自己的 5 分钟桶数估计；算不出来时 None（对比图里这条线不画）。"""
    s5 = sigma_5m(close, kind)
    if s5 is None:
        return None
    if hpd is None:
        b = close.index.floor("5min").unique()
        days = max(1, len(pd.DatetimeIndex(b).tz_convert(ET).normalize().unique()))
        hpd = len(b) * 5 / 60 / days
    return round(s5 * math.sqrt(max(hpd, 0.5) * 12), 4)


def gap_seconds(step: int) -> int:
    """相邻两点隔多久算“没有交易”：3 个步长，至少 20 分钟（和 macro.js 的 gapSeconds 一致）。"""
    return max(3 * step, 20) * 60


def has_gap(times: list[int], step: int) -> bool:
    g = gap_seconds(step)
    return any(b - a > g for a, b in zip(times, times[1:]))


def series_payload(store: "BarStore", settings: "Settings", calendar: "MarketCalendar | None",
                   start: datetime, end: datetime) -> dict[str, Any]:
    """趋势小图：每个变量的实际值（points / fill 是 [图表时间, 值]），附窗口起点的参照值 base 和较起点的变化 change（bp 或 %）。
    按分组排好；库里没有数据的变量也列出来（items 里 points 为空）。
    收益率有期货折算的部分放在 fill（画虚线），proxy 写明用的哪个期货、beta_bp = 期货涨 1% 时收益率变几个 bp。
    compress：只在交易时段有数据（hours_per_day < SESSION_HOURS），小图把各段拼起来。"""
    tz = settings.tz
    inds = indicators(settings)
    step = step_minutes(start, end)
    picks = {ind.key: pick_symbol(ind, calendar, start, end) for ind in inds}
    wanted = {s for s, _ in picks.values()} | {ind.proxy for ind in inds if ind.proxy}
    lookback = start - max(REF_LOOKBACK, timedelta(days=CLASSIFY_DAYS))
    data = store.series(wanted, lookback, end)
    groups: dict[str, list[dict[str, Any]]] = {}
    last_ts: datetime | None = None
    for ind in inds:
        sym, fut = picks[ind.key]
        item: dict[str, Any] = {"key": ind.key, "name": (ind.off_hours_name or ind.name) if fut else ind.name,
                                "kind": ind.kind, "unit": ind.unit, "symbol": sym, "futures": fut,
                                "sub_tag": sub_tag(sym) if fut else None, "holding": ind.holding, "group": ind.group,
                                "sigma_day": None,
                                "points": [], "fill": [], "proxy": None, "beta_bp": None,
                                "change": None, "value": None, "base": None, "value_implied": False,
                                "compress": False, "hours_per_day": None}
        close, mark, beta = _closes(ind, sym, data)
        if close is not None and not close.empty:
            recent = close[close.index >= pd.Timestamp(end - timedelta(days=CLASSIFY_DAYS))]
            hpd = hours_per_day(recent)
            item["hours_per_day"] = round(hpd, 1) if hpd is not None else None
            real = data[sym]["close"].dropna()
            item["sigma_day"] = sigma_day(real[real.index >= pd.Timestamp(end - timedelta(days=CLASSIFY_DAYS))],
                                          ind.kind, hpd)
            session_only = hpd is not None and hpd < SESSION_HOURS
            ref = base_value(close, start)
            inwin = (close.index >= pd.Timestamp(start)) & (close.index <= pd.Timestamp(end))
            win = close[inwin]
            if ref is not None and len(win):
                frame = pd.DataFrame({"v": win, "f": (mark[inwin] if mark is not None else False)})
                if step > 1:
                    frame = frame.resample(f"{step}min", label="left", closed="left").last().dropna(subset=["v"])
                pts = [(shift(t, tz), round(float(v), 6), bool(f))
                       for t, v, f in zip(frame.index, frame["v"].to_numpy(dtype=float), frame["f"].to_numpy())
                       if math.isfinite(v)]
                pts = drop_repeated_hour(pts)  # 冬令时切换那天本地 01:00–02:00 出现两遍：时间不能往回跳
                item["points"], item["fill"] = _fill_runs(pts)
                # 拼接只在窗口里真有空档时才做：窗口落在一个交易时段里（盘中的异动窗口、股指换成现货的窗口）照常和其他图对齐
                item["compress"] = session_only and has_gap([p[0] for p in pts], step)
                if item["fill"]:
                    item["proxy"], item["beta_bp"] = ind.proxy, round(beta, 1) if beta is not None else None
                item["value"] = float(win.iloc[-1])
                item["value_implied"] = bool(mark[inwin].iloc[-1]) if mark is not None else False
                item["base"] = ref
                item["change"] = change(ind.kind, float(win.iloc[-1]), ref)
                if not item["value_implied"]:
                    t = win.index[-1].to_pydatetime()
                    last_ts = t if last_ts is None or t > last_ts else last_ts
        groups.setdefault(ind.group, []).append(item)
    return {"start": start.isoformat(), "end": end.isoformat(), "start_local": shift(start, tz), "end_local": shift(end, tz),
            "step_minutes": step, "data_until": last_ts.isoformat() if last_ts else None,
            "groups": [{"name": g, "items": items} for g, items in groups.items()]}


# ───────── 谁动了 ─────────


def five_min_changes(close: pd.Series, kind: str) -> np.ndarray:
    """不重叠的 5 分钟变化（bp 或对数收益 %），只取相邻两个 5 分钟桶之间的（中间没成交的空档不算）。"""
    if close.empty:
        return np.array([])
    b = close.resample("5min", label="left", closed="left").last().dropna()
    if len(b) < 2:
        return np.array([])
    gaps = b.index.to_series().diff() == pd.Timedelta(minutes=5)
    v = b.to_numpy(dtype=float)
    if kind == "yield":
        d = np.diff(v) * 100
    else:
        with np.errstate(divide="ignore", invalid="ignore"):
            d = np.diff(np.log(v)) * 100
    d = d[gaps.to_numpy()[1:]]
    return d[np.isfinite(d)]


def sigma_5m(close: pd.Series, kind: str) -> float | None:
    d = five_min_changes(close, kind)
    if len(d) < 30:
        return None
    mad = float(np.median(np.abs(d - np.median(d)))) * MAD_SCALE
    return mad if mad > 0 else None


def traded_minutes(close: pd.Series, start: datetime, end: datetime) -> int:
    """[start, end] 里有成交的分钟数（按有数据的 5 分钟桶数 × 5 估计，1 分钟线和压缩后的 5 分钟线一样算）。"""
    win = close[(close.index > pd.Timestamp(start)) & (close.index <= pd.Timestamp(end))]
    if win.empty:
        return 0
    return 5 * int(win.index.floor("5min").nunique())


def asof_value(close: pd.Series, t: datetime, lookback: timedelta = REF_LOOKBACK) -> float | None:
    ts = pd.Timestamp(t)
    sub = close[(close.index <= ts) & (close.index >= ts - pd.Timedelta(lookback))]
    return float(sub.iloc[-1]) if len(sub) else None


def movers(store: "BarStore", settings: "Settings", calendar: "MarketCalendar | None",
           start: datetime, end: datetime) -> dict[str, Any]:
    """[start, end] 里每个变量的变化和 z 值，按 |z| 从大到小排（算不出 z 的排在最后，按变化的绝对值）。"""
    inds = indicators(settings)
    picks = {ind.key: pick_symbol(ind, calendar, start, end) for ind in inds}
    lookback = start - timedelta(days=SIGMA_DAYS)
    wanted = {s for s, _ in picks.values()} | {ind.proxy for ind in inds if ind.proxy}
    data = store.series(wanted, lookback, end)
    items: list[dict[str, Any]] = []
    for ind in inds:
        sym, fut = picks[ind.key]
        df = data.get(sym)
        if df is None or df.empty:
            continue
        real = df["close"].dropna()
        # 收益率的空档用期货折算：起点、终点落在夜里时也有值；平常的波动只用真实数据估计
        close, mark, _ = _closes(ind, sym, data)
        if close is None or close.empty:
            continue
        a = base_value(close, start)
        b = asof_value(close, end)
        if a is None or b is None:
            continue
        chg = change(ind.kind, b, a)
        if chg is None:
            continue
        sig5 = sigma_5m(real[real.index < pd.Timestamp(start)], ind.kind)
        minutes = traded_minutes(close, start, end)
        implied = False  # 起点、终点或中间用到了期货折算
        if mark is not None:
            ts0, ts1 = pd.Timestamp(start), pd.Timestamp(end)
            before = mark[mark.index < ts0]
            implied = bool(mark[(mark.index >= ts0) & (mark.index <= ts1)].any()) or bool(len(before) and before.iloc[-1])
        z = None
        if sig5 is not None and minutes > 0:
            z = chg / (sig5 * math.sqrt(minutes / 5))
        items.append({"key": ind.key, "name": (ind.off_hours_name or ind.name) if fut else ind.name, "group": ind.group,
                      "kind": ind.kind, "unit": ind.unit, "symbol": sym, "futures": fut,
                      "change": round(chg, 4), "z": round(z, 3) if z is not None else None,
                      "from": a, "to": b, "minutes": minutes, "proxy": ind.proxy if implied else None,
                      "holding": ind.holding, "sub_tag": sub_tag(sym) if fut else None})
    # 按 |z| 排：涨得最明显和跌得最明显的都排在前面（方向看颜色）；截取前几条时不会把大跌的变量截掉
    items.sort(key=lambda x: (x["z"] is None, -abs(x["z"] if x["z"] is not None else x["change"])))
    return {"start": start.isoformat(), "end": end.isoformat(), "items": items}


# ───────── 收益率曲线、小走势线 ─────────


def curve_payload(store: "BarStore", since: date) -> dict[str, Any]:
    df = store.curve(since)
    if df.empty:
        return {"days": [], "series": {}}
    days = [d.isoformat() for d in df.index]
    series = {t: [[d, (None if pd.isna(v) else float(v))] for d, v in zip(days, df[t])] for t in df.columns}
    if "2Y" in df.columns and "10Y" in df.columns:
        spread = (df["10Y"] - df["2Y"]) * 100
        series["2s10s"] = [[d, (None if pd.isna(v) else round(float(v), 1))] for d, v in zip(days, spread)]
    return {"days": days, "series": series}


def sparkline(store: "BarStore", symbol: str, now: datetime, *, proxy: str | None = None, hours: int = 24, width: int = 64,
              height: int = 18, points: int = 64) -> dict[str, Any] | None:
    """总览看板每行的小走势线：这个代码最后一根往前 hours 小时的收盘价（和看板“24h”同一口径：周末看的是周五收盘前的
    24 小时），缩放进 width × height 的 SVG，返回 {points, up}。最后一根早于 SPARK_MAX_AGE 或数据太少时 None。
    proxy：收益率的空档用这个国债期货折算补上（同宏观页）。横轴按点的顺序排，没有交易的时段不占位置。"""
    last = store.last_ts(symbol)
    if last is None:
        return None
    if proxy:
        pl = store.last_ts(proxy)
        if pl is not None and pl > last:
            last = pl
    if now - last > SPARK_MAX_AGE:
        return None
    start = last - timedelta(hours=hours)
    if proxy:
        ind = Indicator(symbol, symbol, "", "yield", symbol, proxy=proxy)
        data = store.series([symbol, proxy], start - REF_LOOKBACK, last)
        close, _, _ = _closes(ind, symbol, data)
        if close is None:
            return None
        close = close[close.index >= pd.Timestamp(start)]
    else:
        data = store.series([symbol], start, last).get(symbol)
        if data is None:
            return None
        close = data["close"].dropna()
    if len(close) < 5:
        return None
    step = max(1, math.ceil(len(close) / points))
    vals = close.to_numpy(dtype=float)[::step]
    if vals[-1] != close.iloc[-1]:
        vals = np.append(vals, close.iloc[-1])
    return spark_points(vals, width, height)


def spark_points(vals, width: int = 64, height: int = 18) -> dict[str, Any]:
    """一串数缩放进 width × height 的 SVG polyline：{points, up, width, height}。"""
    vals = np.asarray(vals, dtype=float)
    lo, hi = float(vals.min()), float(vals.max())
    rng = hi - lo or 1.0
    n = len(vals)
    pts = " ".join(f"{i * (width - 2) / max(n - 1, 1) + 1:.1f},{height - 1 - (v - lo) / rng * (height - 2):.1f}"
                   for i, v in enumerate(vals))
    return {"points": pts, "up": bool(vals[-1] >= vals[0]), "width": width, "height": height}


CURVE_ROWS = (("2Y", "美债2年"), ("10Y", "美债10年"), ("30Y", "美债30年"), ("2s10s", "2s10s 利差"))


def curve_rows(store: "BarStore", today: date, days: int = 90) -> list[dict[str, Any]]:
    """总览看板的“财政部收益率曲线”：每个期限一行（和其他行同样的列），值 = 最近一天的收盘收益率（利差为 bp），
    变化 = 较前一个交易日（bp），小走势线 = 最近 days 天的日线（财政部每天只公布一次收盘，没有日内数据）。"""
    df = store.curve(today - timedelta(days=days))
    if df.empty:
        return []
    if "2Y" in df.columns and "10Y" in df.columns:
        df = df.assign(**{"2s10s": (df["10Y"] - df["2Y"]) * 100})
    out = []
    for key, name in CURVE_ROWS:
        if key not in df.columns:
            continue
        s = df[key].dropna()
        if s.empty:
            continue
        bp = 1.0 if key == "2s10s" else 100.0  # 收益率是 %，换成 bp
        out.append({"key": key, "name": name, "value": float(s.iloc[-1]), "spread": key == "2s10s",
                    "change_bp": (float(s.iloc[-1] - s.iloc[-2]) * bp) if len(s) >= 2 else None,
                    "day": s.index[-1], "spark": spark_points(s.to_numpy()) if len(s) >= 5 else None})
    return out
