"""网页服务器的两种运行方式。

- 嵌入（tsen run）：在 App 的事件循环里跑 uvicorn。信号仍由 App 处理（uvicorn 默认会接管 Ctrl+C，这里关掉）；
  端口被占用、依赖没装都只返回原因，不影响监控和推送。
- 单独运行（tsen web）：端口上已经是我们自己的网页（/api/ping）就直接打开浏览器；否则起一个不排任务的 App：不检测、
  不跑定时任务、不发定时推送，但用的是正式库——网页里的提问、标注、“现在补抓”照常写入（飞书 /scope 那套改动确认后也照常生效）。
  --preview 用正式库的内存副本（改动不落盘）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
import urllib.request
import webbrowser
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tickersentinel.web.context import WebContext

if TYPE_CHECKING:
    from tickersentinel.config import Settings

log = logging.getLogger(__name__)


def url_for(host: str, port: int) -> str:
    return f"http://{'[::1]' if host == '::1' else host}:{port}"


def port_free(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def probe(host: str, port: int, timeout: float = 1.5) -> dict | None:
    """端口上是不是我们自己的网页：是就返回 /api/ping 的内容。"""
    try:
        with urllib.request.urlopen(url_for(host, port) + "/api/ping", timeout=timeout) as resp:  # noqa: S310 - 本机地址
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    from tickersentinel.web.factory import APP_ID

    return data if isinstance(data, dict) and data.get("app") == APP_ID else None


def _uvicorn_config(app, host: str, port: int):
    import uvicorn

    return uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False, lifespan="off",
                          timeout_graceful_shutdown=3)


def _embedded_server_cls():
    import uvicorn

    class EmbeddedServer(uvicorn.Server):
        """不接管信号：Ctrl+C 由 App.run_forever 处理，退出时调用 stop()。"""

        @contextlib.contextmanager
        def capture_signals(self):  # uvicorn ≥ 0.29
            yield

        def install_signal_handlers(self) -> None:  # 旧版 uvicorn
            pass

    return EmbeddedServer


@dataclass
class WebHandle:
    url: str
    server: object
    task: asyncio.Task

    async def stop(self) -> None:
        self.server.should_exit = True  # type: ignore[attr-defined]
        try:
            await asyncio.wait_for(self.task, 5)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            self.task.cancel()


async def start_embedded(ctx: WebContext) -> WebHandle | str:
    """在当前事件循环里启动网页。成功返回 WebHandle；没启动返回原因（给启动消息用）。"""
    from tickersentinel.web import available, missing_deps

    cfg = ctx.settings.web
    if not cfg.enabled:
        return "关闭（settings.yaml 的 web.enabled）"
    if not available():
        return f"没有启动：缺少依赖 {'、'.join(missing_deps())}（pip install -e \".[web]\"）"
    if not port_free(cfg.host, cfg.port):
        return f"没有启动：端口 {cfg.port} 被占用（改 settings.yaml 的 web.port）"
    from tickersentinel.web.factory import create_app

    server = _embedded_server_cls()(_uvicorn_config(create_app(ctx), cfg.host, cfg.port))

    async def serve() -> None:
        try:
            await server.serve()
        except (SystemExit, OSError) as exc:  # uvicorn 绑定失败时 sys.exit(1)
            log.warning("网页服务器退出：%s", exc)

    task = asyncio.create_task(serve())
    for _ in range(50):  # 最多等 5 秒
        if server.started or task.done():
            break
        await asyncio.sleep(0.1)
    if not server.started:
        task.cancel()
        return "没有启动（见日志）"
    url = url_for(cfg.host, cfg.port)
    log.info("网页：%s", url)
    return WebHandle(url, server, task)


async def run_standalone(settings: "Settings", *, preview: bool = False, port: int | None = None,
                         open_browser: bool = True) -> None:
    """tsen web。"""
    import uvicorn

    from tickersentinel.app import App
    from tickersentinel.web.factory import create_app

    host = settings.web.host
    port = port or settings.web.port
    url = url_for(host, port)
    if not preview and (info := probe(host, port)) is not None:
        print(f"网页已经在运行（{info.get('mode')}）：{url}")
        if open_browser:
            webbrowser.open(url)
        return
    if not port_free(host, port):
        raise SystemExit(f"端口 {port} 被别的程序占用：换一个端口，例如 tsen web --port {port + 1}")
    app = App(settings, dry_run=preview, copy_db=preview)
    try:
        ctx = app.web_context("preview" if preview else "standalone")
        server = uvicorn.Server(_uvicorn_config(create_app(ctx), host, port))
        task = asyncio.create_task(server.serve())
        for _ in range(50):
            if server.started or task.done():
                break
            await asyncio.sleep(0.1)
        if server.started:
            note = ("预览：正式库的内存副本，网页里的改动不会保存" if preview
                    else "单独运行：不检测、不跑定时任务（tsen run 负责）；网页里的提问、标注、补抓照常写入正式库")
            print(f"网页：{url}（{note}；Ctrl+C 退出）")
            if open_browser:
                webbrowser.open(url)
        await task
    finally:
        await app.aclose()
