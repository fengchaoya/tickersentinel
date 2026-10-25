"""飞书推送（双向）：基于官方 SDK lark-oapi 1.7+ 的 FeishuChannel。安装：uv pip install -e ".[feishu]"。

- 发送：机器人以应用身份把消息发到 FEISHU_CHAT_ID（和机器人的单聊或某个群，形如 oc_xxx）。
  消息用富文本 post（标题 + Markdown 正文）；SDK 负责把 Markdown 转成飞书格式、超长自动拆成多条、
  可重试的错误（限流、网络）自动重试。归因用 reply_to 挂在对应的异动消息下面。
  发送只走 HTTP 接口，不依赖长连接，所以一次性命令（tsen brief --send）也能推送。
- 接收：长连接（WebSocket，不需要公网地址）。只处理来自 FEISHU_CHAT_ID 的消息，其他会话一律忽略，
  避免别人把你的机器人拉进群后消耗你的大模型额度。
- 线程：FeishuChannel 在自己的后台线程和事件循环里收消息；收到后把处理逻辑交回主程序的事件循环执行
  （主程序的大模型、数据库客户端都绑定在主循环上），回复再由 SDK 发出（它每次请求都新建 HTTP 客户端，跨循环安全）。

飞书后台的设置步骤见 README「推送：飞书」；`tsen feishu setup` 用来建立长连接并查出 chat_id，`tsen feishu test` 发测试消息。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any

from tickersentinel.agents.chat import IncomingMessage
from tickersentinel.models import Message
from tickersentinel.notify.base import InboundHandler, Notifier

log = logging.getLogger(__name__)

DOMAINS = {"feishu": "https://open.feishu.cn", "lark": "https://open.larksuite.com"}


def make_channel(app_id: str, app_secret: str, domain: str = "feishu") -> Any:
    """创建 FeishuChannel。SDK 是可选依赖，用到时才导入。"""
    try:
        from lark_oapi.channel import FeishuChannel
        from lark_oapi.core.enum import LogLevel
    except ImportError as exc:  # pragma: no cover - 取决于是否安装了可选依赖
        raise RuntimeError('使用飞书推送需要安装 SDK：uv pip install -e ".[feishu]"') from exc
    return FeishuChannel(app_id=app_id, app_secret=app_secret, domain=DOMAINS[domain], log_level=LogLevel.WARNING)


async def start_long_connection(channel: Any, timeout: float = 30.0) -> None:
    """在专用线程里建立长连接，连上后返回；失败（凭证错误、网络不通、超时）抛 RuntimeError。

    不用 SDK 自带的 start_background()：长连接模块 lark_oapi.ws.client 在**导入时**抓取了当时的事件循环，
    之后用它 run_until_complete。我们是在主循环运行中导入的，它抓到的就是正在运行的主循环，
    于是报 “This event loop is already running”（2026-10-21 实测，lark-oapi 1.7.5）。
    这里给它换上一个专用循环，并在同一个线程里把它设为当前循环（SDK 内部缓存也会用它）；
    SDK 的 stop() 关闭长连接时用的也是这个模块级变量，所以 channel.disconnect() 照常有效。
    """
    import lark_oapi.ws.client as ws_module

    loop = asyncio.new_event_loop()
    ws_module.loop = loop
    errors: list[BaseException] = []

    def run() -> None:
        asyncio.set_event_loop(loop)
        try:
            channel.start()  # 阻塞：连上后一直收消息，直到 channel.disconnect() 把循环停掉
        except BaseException as exc:  # noqa: BLE001 - 包括停止时的 “Event loop stopped”
            if not getattr(channel, "_stop_requested", False):
                errors.append(exc)
        finally:
            _drain(loop)

    thread = threading.Thread(target=run, name="feishu-ws", daemon=True)
    thread.start()
    channel._tsen_ws_thread = thread  # 关闭时等它收尾，见 stop_long_connection
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ws = getattr(channel, "ws_client", None)
        if ws is not None and getattr(ws, "_conn", None) is not None:  # 与 SDK 自己判断“已连接”的方式相同
            return
        if errors or not thread.is_alive():
            break
        await asyncio.sleep(0.1)
    if errors:
        raise RuntimeError(f"{type(errors[0]).__name__}: {errors[0]}") from errors[0]
    raise RuntimeError(f"{timeout:.0f} 秒内没有连上飞书")


def _drain(loop: asyncio.AbstractEventLoop) -> None:
    """长连接停止后，取消 SDK 留在专用循环上的任务（心跳、收消息、缓存清理）再关闭循环，
    否则退出时会打印一串 “Task was destroyed but it is pending!”。"""
    try:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
    except Exception:  # pragma: no cover - 收尾失败不影响退出
        log.debug("清理飞书长连接的事件循环出错", exc_info=True)


async def stop_long_connection(channel: Any, timeout: float = 5.0) -> None:
    """关闭长连接并等专用线程收尾。SDK 会把正常关闭（1000 OK）也记成 ERROR，关闭期间把它的日志调到最高级别。"""
    logging.getLogger("Lark").setLevel(logging.CRITICAL)
    await channel.disconnect()
    thread = getattr(channel, "_tsen_ws_thread", None)
    if thread is not None:
        await asyncio.to_thread(thread.join, timeout)


def post(markdown: str, title: str = "") -> Any:
    from lark_oapi.channel.types import OutboundPost

    return OutboundPost(markdown=markdown, title=title or None)


class FeishuNotifier(Notifier):
    name = "feishu"

    def __init__(self, app_id: str | None, app_secret: str | None, chat_id: str | None, *,
                 domain: str = "feishu", channel: Any | None = None, ack: str = ""):
        if not app_id or not app_secret or not chat_id:
            raise ValueError("使用飞书推送需要在 .env 中填写 FEISHU_APP_ID、FEISHU_APP_SECRET 和 FEISHU_CHAT_ID"
                             "（chat_id 可以用 tsen feishu setup 查出来）")
        self.chat_id = chat_id
        self.ack = ack  # 收到消息先回的一句话（大模型回答要几十秒）；空字符串表示不回
        self._channel = channel if channel is not None else make_channel(app_id, app_secret, domain)
        self._on_message: InboundHandler | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._listening = False

    async def send(self, msg: Message) -> str | None:
        opts = {"reply_to": msg.reply_to} if msg.reply_to else None
        res = await self._channel.send(self.chat_id, post(msg.body, msg.title), opts)
        if not res.success:
            raise RuntimeError(f"飞书发送失败：{res.error}")
        return res.message_id

    async def start(self, on_message: InboundHandler | None = None) -> None:
        """建立长连接接收消息（常驻运行时调用）。不需要接收时可以不调用，发送照样可用。"""
        if on_message is None:
            return
        self._on_message = on_message
        self._loop = asyncio.get_running_loop()
        self._channel.on("message", self._handle)
        await start_long_connection(self._channel)
        self._listening = True
        log.info("飞书长连接已建立，只接收会话 %s 的消息", self.chat_id)

    async def _handle(self, m: Any) -> None:
        """在 SDK 的后台线程里被调用。"""
        if m.chat_id != self.chat_id:
            log.info("忽略来自其他会话的飞书消息（chat_id=%s）", m.chat_id)
            return
        if self._on_message is None or self._loop is None:
            return
        incoming = IncomingMessage(text=m.content_text, chat_id=m.chat_id,
                                   reply_to_text=m.reply.text if m.reply else None)
        if self.ack and incoming.text.strip():
            try:
                await self._channel.send(self.chat_id, post(self.ack), {"reply_to": m.message_id})
            except Exception:  # 先行提示发不出去不影响正式回答
                log.warning("飞书先行提示发送失败", exc_info=True)
        future = asyncio.run_coroutine_threadsafe(self._on_message(incoming), self._loop)
        try:
            answer = await asyncio.wrap_future(future)
        except Exception:
            log.exception("处理飞书消息失败")
            answer = "处理这条消息时出错了，详情见程序日志。"
        if answer:
            await self._channel.send(self.chat_id, post(answer), {"reply_to": m.message_id})

    async def bot_name(self) -> str | None:
        """机器人的名字（tsen check 用来确认 App ID / Secret 正确、机器人能力已开通）。"""
        identity = await self._channel.resolve_bot_identity()
        return getattr(identity, "name", None) if identity else None

    async def stop(self) -> None:
        if self._listening:
            self._listening = False
            await stop_long_connection(self._channel)
