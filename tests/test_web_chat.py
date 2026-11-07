"""网页提问（总览页左边的“对话”选项卡，web/pages/chat.py）：和飞书同一套 ChatAgent，会话 web；写请求要 token。全部离线。"""

import asyncio

import pytest

pytest.importorskip("fastapi")

from web_support import client, make_ctx, write_headers  # noqa: E402

from tickersentinel.storage import Storage  # noqa: E402


class FakeChat:
    """ChatAgent 的替身：记下问题，回答一段 markdown，并像真的那样把问答存进 chat_messages。"""

    def __init__(self, storage, answer="**MU** 跌了 3%，见 [新闻](https://example.com/a)", delay=0.0, boom=False):
        self.storage, self.answer, self.delay, self.boom = storage, answer, delay, boom
        self.seen = []

    async def handle(self, msg):
        self.seen.append((msg.chat_id, msg.text))
        if self.boom:
            raise RuntimeError("LM Studio 没开")
        await asyncio.sleep(self.delay)
        self.storage.add_chat_message(msg.chat_id, "user", msg.text)
        self.storage.add_chat_message(msg.chat_id, "assistant", self.answer)
        return self.answer


def ctx_with_chat(settings, **kw):
    st = Storage(":memory:")
    ctx = make_ctx(settings, storage=st)
    ctx.chat = FakeChat(st, **kw)
    return ctx


async def test_ask_answers_with_rendered_markdown_and_keeps_history(settings):
    ctx = ctx_with_chat(settings)
    async with client(ctx) as c:
        r = await c.post("/api/chat", json={"text": "MU 今天为什么跌？"}, headers=write_headers(ctx))
        assert r.status_code == 200
        js = r.json()
        assert js["ok"] and "<strong>MU</strong>" in js["answer_html"] and 'href="https://example.com/a"' in js["answer_html"]
        assert ctx.chat.seen == [("web", "MU 今天为什么跌？")]
        log = (await c.get("/partials/chat")).text
    assert "MU 今天为什么跌？" in log and "<strong>MU</strong>" in log and "chat-user" in log and "chat-assistant" in log


async def test_ask_about_a_push_passes_it_as_reply_context(settings):
    """消息流里点“问一下”：那条推送的正文作为 reply_to_text 交给对话（和飞书里回复某条推送一样）。"""
    ctx = ctx_with_chat(settings)
    seen = []
    orig = ctx.chat.handle

    async def handle(msg):
        seen.append(msg.reply_to_text)
        return await orig(msg)

    ctx.chat.handle = handle
    async with client(ctx) as c:
        await c.post("/api/chat", json={"text": "为什么？", "reply_to_text": "【异动】MU 急跌 3%"}, headers=write_headers(ctx))
        await c.post("/api/chat", json={"text": "普通问题"}, headers=write_headers(ctx))
    assert seen == ["【异动】MU 急跌 3%", None]


async def test_overview_has_chat_and_feed_tabs_with_ask_buttons(settings):
    from tickersentinel.models import Message

    ctx = ctx_with_chat(settings)
    ctx.storage.record_message(Message(title="📉 MU 急跌", body="5 分钟跌 3%", kind="alert"), channel="console",
                               message_id="m1")
    async with client(ctx) as c:
        r = (await c.get("/")).text
    assert 'id="tab-btn-chat"' in r and 'id="tab-btn-feed"' in r and 'id="tab-feed"' in r and "hidden" in r
    assert "data-ask" in r and "问一下" in r


async def test_web_shares_the_feishu_conversation(settings):
    """用飞书推送时网页和飞书是同一个会话：飞书里的问答显示在网页上，网页提问也接着同一段上下文。"""
    from pydantic import SecretStr

    settings.notify.provider = "feishu"
    settings.secrets.feishu_chat_id = SecretStr("oc_test")
    ctx = ctx_with_chat(settings)
    ctx.storage.add_chat_message("oc_test", "user", "飞书里问的")
    ctx.storage.add_chat_message("oc_test", "assistant", "飞书里的**回答**")
    async with client(ctx) as c:
        log = (await c.get("/partials/chat")).text
        await c.post("/api/chat", json={"text": "网页问的"}, headers=write_headers(ctx))
    assert "飞书里问的" in log and "<strong>回答</strong>" in log
    assert ctx.chat.seen == [("oc_test", "网页问的")]


async def test_pages_link_static_files_with_a_version(settings):
    """css / js 网址带版本号（文件修改时间）：改了代码浏览器一定取新的，不会用缓存里的旧脚本。"""
    ctx = ctx_with_chat(settings)
    async with client(ctx) as c:
        r = (await c.get("/")).text
    assert "/static/js/overview.js?v=" in r and "/static/css/app.css?v=" in r and "/static/js/app.js?v=" in r


async def test_ask_needs_token_and_text(settings):
    ctx = ctx_with_chat(settings)
    async with client(ctx) as c:
        assert (await c.post("/api/chat", json={"text": "hi"})).status_code == 403  # 没带 token
        r = await c.post("/api/chat", json={"text": "  "}, headers=write_headers(ctx))
        assert r.status_code == 422 and not r.json()["ok"]
        r = await c.post("/api/chat", json={"text": "x" * 2001}, headers=write_headers(ctx))
        assert r.status_code == 422
    assert ctx.chat.seen == []


async def test_ask_errors_and_missing_chat_are_reported(settings, monkeypatch):
    ctx = ctx_with_chat(settings, boom=True)
    async with client(ctx) as c:
        js = (await c.post("/api/chat", json={"text": "hi"}, headers=write_headers(ctx))).json()
    assert not js["ok"] and "LM Studio 没开" in js["error"]

    from tickersentinel.web.pages import chat as chat_page

    monkeypatch.setattr(chat_page, "ANSWER_TIMEOUT", 0.05)
    ctx = ctx_with_chat(settings, delay=5)
    async with client(ctx) as c:
        js = (await c.post("/api/chat", json={"text": "hi"}, headers=write_headers(ctx))).json()
    assert not js["ok"] and "还没有回答" in js["error"]

    ctx = make_ctx(settings)  # 没接上对话
    async with client(ctx) as c:
        assert (await c.post("/api/chat", json={"text": "hi"}, headers=write_headers(ctx))).status_code == 503
        assert "没有接上对话功能" in (await c.get("/partials/chat")).text


async def test_overview_has_chat_box_instead_of_status_card(settings):
    ctx = ctx_with_chat(settings)
    async with client(ctx) as c:
        r = (await c.get("/")).text
    assert 'id="chat-form"' in r and 'id="chat-input"' in r and "ov-status" not in r
    assert "不构成投资建议" not in r  # 页脚已去掉


async def test_status_pill_shows_details_and_collection_errors(settings):
    import json

    from tickersentinel.timeutil import utcnow

    st = Storage(":memory:")
    now = utcnow().isoformat()
    hb = {"at": now, "started_at": now, "last_tick_at": now, "last_tick_ok": True, "tick_failures": 0,
          "bars": {"last_ok": now, "failures": 5, "last_error": "SourceError: Too Many Requests"}}
    st.kv_set("runtime:heartbeat", json.dumps(hb))
    ctx = make_ctx(settings, storage=st, mode="embedded")
    async with client(ctx) as c:
        r = (await c.get("/partials/status-dot")).text
    assert "采集出错" in r and "连续失败 5 轮" in r and "Too Many Requests" in r and "启动" in r
