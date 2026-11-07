"""事件日历页：月历 / 列表两种视图，按宏观 / 公司 / 持仓筛选；点一条事件弹出详情（htmx 片段）。

- GET /calendar?view=month|list&month=YYYY-MM&days=14|30|60|100&cat=all|macro|company|holdings
- GET /partials/calendar/event?key=…  事件详情（不认识的 key 返回 404 片段）
- GET /api/events?start=YYYY-MM-DD&end=YYYY-MM-DD&cat=…  简化的事件列表（JSON）
数据整理在 web/calendar_view.py。参数写错一律退回默认值，不报错。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from tickersentinel.feishu.calendar_sync import KIND_TAGS
from tickersentinel.web import calendar_view as cv
from tickersentinel.web.common import render

router = APIRouter()
log = logging.getLogger(__name__)

STALE_HOURS = 36  # 日历超过这么久没刷新就提醒（tsen run 每天刷新两次）


def _today(ctx) -> date:
    return ctx.clock().astimezone(ctx.settings.tz).date()


@router.get("/calendar")
async def page(request: Request, view: str | None = None, month: str | None = None, days: str | None = None,
               cat: str | None = None):
    ctx = request.app.state.ctx
    settings = ctx.settings
    today = _today(ctx)
    view_ = cv.parse_view(view)
    cat_ = cv.parse_cat(cat)
    days_ = cv.parse_days(days)
    month_, month_ok = cv.parse_month(month, today)
    horizon = today + timedelta(days=settings.events.lookahead_days)

    data: dict[str, Any] = {}
    if view_ == "month":
        start, end = cv.grid_range(month_)
        everything = cv.load_events(ctx, start, end)
        events = [c for c in everything if cv.matches(c, cat_)]
        in_month = [c for c in events if c.local_day.year == month_.year and c.local_day.month == month_.month]
        data.update(
            weeks=cv.month_grid(month_, today, events, cv.holiday_marks(everything)), month=month_,
            prev_month=cv.add_months(month_, -1), next_month=cv.add_months(month_, 1),
            is_this_month=month_ == today.replace(day=1), month_ok=month_ok,
            counts={"total": len(in_month), "macro": sum(c.category == "macro" for c in in_month),
                    "company": sum(c.category == "company" for c in in_month)},
        )
        visible_start = start
        visible_end = end
    else:
        end = today + timedelta(days=days_ - 1)  # 今天算第 1 天：“未来 14 天” = 今天起 14 个日历日
        events = cv.load_events(ctx, today, end, cat_)
        data.update(groups=cv.group_by_day(events, today), list_end=end,
                    counts={"total": len(events), "macro": sum(c.category == "macro" for c in events),
                            "company": sum(c.category == "company" for c in events)})
        visible_start = today
        visible_end = end

    refreshed = cv.last_refresh(ctx)
    stale = refreshed is not None and (ctx.clock() - refreshed).total_seconds() > STALE_HOURS * 3600
    # 这一屏没有事件时，区分“筛选 / 月份里没有”和“日历根本还没收集过”
    any_events = bool(events) or bool(ctx.storage.events_between(date(1970, 1, 1), date(2100, 1, 1)))
    return render(
        request, "calendar/index.html", nav="calendar",
        view=view_, cat=cat_, days=days_, list_days=cv.LIST_DAYS, categories=cv.CATEGORIES, views=cv.VIEWS,
        today=today, horizon=horizon, beyond=visible_end > horizon, refreshed=refreshed, stale=stale,
        any_events=any_events, legend=cv.LEGEND, weekdays=cv.WEEKDAYS, chips_per_day=cv.CHIPS_PER_DAY,
        tzname=str(settings.tz), tz_label=cv.tz_label(settings.tz, visible_start, visible_end), lookahead=settings.events.lookahead_days, **data,
    )


@router.get("/partials/calendar/event")
async def event_detail(request: Request, key: str = ""):
    ctx = request.app.state.ctx
    e = cv.find_event(ctx.storage, key)
    if e is None:
        return render(request, "calendar/event_missing.html", status_code=404, key=key)
    try:
        c = cv.classify(e, ctx.settings, ctx.settings.tz)
        rows = cv.detail_rows(c, ctx.settings)
    except Exception:  # 坏数据：退回“找不到”片段，不报 500
        log.exception("事件详情整理失败：%s", key)
        return render(request, "calendar/event_missing.html", status_code=404, key=key, broken=True)
    return render(request, "calendar/event.html", c=c, rows=rows,
                  kind_tag=KIND_TAGS.get(e.kind, "日程"), url=cv.safe_url(e.url),
                  relative=cv.relative_label(c.local_day, _today(ctx)),
                  in_scope=cv.event_in_scope(ctx.settings, e))


def _parse_date(raw: str | None) -> date | None:
    """空 → None；格式不对抛 ValueError。"""
    return date.fromisoformat(raw.strip()) if raw and raw.strip() else None


@router.get("/api/events")
async def api_events(request: Request, start: str | None = None, end: str | None = None, cat: str | None = None):
    ctx = request.app.state.ctx
    today = _today(ctx)
    try:
        s = _parse_date(start) or today
        e = _parse_date(end) or (s + timedelta(days=cv.DEFAULT_LIST_DAYS - 1))  # 默认 start 起 30 天（含两端）
    except ValueError:
        return JSONResponse({"detail": "日期格式应为 YYYY-MM-DD"}, status_code=400)
    if e < s:
        return JSONResponse({"detail": "end 不能早于 start"}, status_code=400)
    if (e - s).days > cv.MAX_API_DAYS:
        return JSONResponse({"detail": f"一次最多查 {cv.MAX_API_DAYS} 天"}, status_code=400)
    cat_ = cv.parse_cat(cat)
    events = cv.load_events(ctx, s, e, cat_)
    tz = ctx.settings.tz
    refreshed = cv.last_refresh(ctx)
    return {"start": s.isoformat(), "end": e.isoformat(), "cat": cat_, "timezone": str(tz),
            "last_refresh": refreshed.isoformat() if refreshed else None,
            "horizon": (today + timedelta(days=ctx.settings.events.lookahead_days)).isoformat(),
            "events": [c.to_dict(tz) for c in events]}
