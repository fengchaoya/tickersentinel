"""网页测试的公共工具：构造 WebContext、离线的 HTTP 客户端（httpx.ASGITransport，不起服务器、不联网）。

用法：
    ctx = make_ctx(settings, storage=st, now=utc(2026, 10, 9, 15))
    async with client(ctx) as c:
        r = await c.get("/")
        r = await c.post("/api/...", headers=write_headers(ctx), json={...})
"""

from __future__ import annotations

import contextlib
from datetime import datetime

import httpx

from tickersentinel.market_calendar import MarketCalendar
from tickersentinel.storage import Storage
from tickersentinel.web.context import WebContext
from tickersentinel.web.factory import create_app

BASE_URL = "http://127.0.0.1:8765"


def make_ctx(settings, *, storage: Storage | None = None, calendar: MarketCalendar | None = None, prices=None,
             events=None, macro=None, mode: str = "standalone", now: datetime | None = None, bars=None,
             collector=None) -> WebContext:
    kw = {}
    if now is not None:
        kw["clock"] = lambda: now
    return WebContext(settings=settings, storage=storage or Storage(":memory:"), calendar=calendar or MarketCalendar(),
                      prices=prices, events=events, macro=macro, mode=mode, token="test-token", bars=bars,
                      collector=collector, **kw)  # type: ignore[arg-type]


@contextlib.asynccontextmanager
async def client(ctx: WebContext, base_url: str = BASE_URL):
    app = create_app(ctx)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url) as c:
        yield c


def write_headers(ctx: WebContext) -> dict[str, str]:
    """写请求要带的头（security.py）。"""
    return {"X-Tsen-Token": ctx.token, "Origin": BASE_URL}
