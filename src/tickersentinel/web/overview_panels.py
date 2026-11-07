"""总览页右侧的几块：宏观看板、今明日程、今日异动。每块都只读，数据缺失时给出说明而不是报错。"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from tickersentinel.models import RULE_PRIORITY, Event
from tickersentinel.timeutil import UTC, utcnow
from tickersentinel.web.common import to_dt

log = logging.getLogger(__name__)


# ───────── 宏观看板 ─────────

MACRO_GROUP_ORDER = ["利率", "大宗商品", "股指", "其他"]
MACRO_EXTRA_SECONDS = 5.0  # 网页比推送多等几秒（推送是 macro.timeout_seconds）


def group_quotes(quotes: list) -> list[tuple[str, list]]:
    groups: dict[str, list] = {}
    for q in quotes:
        groups.setdefault(q.group or "其他", []).append(q)
    order = MACRO_GROUP_ORDER + [g for g in groups if g not in MACRO_GROUP_ORDER]
    return [(g, groups[g]) for g in order if g in groups]


async def macro_data(ctx) -> dict[str, Any]:
    """{state: disabled | error | ok, ...}。超时或出错也返回 200 的说明，不让 htmx 报错。"""
    if ctx.macro is None:
        return {"state": "disabled"}
    timeout = float(ctx.settings.macro.timeout_seconds) + MACRO_EXTRA_SECONDS
    try:
        snap = await asyncio.wait_for(ctx.macro.snapshot(), timeout)
    except asyncio.TimeoutError:
        return {"state": "error", "reason": f"等了 {timeout:g} 秒还没取到"}
    except Exception as exc:  # noqa: BLE001
        return {"state": "error", "reason": _short(f"{type(exc).__name__}: {exc}")}
    quotes = list(getattr(snap, "quotes", None) or [])
    base = [q for q in quotes if getattr(q, "theme", None) is None]
    themes: list[dict[str, Any]] = []
    u = ctx.settings.universe
    for key in dict.fromkeys(q.theme for q in quotes if getattr(q, "theme", None) is not None):
        th = u.themes.get(key)
        themes.append({"key": key, "name": th.name if th else key, "quotes": [q for q in quotes if q.theme == key]})
    curve = getattr(snap, "curve", None)
    curve_items = []
    if curve is not None:
        for k in ("2Y", "10Y", "30Y"):
            if k in (curve.yields or {}):
                curve_items.append((k, curve.yields[k]))
    errors = getattr(snap, "errors", None) or {}
    sparks = await sparklines(ctx, [q.symbol for q in quotes])
    return {
        "state": "ok", "snap": snap, "groups": group_quotes(base), "themes": themes, "curve": curve,
        "curve_items": curve_items, "curve_rows": await curve_rows(ctx),
        "off_hours": any(getattr(q, "off_hours", False) for q in quotes),
        "errors": list(errors)[:6], "at": getattr(snap, "at", None), "sparks": sparks,
    }


async def curve_rows(ctx) -> list[dict[str, Any]]:
    """财政部收益率曲线按行显示（和其他行对齐，带最近 90 天的日线小走势线）；没有分钟线库或库里还没有曲线时为空，
    看板退回快照里的一行文字。"""
    store = getattr(ctx, "bars", None)
    if store is None:
        return []
    from tickersentinel.web.macro_data import curve_rows as rows

    try:
        return await asyncio.to_thread(rows, store, utcnow().date())
    except Exception as exc:  # pragma: no cover - 只是不画
        log.warning("收益率曲线读库失败：%s", exc)
        return []


async def sparklines(ctx, symbols: list[str]) -> dict[str, dict[str, Any]]:
    """每行的小走势线（{代码: macro_data.sparkline 的结果}）：这个代码最后一根往前 24 小时，收益率的空档用国债期货折算。
    没有分钟线库、库里没数据的代码不出现；读库是同步 SQLite，放进线程里一次算完。出错只是不画，不影响看板。"""
    store = getattr(ctx, "bars", None)
    if store is None or not symbols:
        return {}
    from tickersentinel.web.macro_data import indicators, sparkline

    now = ctx.clock()
    proxies = {i.symbol: i.proxy for i in indicators(ctx.settings, holdings=False) if i.proxy}

    def work() -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for sym in dict.fromkeys(s for s in symbols if s):
            try:
                sp = sparkline(store, sym, now, proxy=proxies.get(sym))
            except Exception as exc:  # noqa: BLE001 - 单个代码读不通：这一行不画
                log.info("小走势线 %s 读库失败：%s", sym, exc)
                continue
            if sp is not None:
                out[sym] = sp
        return out

    try:
        return await asyncio.to_thread(work)
    except Exception as exc:  # noqa: BLE001
        log.warning("小走势线读库失败：%s", exc)
        return {}


def _short(text: str, n: int = 120) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


# ───────── 今明日程 ─────────

KIND_BADGES = {"earnings": ("财报", "accent"), "insider_sale": ("拟减持", "amber"), "lockup_expiry": ("解禁", "amber"),
               "custom": ("日程", "teal")}


@dataclass
class EventRow:
    event: Event
    time_text: str
    tz_text: str
    badge: str
    badge_cls: str
    holding: bool
    sort_key: tuple = field(default=())


def event_rows(ctx) -> dict[str, Any]:
    from tickersentinel.events.digest import local_day
    from tickersentinel.events.hub import event_in_scope
    from tickersentinel.sources.events_macro import MACRO_GROUPS

    tz = ctx.settings.tz
    today = ctx.clock().astimezone(tz).date()
    tomorrow = today + timedelta(days=1)
    try:
        if ctx.events is not None:
            events = ctx.events.upcoming(today - timedelta(days=1), tomorrow + timedelta(days=1))
        else:
            events = [e for e in ctx.storage.events_between(today - timedelta(days=1), tomorrow + timedelta(days=1))
                      if event_in_scope(ctx.settings, e)]
    except Exception as exc:  # noqa: BLE001
        log.warning("读取事件日历失败：%s", exc)
        return {"error": _short(str(exc)), "today": [], "tomorrow": [], "today_date": today, "tomorrow_date": tomorrow}
    holdings = set(ctx.settings.universe.symbols)
    days: dict[date, list[EventRow]] = {today: [], tomorrow: []}
    for e in events:
        try:
            d = local_day(e, tz)
        except Exception:  # noqa: BLE001
            continue
        if d not in days:
            continue
        if e.kind == "macro":
            badge, cls = MACRO_GROUPS.get(e.symbol, "宏观"), "purple"
        else:
            badge, cls = KIND_BADGES.get(e.kind, ("日程", "teal"))
        if e.at is not None:
            loc = e.at.astimezone(tz)
            text, tzt, key = loc.strftime("%H:%M"), loc.tzname() or "", (1, loc.timestamp())
        else:
            text, tzt, key = (e.time_hint or "全天"), "", (0, 0.0)
        days[d].append(EventRow(e, text, tzt, badge, cls, e.kind != "macro" and e.symbol in holdings, key))
    for rows in days.values():
        rows.sort(key=lambda r: (r.sort_key, r.event.title))
    return {"error": None, "today": days[today], "tomorrow": days[tomorrow], "today_date": today,
            "tomorrow_date": tomorrow}


def safe_url(url: str | None) -> str | None:
    return url if url and url.startswith(("http://", "https://")) else None


# ───────── 今日异动 ─────────


@dataclass
class SymbolDay:
    symbol: str
    count: int = 0
    best_move: float | None = None  # 幅度最大的一次（标题触发的幅度；旧记录用 move_pct）
    best_unit: str = "%"
    last_at: datetime | None = None
    last_direction: int = 0
    ups: int = 0
    downs: int = 0


def headline_move(row: dict) -> tuple[float | None, str]:
    """标题触发（按规则优先级）的幅度；有起点时是“个点”。读不通时退回 move_pct 列。"""
    try:
        trs = json.loads(row.get("triggers_json") or "[]")
        trs = [t for t in trs if isinstance(t, dict) and "move_pct" in t]
        if trs:
            t = sorted(trs, key=lambda t: RULE_PRIORITY.get(str(t.get("rule")), 9))[0]
            move = float(t["move_pct"])
            if t.get("rule") in ("rapid", "day") and t.get("anchor_pct") is not None:
                return move, " 个点"
            return move, "%"
    except (TypeError, ValueError):
        pass
    try:
        return (float(row["move_pct"]), "%") if row.get("move_pct") is not None else (None, "%")
    except (TypeError, ValueError):
        return None, "%"


def today_alerts(ctx) -> dict[str, Any]:
    tz = ctx.settings.tz
    now = ctx.clock()
    local_today = now.astimezone(tz).date()
    start = datetime.combine(local_today, time(0), tzinfo=tz).astimezone(UTC)
    end = datetime.combine(local_today + timedelta(days=1), time(0), tzinfo=tz).astimezone(UTC)
    try:
        rows = ctx.storage.alert_rows_between(start, end)
    except Exception as exc:  # noqa: BLE001
        log.warning("读取今日异动失败：%s", exc)
        return {"error": _short(str(exc)), "symbols": [], "total": 0, "day": local_today.strftime("%m-%d")}
    by: dict[str, SymbolDay] = {}
    for r in rows:
        sym = str(r.get("symbol") or "")
        if not sym:
            continue
        s = by.setdefault(sym, SymbolDay(sym))
        s.count += 1
        try:
            d = int(r.get("direction") or 0)
        except (TypeError, ValueError):
            d = 0
        s.ups += d > 0
        s.downs += d < 0
        move, unit = headline_move(r)
        if move is not None and (s.best_move is None or abs(move) > abs(s.best_move)):
            s.best_move, s.best_unit = move, unit
        at = to_dt(r.get("ts"))
        if at is not None and (s.last_at is None or at >= s.last_at):
            s.last_at, s.last_direction = at, d
    syms = sorted(by.values(), key=lambda s: (s.last_at or datetime.min.replace(tzinfo=UTC)), reverse=True)
    return {"error": None, "symbols": syms, "total": sum(s.count for s in syms), "day": local_today.strftime("%m-%d")}
