"""异动详情页的走势图数据（GET /api/alerts/{id}/bars）：这只资产、它的基准和几只同业在异动前后的分钟线，
都换算成“较各自参照价的涨跌（%）”，和检测时同一口径：

- 这只资产：入库时记下的参照价（alerts.prev_close），和推送里的“较昨收”一致；
- 基准、同业：盘前 / 盘中 / 海外交易所 = 上一交易日收盘（日线）；盘后 = 当天收盘（日线有当天就用，否则取收盘那一刻的分钟线）；
  期货 / 加密货币 = 上一段结束那一刻的分钟价（jobs/monitor.prepare_context 的做法）；篮子由成分算出来（detect/basket.py）。
- 取不到参照价的，按窗口里第一根 K 线归一，并在 ref_note 里写明。

时间窗口：[max(这一段开始, 异动前 3 小时), min(这一段结束, 异动后 90 分钟, 现在)]；美股盘中段从盘前算起（同一个昨收）。
返回的时间是 Unix 秒，已经加上本地时区的偏移（图表库按 UTC 显示，加上偏移后看到的就是本地墙上时间）。

分钟线先查本地库（barstore.py，决策 21）：要取的每个代码库里都盖住了这段时间，就直接用库里的（不受 Yahoo 只保留约 30 天的限制），
否则照旧现取；参照价用的日线照旧找行情源要。payload 的 source 写明分钟线来自“本地库”还是“Yahoo”。
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import pandas as pd

from tickersentinel.config import Settings, is_pseudo, is_us_symbol
from tickersentinel.detect.basket import add_baskets
from tickersentinel.detect.baseline import prev_close_from_daily
from tickersentinel.detect.clock import Segment
from tickersentinel.jobs.monitor import MonitorGroup, asof_close, build_groups, fetch_bars
from tickersentinel.models import Alert
from tickersentinel.timeutil import ET, tz_abbr

if TYPE_CHECKING:
    from tickersentinel.barstore import BarStore

log = logging.getLogger(__name__)

BEFORE = timedelta(hours=3)
AFTER = timedelta(minutes=90)
FETCH_TIMEOUT = 20.0  # 秒：取行情的总超时
MAX_PEERS = 3
MINUTE_DATA_DAYS = 29  # yfinance 的 1 分钟线只保留约 30 天
TOO_OLD_REASON = "分钟线只保留约 30 天，这条异动的走势已经取不到"
CACHE_TTL = 600.0  # 秒：画完整的窗口缓存 10 分钟
CACHE_TTL_LIVE = 60.0  # 窗口还没走完（异动刚发生）：1 分钟后重取
SOURCE_STORE = "本地库"
SOURCE_LIVE = "Yahoo"


def find_group(settings: Settings, calendar, symbol: str) -> MonitorGroup | None:
    try:
        groups = build_groups(settings, calendar)
    except Exception as exc:  # 配置或交易日历出问题：退回只画这一只
        log.info("走势图：分组失败：%s", exc)
        return None
    return next((g for g in groups if symbol in g.symbols), None)


def _segment(group: MonitorGroup | None, ts: datetime) -> Segment | None:
    if group is None:
        return None
    try:
        return group.clock.segment_at(ts)
    except Exception:  # 交易日历超出范围
        return None


def _day_start(group: MonitorGroup, seg: Segment) -> datetime:
    """美股盘中段：从当天盘前开始画（同一个昨收、全天累计带着盘前的高低点）。"""
    if seg.kind == "regular" and group.prepost and hasattr(group.clock, "segments_on"):
        try:
            pre = next((g for g in group.clock.segments_on(seg.day) if g.kind == "pre" and g.key == seg.key), None)
        except Exception:
            pre = None
        if pre is not None:
            return pre.open
    return seg.open


def plan(settings: Settings, calendar, alert: Alert, now: datetime) -> dict[str, Any]:
    """要画哪些代码、哪段时间：{group, seg, start, end, bench, peers, fetch, baskets, prepost}。"""
    u = settings.universe
    sym = alert.symbol
    group = find_group(settings, calendar, sym)
    seg = _segment(group, alert.ts)
    if group is not None and seg is not None:
        start = max(_day_start(group, seg), alert.ts - BEFORE)
        end = min(seg.close, alert.ts + AFTER, now)
        bench = alert.benchmark or group.benchmarks.get(sym) or group.benchmark
        peers = [p for p in group.peers.get(sym, []) if p not in (sym, bench)][:MAX_PEERS]
        prepost = group.prepost
    else:  # 已经移出监控范围，或这个时刻不在任何交易时段：只画它自己和入库时的基准
        start = alert.ts - BEFORE
        end = min(alert.ts + AFTER, now)
        bench = alert.benchmark if alert.benchmark and not is_pseudo(alert.benchmark) else None
        peers = []
        prepost = is_us_symbol(sym)  # 美股带上盘前盘后
    end = max(end, alert.ts + timedelta(minutes=1))
    codes = [c for c in [sym, bench, *peers] if c]
    baskets: dict[str, list[str]] = {}
    for c in codes:
        if is_pseudo(c):
            members = (group.baskets.get(c) if group is not None else None) or u.basket_members(c)
            if members:
                baskets[c] = list(members)
    fetch: list[str] = []
    for c in codes:
        for x in baskets.get(c, [c] if not is_pseudo(c) else []):
            if x not in fetch:
                fetch.append(x)
    return {"group": group, "seg": seg, "start": start, "end": end, "bench": bench, "peers": peers, "codes": codes,
            "fetch": fetch, "baskets": baskets, "prepost": prepost,
            "night": bool(getattr(group, "night", False)) if group is not None else False,
            "basket_tz": group.basket_tz if group is not None else ET}


def shift(ts: datetime | pd.Timestamp, tz) -> int:
    """UTC 时刻 → 加上本地时区偏移的 Unix 秒（图表按 UTC 显示，看到的就是本地时间）。"""
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    py = t.to_pydatetime()
    off = py.astimezone(tz).utcoffset() or timedelta(0)
    return int(py.timestamp() + off.total_seconds())


def drop_repeated_hour(points: list, key=lambda p: p[0]) -> list:
    """shift 之后的点按时间排好：冬令时切换那天本地 01:00–02:00 出现两遍，第二遍的本地时间往回跳，
    图表库要求时间严格递增（否则整张图报错不画）。跳过时间没有前进的点——一年只在那一小时少画一段，
    其他时刻的标注都准确（不能整段换成同一个偏移：那样切换之前的点会错开 1 小时）。夏令时那天往前跳一小时，本来就递增。"""
    out, last = [], None
    for p in points:
        t = key(p)
        if last is not None and t <= last:
            continue
        out.append(p)
        last = t
    return out


def _ref_for(code: str, seg: Segment | None, intraday: dict[str, pd.DataFrame], daily: dict[str, pd.DataFrame],
             extra: dict[str, pd.DataFrame]) -> float | None:
    """基准 / 同业的参照价（与检测同一口径）；取不到为 None。"""
    if seg is None:
        return None
    if seg.kind in ("regular", "pre", "night", "exchange"):  # 夜盘、盘前、盘中都较前一个交易日收盘
        return prev_close_from_daily(daily.get(code), seg.day)
    if seg.kind == "post":
        d = daily.get(code)
        if d is not None and not d.empty and "close" in d:
            c = d["close"].dropna()
            c = c[pd.DatetimeIndex(c.index).date == seg.day]
            if len(c):
                return float(c.iloc[-1])
        if seg.ref_at is not None:
            return asof_close(intraday.get(code), seg.ref_at) or asof_close(extra.get(code), seg.ref_at)
        return None
    if seg.kind in ("cme", "utc") and seg.ref_at is not None:
        return asof_close(extra.get(code), seg.ref_at) or asof_close(intraday.get(code), seg.ref_at)
    return None


def _snap(times: list[int], t: int) -> int | None:
    """标记要落在已有的 K 线上：取不晚于 t 的最后一根（都晚于 t 时取第一根）。"""
    if not times:
        return None
    best = times[0]
    for x in times:
        if x <= t:
            best = x
        else:
            break
    return best


def _levels(settings: Settings, alert: Alert) -> list[dict[str, Any]]:
    """全天累计的档位线（只在这条异动有全天累计触发时画）：从参照价算的画在 ±档位，从日内高低点算的画在起点 ∓ 档位。"""
    day = next((t for t in alert.triggers if t.rule in ("day", "level")), None)
    if day is None:
        return []
    try:
        levels = list(settings.rules_for(alert.symbol).day_pct)
    except Exception:
        return []
    hit = day.level_pct or (levels[0] if levels else None)
    if hit is None:
        return []
    out = []
    shown = [x for x in levels if x <= hit] + [x for x in levels if x > hit][:1]
    for x in shown:
        base = day.anchor_pct if (day.rule == "day" and day.anchor_ts is not None and day.anchor_pct is not None) else 0.0
        out.append({"value": round(base + alert.direction * x, 3), "title": f"全天 {x:g}%", "hit": x <= hit})
    return out


def from_store(store: "BarStore", p: dict[str, Any], symbol: str) -> dict[str, pd.DataFrame] | None:
    """本地库能盖住这张图要的全部代码时，返回库里的分钟线；否则 None（照旧现取）。
    covers 只看首尾，中间停机留下的空档看不出来，所以再要求这只资产自己（篮子看成分）在窗口里确实有 K 线。同步读库，调用方放进线程。"""
    codes = p["fetch"]
    if not codes or not all(store.covers(c, p["start"], p["end"]) for c in codes):
        return None
    data = store.series(codes, p["start"], p["end"] + timedelta(minutes=1))
    own = p["baskets"].get(symbol, [symbol])
    lo, hi = pd.Timestamp(p["start"]), pd.Timestamp(p["end"])
    if not any((df := data.get(c)) is not None and ((df.index >= lo) & (df.index <= hi)).any() for c in own):
        return None
    return data


async def build(settings: Settings, calendar, prices, alert: Alert, now: datetime,
                timeout: float = FETCH_TIMEOUT, store: "BarStore | None" = None) -> dict[str, Any]:
    """走势图 JSON。取不到时 available=False，reason 写明原因（页面上直接显示）。
    store：本地分钟线库（可选），盖得住这段时间就不找行情源要分钟线。"""
    tz = settings.tz
    u = settings.universe
    out: dict[str, Any] = {"available": False, "reason": "", "symbol": alert.symbol, "series": [], "marker": None,
                           "anchor_time": None, "window_start": None, "levels": [], "tz_label": tz_abbr(alert.ts, tz),
                           "ref_label": alert.ref_label, "ref_note": None, "complete": False, "source": None}
    if prices is None and store is None:
        out["reason"] = "没有行情数据源（预览或配置里没有启用），画不了走势"
        return out
    p = plan(settings, calendar, alert, now)
    stored: dict[str, pd.DataFrame] | None = None
    if store is not None:
        try:
            stored = await asyncio.to_thread(from_store, store, p, alert.symbol)
        except Exception as exc:  # noqa: BLE001 - 库读不通：照旧现取
            log.info("走势图：读本地分钟线库失败：%s", exc)
    if stored is None and prices is None:
        out["reason"] = "没有行情数据源，本地分钟线库里也没有这段时间的数据，画不了走势"
        return out
    out["source"] = SOURCE_STORE if stored is not None else SOURCE_LIVE
    out["complete"] = p["end"] >= min(alert.ts + AFTER, p["seg"].close if p["seg"] is not None else alert.ts + AFTER)
    too_old = stored is None and now - alert.ts > timedelta(days=MINUTE_DATA_DAYS)
    seg: Segment | None = p["seg"]
    group: MonitorGroup | None = p["group"]
    stocks = [s for s in p["fetch"] if group is None or group.key not in ("cme", "utc")]
    need_extra = seg is not None and seg.ref_at is not None and seg.kind in ("cme", "utc", "post")

    async def fetch_all() -> tuple[dict, dict, dict]:
        if stored is not None:
            intraday = stored
        else:  # 和检测同一套取数：美股夜盘（hours: 24x5）并上 overnight_bars
            intraday = await fetch_bars(prices, p["fetch"], p["start"], p["end"] + timedelta(minutes=1),
                                        prepost=p["prepost"], night=p["night"])
        daily: dict = {}
        if prices is not None and stocks and (seg is None or seg.kind not in ("cme", "utc")):
            try:
                back = max(15, (now.date() - (seg.day if seg is not None else alert.ts.date())).days + 15)
                daily = await prices.daily_bars(stocks, lookback_days=back)
            except Exception as exc:  # 没有日线：参照价退回第一根 K 线
                log.info("走势图：取日线失败：%s", exc)
        extra: dict = {}
        if need_extra:
            ref_start, ref_end = seg.ref_at - timedelta(minutes=30), seg.ref_at + timedelta(minutes=1)
            if store is not None:  # 参照时刻附近的分钟线也先看库
                try:
                    extra = await asyncio.to_thread(store.series, p["fetch"], ref_start, ref_end)
                except Exception as exc:  # noqa: BLE001
                    log.info("走势图：读库里参照时刻的分钟线失败：%s", exc)
            if not extra and prices is not None:
                try:
                    extra = await prices.intraday_bars(p["fetch"], start=ref_start, end=ref_end, prepost=p["prepost"])
                except Exception as exc:
                    log.info("走势图：取参照时刻的分钟线失败：%s", exc)
        return intraday or {}, daily or {}, extra or {}

    try:
        intraday, daily, extra = await asyncio.wait_for(fetch_all(), timeout)
    except asyncio.TimeoutError:
        out["reason"] = f"取行情超时（{timeout:.0f} 秒），稍后刷新再试"
        return out
    except Exception as exc:  # yfinance 取不到分钟线时抛 SourceError，不是返回空表
        log.info("走势图：取分钟线失败（异动 %s）：%s", alert.id, exc)
        out["reason"] = (TOO_OLD_REASON if too_old
                         else f"取行情失败：{(str(exc) or type(exc).__name__)[:160]}")
        return out

    if p["baskets"]:
        try:
            intraday, daily = add_baskets(p["baskets"], intraday=intraday, daily=daily,
                                          anchor=seg.day if seg is not None else None, tz=p["basket_tz"])
        except Exception as exc:
            log.info("走势图：篮子计算失败：%s", exc)

    notes: list[str] = []
    for code in p["codes"]:
        df = intraday.get(code)
        if df is None or df.empty or "close" not in df:
            continue
        closes = df["close"].dropna().astype(float)
        closes = closes[(closes.index >= pd.Timestamp(p["start"])) & (closes.index <= pd.Timestamp(p["end"]))]
        closes = closes[closes > 0]
        if closes.empty:
            continue
        role = "symbol" if code == alert.symbol else "benchmark" if code == p["bench"] else "peer"
        ref = alert.prev_close if role == "symbol" and alert.prev_close else _ref_for(code, seg, intraday, daily, extra)
        if not ref or ref <= 0:
            ref = float(closes.iloc[0])
            notes.append(u.display(code))
        points = [{"time": shift(t, tz), "value": round((float(v) / ref - 1) * 100, 3)} for t, v in closes.sort_index().items()]
        points = drop_repeated_hour(points, key=lambda p: p["time"])  # 冬令时切换那一小时
        dedup: dict[int, dict] = {}
        for pt in points:  # 同一分钟只留一根（数据源偶尔重复）
            dedup[pt["time"]] = pt
        name = u.label(code)
        out["series"].append({"key": code, "label": u.display(code), "name": "" if name == u.display(code) else name,
                              "role": role, "ref": round(float(ref), 4), "points": sorted(dedup.values(), key=lambda x: x["time"])})

    sym_series = next((s for s in out["series"] if s["role"] == "symbol"), None)
    if sym_series is None:
        out["series"] = []
        out["reason"] = (TOO_OLD_REASON if too_old
                         else "数据源没有返回这段时间的分钟线（可能停牌、代码已变，或数据源暂时取不到）")
        return out
    out["series"].sort(key=lambda s: {"symbol": 0, "benchmark": 1, "peer": 2}[s["role"]])
    times = [pt["time"] for pt in sym_series["points"]]
    head = alert.headline
    out["marker"] = {"time": _snap(times, shift(alert.ts, tz)), "direction": alert.direction,
                     "text": head.short(alert.ref_label)}
    anchor = head.anchor_ts or next((t.anchor_ts for t in alert.triggers if t.anchor_ts is not None), None)
    if anchor is not None and anchor < alert.ts:
        out["anchor_time"] = _snap(times, shift(anchor, tz))
    win = head.window_minutes if head.rule in ("rapid", "idio") else None
    if win:
        ws = _snap(times, shift(alert.ts - timedelta(minutes=int(win)), tz))
        if ws is not None and ws != out["anchor_time"] and ws != out["marker"]["time"]:
            out["window_start"] = ws
        out["window_minutes"] = int(win)
    out["levels"] = _levels(settings, alert)
    if notes:
        out["ref_note"] = f"{'、'.join(notes)} 取不到参照价，按窗口里第一根 K 线归一"
    out["available"] = True
    return out


class ChartCache:
    """按异动 id 缓存成功的结果（窗口走完的 10 分钟，没走完的 1 分钟）。"""

    def __init__(self) -> None:
        self._data: dict[int, tuple[float, dict]] = {}

    def get(self, alert_id: int) -> dict | None:
        hit = self._data.get(alert_id)
        if hit is None:
            return None
        if hit[0] < _time.monotonic():
            self._data.pop(alert_id, None)
            return None
        return hit[1]

    def put(self, alert_id: int, payload: dict) -> None:
        if not payload.get("available"):
            return
        ttl = CACHE_TTL if payload.get("complete") else CACHE_TTL_LIVE
        if len(self._data) > 200:  # 防止无限增长
            self._data.clear()
        self._data[alert_id] = (_time.monotonic() + ttl, payload)
