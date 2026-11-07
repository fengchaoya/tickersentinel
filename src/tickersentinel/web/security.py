"""只限本机访问的两道检查（纯 ASGI 中间件，不缓冲响应，SSE 照常流式输出）。

1. Host 白名单：只接受 127.0.0.1 / localhost / [::1]。防 DNS 重绑定——恶意网站把自己的域名解析到 127.0.0.1 后，
   浏览器发来的请求 Host 仍是那个域名，在这里被拒（400）。
2. 写请求（POST / PUT / PATCH / DELETE）必须带请求头 X-Tsen-Token，值等于页面 meta 里的 token（每个进程随机生成）。
   其他网站读不到这个值（同源策略），也没法跨域加自定义请求头（会先发预检，我们不开 CORS），所以伪造不了；
   带了 Origin 的再核对一次是本机。
"""

from __future__ import annotations

import hmac
from typing import Any, Awaitable, Callable

ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
TOKEN_HEADER = "x-tsen-token"

Scope = dict[str, Any]
ASGIApp = Callable[[Scope, Callable, Callable], Awaitable[None]]


def host_of(value: str) -> str:
    """'127.0.0.1:8765' → '127.0.0.1'；'[::1]:8765' → '::1'；'http://localhost:8765' → 'localhost'。"""
    v = value.strip().lower()
    if "://" in v:
        v = v.split("://", 1)[1].split("/", 1)[0]
    if v.startswith("["):
        return v[1:].split("]", 1)[0]
    return v.rsplit(":", 1)[0] if v.count(":") == 1 else v


class LocalOnlyMiddleware:
    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Callable, send: Callable) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if host_of(headers.get("host", "")) not in ALLOWED_HOSTS:
            await _reply(send, 400, "只接受本机访问（Host 不是 127.0.0.1 / localhost）")
            return
        if scope["type"] == "http" and scope.get("method", "GET").upper() in UNSAFE_METHODS:
            origin = headers.get("origin")
            if origin and origin != "null" and host_of(origin) not in ALLOWED_HOSTS:
                await _reply(send, 403, "拒绝来自其他网站的请求")
                return
            if not hmac.compare_digest(headers.get(TOKEN_HEADER, ""), self.token):
                await _reply(send, 403, "缺少或错误的 X-Tsen-Token（请刷新页面再试）")
                return
        await self.app(scope, receive, send)


async def _reply(send: Callable, status: int, text: str) -> None:
    body = text.encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
