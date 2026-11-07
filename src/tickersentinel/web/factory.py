"""create_app(ctx)：组装网页。各页面的路由在 pages/ 下，各自一个 APIRouter。"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from tickersentinel.web.common import STATIC_DIR, build_templates, render, segments_now
from tickersentinel.web.context import MODE_NAMES, WebContext
from tickersentinel.web.security import LocalOnlyMiddleware

APP_ID = "tickersentinel"  # /api/ping 的标识：tsen web 据此判断端口上跑的是不是自己


def create_app(ctx: WebContext) -> FastAPI:
    app = FastAPI(title="tickersentinel", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
    app.state.templates = build_templates(ctx)
    app.add_middleware(LocalOnlyMiddleware, token=ctx.token)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/api/ping")
    async def ping() -> dict[str, Any]:
        return {"app": APP_ID, "mode": ctx.mode}

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        rt = ctx.runtime()
        return {"mode": ctx.mode, "mode_name": MODE_NAMES.get(ctx.mode, ctx.mode), "running": rt["running"],
                "age_seconds": rt["age_seconds"], "heartbeat": rt["heartbeat"],
                "segments": [{**s, "until": s["until"].isoformat() if s["until"] else None} for s in segments_now(ctx)]}

    @app.get("/partials/status-dot")
    async def status_dot(request: Request):
        return render(request, "partials/status_dot.html", rt=ctx.runtime())

    from tickersentinel.web.pages import alerts, calendar, chat, macro, overview

    app.include_router(overview.router)
    app.include_router(alerts.router)
    app.include_router(calendar.router)
    app.include_router(macro.router)
    app.include_router(chat.router)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith("/api/") or request.url.path.startswith("/static/"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return render(request, "error.html", status_code=exc.status_code, code=exc.status_code,
                      message="页面不存在" if exc.status_code == 404 else str(exc.detail))

    return app
