"""总览页（/）：左边“对话 / 消息流”两个选项卡（消息流和飞书里收到的一样，实时推送），右边宏观看板、今明日程、今日异动。
运行状态只在顶栏小标（partials/status_dot.html，factory.py 的 /partials/status-dot）；网页提问的接口在 pages/chat.py。

路由：
- GET /                          页面（消息流第一页、日程、今日异动直接渲染；宏观看板随后用 htmx 取）
- GET /partials/overview/macro   宏观看板（每 60 秒刷新；取不到时说明原因，仍是 200）
- GET /partials/overview/events  今明日程
- GET /partials/overview/alerts  今日异动
- GET /partials/overview/feed    往前翻一页消息流（before_id 或 before_ts）
- GET /api/feed                  同上，JSON
- GET /api/stream?after=<id>     SSE：新消息（每 POLL_SECONDS 秒查一次库，没有新消息时不做别的事）
"""

from __future__ import annotations

import asyncio
import time as _time
from datetime import datetime
from typing import Any, AsyncIterator

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from tickersentinel.web import overview_feed as feed
from tickersentinel.web import overview_panels as panels
from tickersentinel.web.common import render
from tickersentinel.web.context import MODE_NAMES, WebContext

router = APIRouter()

POLL_SECONDS = 2.0  # SSE 查库的间隔（测试里调小）
PING_SECONDS = 15.0  # 没有新消息时多久发一次注释行，保持连接
BATCH = 50


def _ctx(request: Request) -> WebContext:
    return request.app.state.ctx


def when_fn(ctx: WebContext):
    """卡片上的时间：今天只写时分，其他日子带上月日。"""
    tz = ctx.settings.tz
    today = ctx.clock().astimezone(tz).date()

    def when(ts: datetime | None) -> str:
        if ts is None:
            return "—"
        loc = ts.astimezone(tz)
        return loc.strftime("%H:%M") if loc.date() == today else loc.strftime("%m-%d %H:%M")

    return when


def _base(ctx: WebContext) -> dict[str, Any]:
    return {"settings": ctx.settings, "u": ctx.settings.universe, "token": ctx.token, "mode": ctx.mode,
            "mode_name": MODE_NAMES.get(ctx.mode, ctx.mode), "readonly": ctx.readonly,
            "up_color": ctx.settings.web.up_color, "nav": "overview", "now": ctx.clock(), "when": when_fn(ctx)}


def fragment(app, name: str, **data: Any) -> str:
    """在请求之外渲染一个片段（SSE 用），和 render() 用同一套变量。"""
    ctx: WebContext = app.state.ctx
    tpl = app.state.templates.env.get_template(name)
    return tpl.render({**_base(ctx), **data})


def _int(v: Any) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


@router.get("/")
async def page(request: Request):
    ctx = _ctx(request)
    first = feed.feed_page(ctx)
    return render(request, "overview/index.html", nav="overview", page=first, after=ctx.storage.last_message_id(),
                  when=when_fn(ctx), ev=panels.event_rows(ctx),
                  ta=panels.today_alerts(ctx), safe_url=panels.safe_url)


@router.get("/partials/overview/macro")
async def macro(request: Request):
    ctx = _ctx(request)
    return render(request, "overview/_macro.html", m=await panels.macro_data(ctx))


@router.get("/partials/overview/events")
async def events(request: Request):
    ctx = _ctx(request)
    return render(request, "overview/_events.html", ev=panels.event_rows(ctx), safe_url=panels.safe_url)


@router.get("/partials/overview/alerts")
async def alerts_today(request: Request):
    ctx = _ctx(request)
    return render(request, "overview/_today.html", ta=panels.today_alerts(ctx))


@router.get("/partials/overview/feed")
async def feed_more(request: Request, before_id: str | None = None, before_ts: str | None = None):
    ctx = _ctx(request)
    pg = feed.feed_page(ctx, before_id=_int(before_id), before_ts=before_ts or None)
    return render(request, "overview/_feed.html", page=pg, when=when_fn(ctx), more=True)


@router.get("/api/feed")
async def feed_json(request: Request, before_id: str | None = None, before_ts: str | None = None,
                    limit: str | None = Query(default=None)):
    ctx = _ctx(request)
    n = _int(limit) or feed.PAGE_SIZE
    pg = feed.feed_page(ctx, before_id=_int(before_id), before_ts=before_ts or None, limit=n)
    return {"items": [it.to_dict() for it in pg.items], "count": pg.raw_count,
            "next_before_id": pg.next_before_id, "next_before_ts": pg.next_before_ts}


def message_payload(app, row: dict) -> dict[str, Any]:
    """一条新消息的 SSE 内容：卡片（独立显示）和回复形式（挂在异动下面）两种 HTML，前端按页面上有没有那条异动来选。"""
    ctx: WebContext = app.state.ctx
    item = feed.item_from_message(ctx.storage, row)
    return {
        "id": item.id, "kind": item.kind, "reply_to": item.reply_to, "message_id": item.message_id,
        "alert_ids": item.alert_ids,
        "html": fragment(app, "overview/_one.html", it=item, nested=False),
        "thread_html": fragment(app, "overview/_one.html", it=item, nested=True),
    }


async def stream_events(app, after: int, is_disconnected, *, poll: float | None = None,
                        ping: float | None = None) -> AsyncIterator[str]:
    """SSE 生成器：id 大于 after 的新消息逐条发出；空闲时每 ping 秒发一行注释。"""
    ctx: WebContext = app.state.ctx
    poll = POLL_SECONDS if poll is None else poll
    ping = PING_SECONDS if ping is None else ping
    last = after
    yield "retry: 3000\n: connected\n\n"
    idle_since = _time.monotonic()
    while True:
        if await is_disconnected():
            break
        try:
            rows = ctx.storage.messages_after(last, BATCH)
        except Exception:  # noqa: BLE001 - 数据库正忙等：下一轮再查
            rows = []
        for row in rows:
            try:
                payload = message_payload(app, row)
            except Exception:  # noqa: BLE001 - 一条排版出错不影响后面的
                payload = {"id": row.get("id"), "kind": row.get("kind"), "reply_to": None, "alert_ids": [],
                           "html": "", "thread_html": ""}
            last = max(last, int(row["id"]))
            yield feed.sse_event(int(row["id"]), payload)
        if rows:
            idle_since = _time.monotonic()
        elif _time.monotonic() - idle_since >= ping:
            idle_since = _time.monotonic()
            yield ": ping\n\n"
        await asyncio.sleep(poll)


@router.get("/api/stream")
async def stream(request: Request, after: str | None = None):
    ctx = _ctx(request)
    start = _int(after)
    last_seen = _int(request.headers.get("last-event-id"))  # EventSource 重连时自动带上
    if start is None:
        start = ctx.storage.last_message_id()
    if last_seen is not None:
        start = max(start, last_seen)
    gen = stream_events(request.app, start, request.is_disconnected)
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

