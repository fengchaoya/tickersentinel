"""飞书推送：用假的 FeishuChannel 验证发送格式、回复挂接、只处理指定会话、消息交回主循环处理。不联网。"""

import asyncio
import threading
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("lark_oapi.channel")

from tickersentinel.agents.chat import IncomingMessage  # noqa: E402
from tickersentinel.models import Message  # noqa: E402
from tickersentinel.notify.feishu import FeishuNotifier  # noqa: E402


class FakeChannel:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []
        self.handlers = {}
        self.started = False

    async def send(self, to, message, opts=None):
        self.sent.append((to, message, opts))
        if not self.ok:
            return NS(success=False, message_id=None, error="230001 invalid message content")
        return NS(success=True, message_id=f"om_{len(self.sent)}", error=None)

    def on(self, name, handler):
        self.handlers[name] = handler

    async def start_background(self, timeout=30):  # 不应再被调用（见 start_long_connection）
        raise AssertionError("start_background 会撞上 SDK 的事件循环缺陷")

    @property
    def ws_client(self):
        return NS(_conn=object()) if self.started else None

    def start(self):  # 由 start_long_connection 在专用线程里调用；真实 SDK 会阻塞，这里立即返回
        self.started = True

    async def disconnect(self):
        self.started = False


def notifier(channel):
    return FeishuNotifier("cli_x", "secret", "oc_me", channel=channel)


async def test_send_post_with_title_and_reply():
    ch = FakeChannel()
    n = notifier(ch)
    mid = await n.send(Message(title="☀️ 盘前简报", body="**总览**\n- 要点 [Reuters](https://r.example)"))
    await n.send(Message(title="🧭 归因", body="原因", reply_to=mid))
    (to1, m1, o1), (to2, m2, o2) = ch.sent
    assert to1 == to2 == "oc_me" and mid == "om_1"
    assert m1.kind == "post" and m1.title == "☀️ 盘前简报" and m1.markdown.startswith("**总览**") and o1 is None
    assert o2 == {"reply_to": "om_1"}


async def test_send_failure_raises():
    with pytest.raises(RuntimeError, match="飞书发送失败"):
        await notifier(FakeChannel(ok=False)).send(Message(title="t", body="b"))
    with pytest.raises(ValueError, match="FEISHU_CHAT_ID"):
        FeishuNotifier("cli_x", "secret", None, channel=FakeChannel())


async def test_inbound_only_from_own_chat_and_handled_on_main_loop():
    ch = FakeChannel()
    n = notifier(ch)
    seen = []
    main_thread = threading.get_ident()

    async def on_message(msg: IncomingMessage):
        seen.append((msg.text, msg.chat_id, msg.reply_to_text, threading.get_ident() == main_thread))
        return "收到"

    await n.start(on_message)
    assert ch.started and "message" in ch.handlers
    handle = ch.handlers["message"]

    def inbound(chat_id, text, reply=None):
        return NS(chat_id=chat_id, content_text=text, message_id="om_in", reply=reply)

    # SDK 在自己的后台线程 / 事件循环里调用回调：模拟这一点
    def run_in_sdk_thread(m):
        asyncio.run(handle(m))

    await asyncio.to_thread(run_in_sdk_thread, inbound("oc_stranger", "帮我查一下"))
    assert seen == [] and ch.sent == []  # 别的会话一律忽略
    await asyncio.to_thread(run_in_sdk_thread, inbound("oc_me", "MU 为什么跌", NS(text="MU 急跌 -3%")))
    assert seen == [("MU 为什么跌", "oc_me", "MU 急跌 -3%", True)]  # 在主循环里处理
    to, msg, opts = ch.sent[-1]
    assert to == "oc_me" and msg.markdown == "收到" and opts == {"reply_to": "om_in"}
    await n.stop()
    assert not ch.started


class _FakeWS:
    """假的 WebSocket 连接：recv 一直等，直到被关闭。"""

    def __init__(self):
        self.closed = asyncio.Event()

    async def recv(self):
        await self.closed.wait()
        raise ConnectionError("closed")

    async def close(self):
        self.closed.set()


async def test_real_sdk_long_connection_survives_import_inside_running_loop(monkeypatch):
    # 复现 2026-10-21 的实际报错：lark_oapi.ws.client 在主循环运行中被导入，模块级 loop 指向正在运行的主循环
    import lark_oapi.ws.client as ws_module
    from lark_oapi.channel import FeishuChannel

    from tickersentinel.notify.feishu import start_long_connection, stop_long_connection

    async def fake_connect(url, **kw):
        return _FakeWS()

    monkeypatch.setattr(ws_module.Client, "_get_conn_url", lambda self: "wss://x.example/ws?device_id=1&service_id=2")
    monkeypatch.setattr(ws_module.websockets, "connect", fake_connect)
    monkeypatch.setattr(FeishuChannel, "_fetch_bot_identity_sync", lambda self: None)
    # SDK 自带的 start_background() 在这种状态下会报 “This event loop is already running”（用户实测）；
    # 这里不再演示它：它失败后的清理会把正在运行的主循环也停掉
    monkeypatch.setattr(ws_module, "loop", asyncio.get_running_loop())
    ch = FeishuChannel(app_id="cli_x", app_secret="s")
    await start_long_connection(ch, timeout=5)  # 我们的方式：专用循环 + 专用线程
    assert ch.ws_client._conn is not None and ws_module.loop is not asyncio.get_running_loop()
    ws_loop = ws_module.loop
    await stop_long_connection(ch)
    assert not ch._tsen_ws_thread.is_alive() and ws_loop.is_closed()  # 线程收尾、循环关闭，退出时不再报错


async def test_ack_is_sent_before_the_answer():
    ch = FakeChannel()
    n = FeishuNotifier("cli_x", "secret", "oc_me", channel=ch, ack="收到，思考中…")
    order = []

    async def on_message(msg):
        order.append(("answer-start", len(ch.sent)))
        return "答案"

    await n.start(on_message)
    m = NS(chat_id="oc_me", content_text="MU 怎么样", message_id="om_in", reply=None)
    await asyncio.to_thread(lambda: asyncio.run(ch.handlers["message"](m)))
    assert [x[1].markdown for x in ch.sent] == ["收到，思考中…", "答案"]
    assert order == [("answer-start", 1)] and all(x[2] == {"reply_to": "om_in"} for x in ch.sent)
