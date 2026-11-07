"""网页底座：只限本机的安全检查、推送正文的 Markdown、推送记录、心跳与运行状态、页面骨架。全部离线。"""

import json
from datetime import timedelta

import pytest

pytest.importorskip("fastapi")

from tickersentinel.config import WebCfg  # noqa: E402
from tickersentinel.models import Message  # noqa: E402
from tickersentinel.notify.console import ConsoleNotifier  # noqa: E402
from tickersentinel.notify.recording import RecordingNotifier  # noqa: E402
from tickersentinel.storage import Storage  # noqa: E402
from tickersentinel.web.context import HEARTBEAT_KEY  # noqa: E402
from tickersentinel.web.markdown import md_to_html, strip_md  # noqa: E402
from tickersentinel.web.security import host_of  # noqa: E402

from conftest import utc  # noqa: E402
from web_support import client, make_ctx, write_headers  # noqa: E402

NOW = utc(2026, 10, 9, 15, 0)


# ───────── 安全 ─────────


def test_host_of():
    assert host_of("127.0.0.1:8765") == "127.0.0.1"
    assert host_of("localhost") == "localhost"
    assert host_of("[::1]:8765") == "::1"
    assert host_of("http://localhost:8765") == "localhost"
    assert host_of("evil.example:8765") == "evil.example"


async def test_rejects_foreign_host_and_unauthenticated_writes(settings):
    ctx = make_ctx(settings, now=NOW)
    async with client(ctx) as c:
        assert (await c.get("/api/ping")).json() == {"app": "tickersentinel", "mode": "standalone"}
        # DNS 重绑定：Host 是别的域名
        r = await c.get("/api/ping", headers={"Host": "evil.example:8765"})
        assert r.status_code == 400
        # 写请求没带 token / token 不对 / 来自别的网站
        assert (await c.post("/api/ping")).status_code == 403
        assert (await c.post("/api/ping", headers={"X-Tsen-Token": "wrong"})).status_code == 403
        r = await c.post("/api/ping", headers={"X-Tsen-Token": ctx.token, "Origin": "https://evil.example"})
        assert r.status_code == 403
        # 带对了 token：通过安全检查（这个地址本来就不收 POST → 405）
        assert (await c.post("/api/ping", headers=write_headers(ctx))).status_code == 405
    async with client(ctx, base_url="http://localhost:8765") as c:
        assert (await c.get("/api/ping")).status_code == 200


def test_web_host_must_be_loopback():
    assert WebCfg(host="localhost").host == "localhost"
    with pytest.raises(ValueError, match="只能是本机地址"):
        WebCfg(host="0.0.0.0")


# ───────── Markdown ─────────


def test_markdown_escapes_and_converts():
    out = str(md_to_html("**MU 急跌**\n- 现价 <b>97</b>\n  - 子项\n- [新闻](https://x.example/a?b=1&c=2)\n\n第二段"))
    assert "<strong>MU 急跌</strong>" in out
    assert "&lt;b&gt;97&lt;/b&gt;" in out and "<b>" not in out
    assert '<li class="sub">子项</li>' in out
    assert '<a href="https://x.example/a?b=1&amp;c=2" target="_blank" rel="noopener noreferrer">新闻</a>' in out
    assert out.endswith("<p>第二段</p>")


def test_markdown_rejects_script_and_js_links():
    out = str(md_to_html('<script>alert(1)</script> [点我](javascript:alert(1)) [x](https://a.example/" onclick="y)'))
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "javascript:" in out and 'href="javascript' not in out
    assert 'onclick="' not in out
    assert strip_md("**粗** [链接](https://a.example)\n- 项") == "粗 链接\n项"


# ───────── 推送记录 ─────────


class Boom(ConsoleNotifier):
    async def send(self, msg):
        raise RuntimeError("网络断了")


async def test_recording_notifier_records_success_and_failure():
    st = Storage(":memory:")
    ok = RecordingNotifier(ConsoleNotifier(None, echo=False), st)
    mid = await ok.send(Message(title="🔻 MU 急跌", body="- 现价 97", kind="alert", alert_ids=(3, 4)))
    assert mid and ok.name == "console" and ok.sent  # 内层渠道的属性照样能读
    bad = RecordingNotifier(Boom(None, echo=False), st)
    with pytest.raises(RuntimeError):
        await bad.send(Message(title="归因", body="x", kind="attribution", reply_to=mid))
    rows = st.recent_messages(10)
    assert [r["kind"] for r in rows] == ["attribution", "alert"]
    assert rows[1]["alert_ids"] == "3,4" and rows[1]["ok"] == 1 and rows[1]["message_id"] == mid
    assert rows[0]["ok"] == 0 and "网络断了" in rows[0]["error"] and rows[0]["reply_to"] == mid
    assert [r["id"] for r in st.messages_after(rows[1]["id"])] == [rows[0]["id"]]
    assert st.last_message_id() == rows[0]["id"]
    assert [r["id"] for r in st.recent_messages(10, before_id=rows[0]["id"])] == [rows[1]["id"]]


def test_messages_table_added_to_old_database(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    sqlite3.connect(path).execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)").connection.commit()
    st = Storage.in_memory_copy(path)
    st.record_message(Message(title="t", body="b"), channel="console", message_id="console-1")
    assert st.last_message_id() == 1


# ───────── 心跳与运行状态 ─────────


async def test_runtime_status_from_heartbeat(settings):
    st = Storage(":memory:")
    ctx = make_ctx(settings, storage=st, now=NOW)
    assert ctx.runtime()["running"] is False
    async with client(ctx) as c:
        assert "未运行" in (await c.get("/partials/status-dot")).text
    hb = {"at": (NOW - timedelta(seconds=50)).isoformat(), "last_tick_at": (NOW - timedelta(seconds=50)).isoformat(),
          "last_tick_ok": True}
    st.kv_set(HEARTBEAT_KEY, json.dumps(hb))
    assert ctx.runtime()["running"] is True
    async with client(ctx) as c:
        dot = (await c.get("/partials/status-dot")).text
        assert "运行中" in dot and "dot-ok" in dot
        data = (await c.get("/api/status")).json()
        assert data["running"] is True and {s["key"] for s in data["segments"]} >= {"us"}
    hb["at"] = (NOW - timedelta(minutes=10)).isoformat()  # 心跳停了
    st.kv_set(HEARTBEAT_KEY, json.dumps(hb))
    async with client(ctx) as c:
        assert "tsen run 未运行" in (await c.get("/partials/status-dot")).text
    st.kv_set(HEARTBEAT_KEY, "{bad json")
    assert ctx.heartbeat() is None


async def test_heartbeat_written_by_app(settings, tmp_path):
    from tickersentinel.app import App

    app = App(settings)
    try:
        app.last_tick_at, app.last_tick_ok = utc(2026, 10, 9, 14, 59), True
        app.write_heartbeat()
        hb = json.loads(app.storage.kv_get(HEARTBEAT_KEY))
        assert hb["last_tick_ok"] is True and hb["channel"] == "console" and hb["pid"] > 0
        assert app.web_context().storage is app.storage and app.web_context().mode == "embedded"
    finally:
        await app.aclose()


def test_job_labels():
    from tickersentinel.app import job_label

    assert job_label("brief:premarket:2026-10-30") == "盘前简报"
    assert job_label("events:refresh:06:40") == "刷新事件日历"
    assert job_label("monitor:tick") is None and job_label("catch_up") is None


# ───────── 页面骨架 ─────────


async def test_pages_render_with_nav_token_and_static(settings):
    ctx = make_ctx(settings, now=NOW, mode="preview")
    async with client(ctx) as c:
        for path, title in (("/", "总览"), ("/alerts", "异动"), ("/calendar", "日历")):
            r = await c.get(path)
            assert r.status_code == 200, path
            assert f'content="{ctx.token}"' in r.text and title in r.text
            assert "预览模式" in r.text  # 预览模式的提示条
        missing = await c.get("/no-such-page")
        assert missing.status_code == 404 and "页面不存在" in missing.text
        assert (await c.get("/api/nope")).status_code == 404
        for path in ("/static/css/app.css", "/static/js/app.js", "/static/vendor/htmx.min.js",
                     "/static/vendor/lightweight-charts.standalone.production.js", "/static/manifest.webmanifest"):
            assert (await c.get(path)).status_code == 200, path


async def test_embedded_start_reports_why_not(settings, monkeypatch):
    import socket

    from tickersentinel.web import server

    ctx = make_ctx(settings, now=NOW, mode="embedded")
    settings.web.enabled = False
    assert "关闭" in await server.start_embedded(ctx)
    settings.web.enabled = True
    with socket.socket() as s:  # 占住一个端口
        s.bind(("127.0.0.1", 0))
        s.listen()
        settings.web.port = s.getsockname()[1]
        assert "被占用" in await server.start_embedded(ctx)


async def test_embedded_server_starts_and_stops(settings):
    import socket

    import httpx

    from tickersentinel.web import server

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    settings.web.port = port
    handle = await server.start_embedded(make_ctx(settings, now=NOW, mode="embedded"))
    assert not isinstance(handle, str), handle
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(handle.url + "/api/ping")
            assert r.json()["mode"] == "embedded"
    finally:
        await handle.stop()
    assert handle.task.done()
