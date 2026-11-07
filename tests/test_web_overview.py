"""总览页：消息流（归因挂在异动下面、翻页、历史记录补齐、实时推送 SSE）、宏观看板、今明日程、今日异动。全部离线。
运行状态只在顶栏小标里（/partials/status-dot）。"""

import asyncio
import json
from datetime import date, timedelta

import pytest

pytest.importorskip("fastapi")

from tickersentinel.events.hub import EventHub  # noqa: E402
from tickersentinel.models import Alert, Event, Message, Trigger  # noqa: E402
from tickersentinel.sources.base import SourceError  # noqa: E402
from tickersentinel.sources.macro import MacroQuote, MacroSnapshot, TreasuryCurve  # noqa: E402
from tickersentinel.storage import Storage  # noqa: E402
from tickersentinel.web import overview_feed, overview_panels  # noqa: E402
from tickersentinel.web.context import HEARTBEAT_KEY  # noqa: E402
from tickersentinel.web.factory import create_app  # noqa: E402
from tickersentinel.web.pages import overview as page_mod  # noqa: E402

from conftest import utc  # noqa: E402
from web_support import client, make_ctx  # noqa: E402

NOW = utc(2026, 10, 9, 15, 0)  # 美东 11:00、芝加哥 10:00（周五，盘中）


def mk_alert(symbol: str, ts, direction: int = -1, move: float = -3.2, **kw) -> Alert:
    tr = Trigger(rule="rapid", direction=direction, move_pct=move, window_minutes=30, z=-4.1 * direction * -1,
                 anchor_ts=ts - timedelta(minutes=20), anchor_pct=1.0, minutes=20)
    return Alert(symbol=symbol, ts=ts, price=100.0, prev_close=102.0, triggers=[tr], window_minutes=20, move_pct=move,
                 benchmark_move_pct=-1.0, beta=1.2, abnormal_move_pct=-2.0, volume_ratio=2.0,
                 peers_move_pct={"SNDK": -2.1}, benchmark="SOXX", **kw)


def set_created(st: Storage, alert_id: int, ts) -> None:
    st._exec("UPDATE alerts SET created_at = ? WHERE id = ?", (ts.isoformat(), alert_id))


def insert_raw_alert(st: Storage, **cols) -> int:
    base = {"created_at": (NOW - timedelta(days=30)).isoformat(), "symbol": "MU", "ts": (NOW - timedelta(days=30)).isoformat(),
            "session_day": "2026-09-09", "rule": "level", "direction": -1, "price": 90.0, "prev_close": 95.0,
            "move_pct": -5.3, "triggers_json": json.dumps([{"rule": "level", "direction": -1, "move_pct": -5.3, "level_pct": 5}])}
    base.update(cols)
    keys = ",".join(base)
    cur = st._exec(f"INSERT INTO alerts ({keys}) VALUES ({','.join('?' * len(base))})", tuple(base.values()))
    return int(cur.lastrowid)


def heartbeat(st: Storage, *, age=timedelta(seconds=40), **extra) -> dict:
    hb = {"at": (NOW - age).isoformat(), "pid": 4242, "started_at": (NOW - timedelta(hours=3)).isoformat(),
          "last_tick_at": (NOW - age).isoformat(), "last_tick_ok": True, "tick_failures": 0, "channel": "feishu",
          "price_source": "yfinance", "llm": {"brief": "local(qwen)", "attribution": "local(qwen) → claude(sonnet)",
                                              "chat": "local(qwen)"},
          "events_refreshed_at": (NOW - timedelta(hours=2)).isoformat(),
          "next_jobs": [{"name": "收盘简报", "at": (NOW + timedelta(hours=5, minutes=20)).isoformat()},
                        {"name": "事件汇总", "at": (NOW + timedelta(hours=10)).isoformat()},
                        {"name": "过期的任务", "at": (NOW - timedelta(hours=1)).isoformat()}]}
    hb.update(extra)
    st.kv_set(HEARTBEAT_KEY, json.dumps(hb))
    return hb


# ───────── 空库 ─────────


async def test_empty_database_renders_every_block(settings):
    ctx = make_ctx(settings, now=NOW)  # 没有行情、事件、宏观，也没有心跳
    async with client(ctx) as c:
        r = await c.get("/")
        assert r.status_code == 200
        html = r.text
        assert "总览" in html and "2026-10-09 周五 10:00 CDT" in html
        assert "还没有推送记录" in html and "今天（10-09）还没有异动" in html and "今天和明天没有日程" in html
        assert 'data-after="0"' in html
        assert "还没有 tsen run 的运行记录" in (await c.get("/partials/status-dot")).text  # 运行状态在顶栏小标里
        assert "/static/js/overview.js" in html and "/static/css/overview.css" in html
        assert "宏观快照已关闭" in (await c.get("/partials/overview/macro")).text
        for path in ("/partials/overview/events", "/partials/overview/alerts",
                     "/partials/overview/feed?before_id=10", "/partials/overview/feed?before_ts=2026-10-01T00:00:00%2B00:00",
                     "/partials/overview/feed?before_id=abc", "/partials/overview/feed?before_ts=garbage"):
            assert (await c.get(path)).status_code == 200, path
        data = (await c.get("/api/feed")).json()
        assert data == {"items": [], "count": 0, "next_before_id": None, "next_before_ts": None}
        assert (await c.get("/api/feed?limit=xyz&before_id=")).status_code == 200


# ───────── 消息流 ─────────


def seed_feed(st: Storage) -> dict:
    a1 = st.record_alert(mk_alert("MU", NOW - timedelta(minutes=50)), "2026-10-09")
    a2 = st.record_alert(mk_alert("SNDK", NOW - timedelta(minutes=50), move=-4.0), "2026-10-09")
    a3 = st.record_alert(mk_alert("SKHY", NOW - timedelta(minutes=30), direction=1, move=3.5), "2026-10-09")
    t = NOW - timedelta(hours=1)
    ids = {}
    ids["brief"] = st.record_message(Message(title="☀️ 盘前简报", body="**要点**\n- 第一行\n- 第二行\n- 第三行", kind="brief"),
                                     channel="feishu", message_id="om_b", at=t)
    ids["group"] = st.record_message(
        Message(title="🔻 存储芯片 2 只一起跌", body="- MU 急跌\n- SNDK 急跌\n\n⏳ 正在检索可能原因…\n\n🌐 **宏观**（10:10）\n- 美债10年 4.10%",
                kind="alert", alert_ids=(a1, a2)), channel="feishu", message_id="om_g", at=t + timedelta(minutes=10))
    ids["follow"] = st.record_message(Message(title="🔻 MU 扩大", body="- 跟帖正文", kind="alert", alert_ids=(a1,),
                                              reply_to="om_g"), channel="feishu", message_id="om_f", at=t + timedelta(minutes=12))
    ids["attr"] = st.record_message(Message(title="🧭 存储芯片 2 只一起跌的归因", body="**结论：同业下调指引**\n- 证据 [n1]",
                                            kind="attribution", reply_to="om_g", alert_ids=(a1, a2)),
                                    channel="feishu", message_id="om_a", at=t + timedelta(minutes=14))
    ids["up"] = st.record_message(Message(title="🔺 SKHY 30分钟急涨 3.5 个点", body="- 现价 $100.00", kind="alert",
                                          alert_ids=(a3,)), channel="feishu", message_id="om_u", at=t + timedelta(minutes=30))
    # 归因只靠 alert_ids 对上（reply_to 为空，例如异动当时没拿到消息 ID）
    ids["attr2"] = st.record_message(Message(title="🧭 SKHY 异动归因", body="个股消息：新订单", kind="attribution",
                                             alert_ids=(a3,)), channel="feishu", message_id="om_a2", at=t + timedelta(minutes=33))
    ids["fail"] = st.record_message(Message(title="事件汇总", body="- 明天 MU 财报", kind="events"), channel="feishu",
                                    message_id=None, ok=False, error="RuntimeError: 网络断了", at=t + timedelta(minutes=40))
    ids["xss"] = st.record_message(Message(title='<img src=x onerror=alert(1)>', body="<script>alert(1)</script> [x](javascript:alert(1))",
                                           kind="info"), channel="feishu", message_id="om_x", at=t + timedelta(minutes=45))
    return {"alerts": (a1, a2, a3), **ids}


def article(html: str, key: str) -> str:
    """取出某张卡片的 HTML（到下一个 <article 为止）。"""
    at = html.index(f'data-key="{key}"')
    start = html.rfind("<article", 0, at)
    end = html.find("<article", at)
    return html[start: end if end > 0 else len(html)]


async def test_feed_threads_replies_under_alerts(settings):
    st = Storage(":memory:")
    ids = seed_feed(st)
    a1, a2, a3 = ids["alerts"]
    ctx = make_ctx(settings, storage=st, now=NOW)
    async with client(ctx) as c:
        html = (await c.get("/")).text
    assert f'data-after="{ids["xss"]}"' in html
    # 根卡片从晚到早：xss → 失败的汇总 → SKHY → 合并的异动 → 简报；归因和跟帖不单独成卡
    order = [html.index(f'data-key="m{ids[k]}"') for k in ("xss", "fail", "up", "group", "brief")]
    assert order == sorted(order)
    group = article(html, f"m{ids['group']}")
    assert "fc-alert fc-down" in group and f'data-alert-ids="{a1},{a2}"' in group
    assert f'href="/alerts/{a1}"' in group and f'href="/alerts/{a2}"' in group and "SNDK 详情" in group
    assert f'data-key="m{ids["attr"]}"' in group and f'data-key="m{ids["follow"]}"' in group  # 挂在下面
    assert "结论：同业下调指引" in group and "跟帖" in group
    assert "正在检索可能原因" not in group  # 归因到了，去掉“正在检索”那一行
    assert "宏观快照" in group and "fc-snap" in group  # 推送末尾的宏观数据折叠起来
    up = article(html, f"m{ids['up']}")
    assert "fc-alert fc-up" in up and f'data-key="m{ids["attr2"]}"' in up and "新订单" in up
    assert "30分钟急涨 3.5 个点" in up and "🔺" not in up.split("fc-title", 1)[1][:80]
    fail = article(html, f"m{ids['fail']}")
    assert "未送达" in fail and "网络断了" in fail and "fc-failed" in fail
    brief = article(html, f"m{ids['brief']}")
    assert "<details class=\"fc-fold\">" in brief and "盘前简报" in brief and "要点 · 第一行" in brief
    xss = article(html, f"m{ids['xss']}")
    assert "<script>" not in xss and "&lt;script&gt;" in xss and "<img" not in xss and 'href="javascript' not in xss
    assert "加载更早的" not in html and "历史记录" not in html


async def test_feed_json(settings):
    st = Storage(":memory:")
    ids = seed_feed(st)
    ctx = make_ctx(settings, storage=st, now=NOW)
    async with client(ctx) as c:
        data = (await c.get("/api/feed")).json()
        assert data["count"] == 8 and data["next_before_id"] is None
        group = next(i for i in data["items"] if i["id"] == ids["group"])
        assert [t["id"] for t in group["thread"]] == [ids["follow"], ids["attr"]]
        assert group["alerts"][0]["symbol"] == "MU" and group["alerts"][0]["direction"] == -1
        page = (await c.get(f"/api/feed?limit=2&before_id={ids['up']}")).json()
        assert page["count"] == 2 and page["next_before_id"] == ids["follow"]


async def test_feed_paging_by_message_id(settings, monkeypatch):
    st = Storage(":memory:")
    for i in range(35):
        st.record_message(Message(title=f"通知 {i}", body=f"第 {i} 条", kind="info"), channel="console",
                          message_id=f"c-{i}", at=NOW - timedelta(minutes=100 - i))
    ctx = make_ctx(settings, storage=st, now=NOW)
    async with client(ctx) as c:
        html = (await c.get("/")).text
        assert html.count("<article") == 30 and "通知 34" in html and "通知 4<" not in html
        assert 'hx-get="/partials/overview/feed?before_id=6"' in html
        more = (await c.get("/partials/overview/feed?before_id=6")).text
        assert more.count("<article") == 5 and "通知 0" in more and "没有更早的了" in more
        assert "加载更早的" not in more


async def test_history_backfill_before_first_message(settings):
    st = Storage(":memory:")
    old1 = st.record_alert(mk_alert("MU", NOW - timedelta(days=2)), "2026-10-07")
    set_created(st, old1, NOW - timedelta(days=2))
    st.save_attribution(old1, "**主因：财报指引**\n- 细节", {"summary": "主因：财报指引"})
    st.set_alert_message_id(old1, "om_old")
    old2 = st.record_alert(mk_alert("SNDK", NOW - timedelta(days=1), direction=1, move=2.5), "2026-10-08")
    set_created(st, old2, NOW - timedelta(days=1))
    legacy = insert_raw_alert(st, created_at=(NOW - timedelta(days=3)).isoformat())  # 旧版关口、没有 segment / benchmark
    broken = insert_raw_alert(st, created_at=(NOW - timedelta(days=4)).isoformat(), triggers_json="not json")
    st.record_brief("premarket", "☀️ 盘前简报（10-08）", "- 要点", "om_brief", at=NOW - timedelta(days=1, hours=2))
    # 已经进了推送记录的异动（边界附近）：不再当历史记录重复显示
    boundary = st.record_alert(mk_alert("SKHY", NOW - timedelta(hours=2)), "2026-10-09")
    set_created(st, boundary, NOW - timedelta(hours=2, seconds=1))
    st.record_message(Message(title="🔻 SKHY 急跌", body="- 现价", kind="alert", alert_ids=(boundary,)), channel="feishu",
                      message_id="om_new", at=NOW - timedelta(hours=2))
    ctx = make_ctx(settings, storage=st, now=NOW)
    async with client(ctx) as c:
        html = (await c.get("/")).text
        assert html.count("历史记录") >= 4
        keys = [f"m{st.last_message_id()}", f"a{old2}", f"b1", f"a{old1}", f"a{legacy}"]
        pos = [html.index(f'data-key="{k}"') for k in keys]
        assert pos == sorted(pos)  # 从晚到早
        assert f'data-key="a{boundary}"' not in html and f'data-key="a{broken}"' not in html
        h1 = article(html, f"a{old1}")
        assert "主因：财报指引" in h1 and "fc-reply" in h1 and f'href="/alerts/{old1}"' in h1
        assert "20分钟内从高点回落" in h1  # render_alert 重新排出来的正文
        assert "跌破" in article(html, f"a{legacy}")  # 旧版关口的说法
        assert "fc-up" in article(html, f"a{old2}")
        # 按时间往前翻历史记录
        data = (await c.get("/api/feed?limit=2")).json()
        assert [i["key"] for i in data["items"]] == [f"m{st.last_message_id()}", f"a{old2}"]
        assert data["next_before_ts"] is not None and data["next_before_id"] is None
        nxt = (await c.get("/api/feed", params={"limit": 2, "before_ts": data["next_before_ts"]})).json()
        assert [i["key"] for i in nxt["items"]] == ["b1", f"a{old1}"]
        last = (await c.get("/api/feed", params={"limit": 5, "before_ts": nxt["next_before_ts"]})).json()
        assert [i["key"] for i in last["items"]] == [f"a{legacy}"] and last["next_before_ts"] is None
        part = (await c.get("/partials/overview/feed", params={"before_ts": nxt["next_before_ts"]})).text
        assert f'data-key="a{legacy}"' in part and "没有更早的了" in part


async def test_history_only_when_no_messages(settings):
    st = Storage(":memory:")
    a = st.record_alert(mk_alert("MU", NOW - timedelta(hours=3)), "2026-10-09")
    ctx = make_ctx(settings, storage=st, now=NOW)
    async with client(ctx) as c:
        html = (await c.get("/")).text
    assert f'data-key="a{a}"' in html and "历史记录" in html and "还没有推送记录" not in html


async def walk_feed(c, limit: int, max_pages: int = 20) -> list[list[str]]:
    """沿着 next_before_id / next_before_ts 一页页往前翻，返回每一页的根卡片 key。"""
    pages, params = [], {"limit": limit}
    for _ in range(max_pages):
        data = (await c.get("/api/feed", params=params)).json()
        pages.append([i["key"] for i in data["items"]])
        if data["next_before_id"] is not None:
            params = {"limit": limit, "before_id": data["next_before_id"]}
        elif data["next_before_ts"] is not None:
            params = {"limit": limit, "before_ts": data["next_before_ts"]}
        else:
            return pages
    raise AssertionError("翻页没有结束")


async def test_history_paging_survives_corrupt_row_mid_page(settings):
    st = Storage(":memory:")
    ids = []
    for k in range(40):
        a = st.record_alert(mk_alert("MU", NOW - timedelta(hours=k + 1)), "2026-10-09")
        set_created(st, a, NOW - timedelta(hours=k + 1))
        ids.append(a)
    st._exec("UPDATE alerts SET triggers_json = 'bad' WHERE id = ?", (ids[5],))  # 第一页中间的一条坏记录
    ctx = make_ctx(settings, storage=st, now=NOW)
    async with client(ctx) as c:
        pages = await walk_feed(c, 30)
        html = (await c.get("/")).text
    seen = [k for pg in pages for k in pg]
    assert seen == [f"a{i}" for i in ids if i != ids[5]]  # 坏记录跳过，其余全部能翻到、不重复
    assert len(pages) == 2 and len(pages[0]) == 30  # 跳过的那条不占名额
    assert "加载更早的" in html and "before_ts=" in html


async def test_history_paging_with_a_bad_page_and_ties(settings):
    st = Storage(":memory:")
    ids = []
    for k in range(8):
        a = st.record_alert(mk_alert("MU", NOW - timedelta(hours=k + 1)), "2026-10-09")
        set_created(st, a, NOW - timedelta(hours=k + 1))
        ids.append(a)
    for i in ids[:4]:  # 整整一页都是坏记录：这一页是空的，但游标照样往前走
        st._exec("UPDATE alerts SET triggers_json = 'bad' WHERE id = ?", (i,))
    set_created(st, ids[6], NOW - timedelta(hours=6))  # 和上一条同一时刻，正好落在两页的边界上
    ctx = make_ctx(settings, storage=st, now=NOW)
    async with client(ctx) as c:
        pages = await walk_feed(c, 2)
    seen = [k for pg in pages for k in pg]
    assert sorted(seen) == sorted(f"a{i}" for i in ids[4:]) and len(seen) == len(set(seen))


async def test_history_before_ts_with_any_offset(settings):
    st = Storage(":memory:")
    ids = []
    for k in range(5):
        a = st.record_alert(mk_alert("MU", NOW - timedelta(hours=k + 1)), "2026-10-09")
        set_created(st, a, NOW - timedelta(hours=k + 1))
        ids.append(a)
    ctx = make_ctx(settings, storage=st, now=NOW)  # 同一时刻，写成 UTC、芝加哥时间、不带时区三种
    async with client(ctx) as c:
        utc_keys = [i["key"] for i in (await c.get("/api/feed", params={"before_ts": "2026-10-09T12:30:00+00:00"})).json()["items"]]
        cdt_keys = [i["key"] for i in (await c.get("/api/feed", params={"before_ts": "2026-10-09T07:30:00-05:00"})).json()["items"]]
        naive = [i["key"] for i in (await c.get("/api/feed", params={"before_ts": "2026-10-09T12:30:00"})).json()["items"]]
    assert utc_keys == cdt_keys == naive == [f"a{i}" for i in ids[2:]]  # 12:00、11:00、10:00 UTC 那三条


async def test_history_never_repeats_alerts_already_in_messages(settings):
    st = Storage(":memory:")
    ids = []
    for k in range(5):
        a = st.record_alert(mk_alert("MU", NOW - timedelta(days=1, hours=k)), "2026-10-08")
        set_created(st, a, NOW - timedelta(days=1, hours=k))
        ids.append(a)
    # 唯一的一条推送记录是最早那条异动的补发：后面几页的历史记录里也不能再出现它
    st.record_message(Message(title="🔻 MU 急跌（补发）", body="- 现价", kind="alert", alert_ids=(ids[-1],)),
                      channel="feishu", message_id="om_resend", at=NOW - timedelta(hours=1))
    ctx = make_ctx(settings, storage=st, now=NOW)
    async with client(ctx) as c:
        pages = await walk_feed(c, 3)
        # 游标比最早一条消息还晚：历史记录只到最早一条消息之前
        late = (await c.get("/api/feed", params={"before_ts": NOW.isoformat()})).json()
    seen = [k for pg in pages for k in pg]
    mid = st.last_message_id()
    assert seen == [f"m{mid}"] + [f"a{i}" for i in ids[:-1]]
    assert [i["key"] for i in late["items"]] == [f"a{i}" for i in ids[:-1]]


async def test_history_follow_up_threads_under_lead(settings):
    st = Storage(":memory:")
    lead = st.record_alert(mk_alert("MU", NOW - timedelta(days=1, hours=2)), "2026-10-08")
    set_created(st, lead, NOW - timedelta(days=1, hours=2))
    st.save_attribution(lead, "**主因：同业指引下修**\n- 细节", {"summary": "主因：同业指引下修"})
    follow = st.record_alert(mk_alert("SNDK", NOW - timedelta(days=1, hours=1, minutes=55)), "2026-10-08")
    set_created(st, follow, NOW - timedelta(days=1, hours=1, minutes=55))
    note = "同一波行情（09:05 那一条的跟帖），原因见那一条的归因"
    st.save_attribution(follow, note, {"summary": note, "same_wave_as": lead})
    newer = st.record_alert(mk_alert("SKHY", NOW - timedelta(hours=3)), "2026-10-09")
    set_created(st, newer, NOW - timedelta(hours=3))
    ctx = make_ctx(settings, storage=st, now=NOW)
    async with client(ctx) as c:
        html = (await c.get("/")).text
        # 第一条在这一页：跟帖挂在它下面，不单独成卡片
        lead_card = article(html, f"a{lead}")
        assert f'data-key="a{follow}"' in lead_card and "跟帖" in lead_card and "主因：同业指引下修" in lead_card
        assert html.count(f'data-key="a{follow}"') == 1
        # 第一条不在这一页：跟帖单独成卡片，归因换成第一条的结论，并链接过去
        data = (await c.get("/api/feed", params={"limit": 2})).json()
        assert [i["key"] for i in data["items"]] == [f"a{newer}", f"a{follow}"]
        fu = data["items"][1]
        assert fu["lead_id"] == lead and fu["thread"][0]["body"] == "同一波行情：主因：同业指引下修"
    one = overview_feed.feed_page(ctx, limit=2)
    assert one.items[1].thread[0].lead_id == lead
    part = page_mod.fragment(create_app(ctx), "overview/_one.html", it=one.items[1], nested=False)
    assert f'href="/alerts/{lead}"' in part and "同一波的第一条" in part
    # attribution_json 是坏的、或者指向自己：当普通归因处理
    assert overview_feed._lead_of({"id": 3, "attribution_json": "nope"}) is None
    assert overview_feed._lead_of({"id": 3, "attribution_json": json.dumps({"same_wave_as": 3})}) is None
    assert overview_feed._lead_of({"id": 3, "attribution_json": json.dumps({"same_wave_as": "x"})}) is None
    assert overview_feed._lead_of({"id": 3, "attribution_json": json.dumps([1])}) is None


def test_feed_item_helpers():
    it = overview_feed.FeedItem(key="m1", kind="alert", title="🔺 MU 急涨", body="- a\n\n⏳ 正在检索可能原因…", at=NOW)
    assert it.direction == 1 and it.clean_title == "MU 急涨"
    assert it.parts == ("- a\n\n⏳ 正在检索可能原因…", "")  # 没有归因时保留“正在检索”
    assert overview_feed.parse_ids("3, 4,x,,5") == [3, 4, 5] and overview_feed.parse_ids(None) == []
    empty = overview_feed.FeedItem(key="m2", kind="weird", title="", body="", at=None)
    assert empty.clean_title == "weird" or empty.clean_title == "消息"
    assert empty.direction == 0 and empty.preview == ""


# ───────── 运行状态（顶栏小标） ─────────


async def test_status_pill_follows_heartbeat(settings):
    st = Storage(":memory:")
    ctx = make_ctx(settings, storage=st, now=NOW)
    async with client(ctx) as c:
        assert (await c.get("/partials/overview/status")).status_code == 404  # 原来的运行状态方块已去掉
        heartbeat(st)
        r = (await c.get("/partials/status-dot")).text
        assert "运行中" in r and "dot-ok" in r
        heartbeat(st, last_tick_ok=False, tick_failures=3)
        r = (await c.get("/partials/status-dot")).text
        assert "检测出错" in r and "连续失败 3 次" in r
        heartbeat(st, age=timedelta(minutes=45))  # 心跳停了
        assert "tsen run 未运行" in (await c.get("/partials/status-dot")).text


# ───────── 宏观看板 ─────────


class FakeMacro:
    def __init__(self, snap=None, exc=None, delay=0.0):
        self.snap, self.exc, self.delay = snap, exc, delay

    async def snapshot(self, now=None, *, use_cache=True):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.snap


def macro_snapshot() -> MacroSnapshot:
    q = lambda **kw: MacroQuote(**{"ts": NOW - timedelta(minutes=10), "change_1h": None, **kw})  # noqa: E731
    return MacroSnapshot(at=NOW, quotes=[
        q(key="us10y", name="美债10年", symbol="^TNX", kind="yield", group="利率", value=4.123, change=3.2,
          change_label="较昨收", change_1h=-1.0),
        q(key="oil", name="原油", symbol="CL=F", kind="price", group="大宗商品", value=78.4, change=-1.26, change_label="24h"),
        q(key="es", name="标普期货", symbol="ES=F", kind="level", group="股指", value=6012.0, change=None,
          change_label="较美股收盘", off_hours=True),
        q(key="krw", name="美元/韩元", symbol="KRW=X", kind="level", group="存储芯片", value=1380.5, change=0.4,
          change_label="24h", theme="memory"),
    ], curve=TreasuryCurve(date(2026, 10, 8), {"2Y": 3.52, "10Y": 4.11, "30Y": 4.6}), errors={"vix": "没有数据"})


async def test_macro_panel_renders_groups_curve_and_theme(settings):
    ctx = make_ctx(settings, now=NOW, macro=FakeMacro(macro_snapshot()))
    async with client(ctx) as c:
        r = (await c.get("/partials/overview/macro")).text
    assert "利率" in r and "美债10年" in r and "4.12%" in r and "+3bp" in r and "1h -1bp" in r
    assert "原油" in r and "$78.40" in r and "-1.3%" in r and "24h" in r
    assert "标普期货" in r and "美股休市" in r
    assert "财政部收益率曲线" in r and "10-08" in r and "3.52%" in r and "2s10s" in r and "+59bp" in r
    assert "板块指标 · 存储芯片" in r and "1,380" in r
    assert "快照 10:00 CDT" in r and "有 1 项没取到" in r
    assert "括号" not in r and "标“24h”的为较 24 小时前" in r
    assert r.index("板块指标") > r.index("原油")


async def test_macro_panel_error_and_timeout(settings, monkeypatch):
    ctx = make_ctx(settings, now=NOW, macro=FakeMacro(exc=SourceError("宏观数据全部取不到：yfinance 超时")))
    async with client(ctx) as c:
        r = await c.get("/partials/overview/macro")
        assert r.status_code == 200 and "宏观数据暂时取不到" in r.text and "yfinance 超时" in r.text and "重试" in r.text
    monkeypatch.setattr(overview_panels, "MACRO_EXTRA_SECONDS", 0.0)
    settings.macro.timeout_seconds = 0.05
    ctx = make_ctx(settings, now=NOW, macro=FakeMacro(macro_snapshot(), delay=2))
    async with client(ctx) as c:
        r = await c.get("/partials/overview/macro")
        assert r.status_code == 200 and "宏观数据暂时取不到" in r.text and "还没取到" in r.text
    empty = MacroSnapshot(at=NOW)
    ctx = make_ctx(settings, now=NOW, macro=FakeMacro(empty))
    async with client(ctx) as c:
        assert "没有取到任何宏观指标" in (await c.get("/partials/overview/macro")).text


# ───────── 今明日程 ─────────


def seed_events(st: Storage) -> None:
    evs = [
        Event(kind="earnings", symbol="MU", day=date(2026, 10, 9), title="美光 2026 财年四季度业绩", key="earnings:MU:2026Q4",
              at=utc(2026, 10, 9, 20, 5), confirmed=True, source="finnhub", url="https://investors.example/mu"),
        Event(kind="macro", symbol="FED", day=date(2026, 10, 10), title="FOMC 议息决议", key="fed:2026:7",
              at=None, time_hint="", confirmed=False, source="fed", url="javascript:alert(1)"),
        Event(kind="insider_sale", symbol="SNDK", day=date(2026, 10, 10), title="董事拟减持 1 万股", key="144:abc",
              time_hint="盘后", confirmed=True, source="sec_144"),
        Event(kind="earnings", symbol="NVDA", day=date(2026, 10, 9), title="不在监控范围的公司", key="earnings:ZZZZ:2026Q4",
              confirmed=True, source="yfinance"),
        Event(kind="custom", symbol="MU", day=date(2026, 10, 12), title="后天的日程", key="custom:1", confirmed=True,
              source="config"),
        # 美东 10-09 23:30 = 芝加哥 22:30，按本地日期是今天
        Event(kind="macro", symbol="KR", day=date(2026, 10, 10), title="韩国 10 月前 10 天出口", key="kr:1",
              at=utc(2026, 10, 10, 3, 30), confirmed=False, source="calendar_rules"),
    ]
    for e in evs:
        st.upsert_event(e, now=NOW - timedelta(days=5))


@pytest.mark.parametrize("with_hub", [False, True])
async def test_events_today_and_tomorrow(settings, with_hub):
    st = Storage(":memory:")
    seed_events(st)
    st._exec("UPDATE events SET symbol = 'ZZZZ' WHERE key = 'earnings:ZZZZ:2026Q4'")
    hub = EventHub(settings, [], st) if with_hub else None
    ctx = make_ctx(settings, storage=st, now=NOW, events=hub)
    async with client(ctx) as c:
        r = (await c.get("/partials/overview/events")).text
    today, tomorrow = r.split("明天", 1)
    assert "美光 2026 财年四季度业绩" in today and "15:05" in today and "CDT" in today and "财报" in today
    assert 'href="https://investors.example/mu"' in today and 'rel="noopener noreferrer"' in today
    assert "持仓" in today and "韩国 10 月前 10 天出口" in today and "22:30" in today and "韩国数据" in today
    assert "FOMC 议息决议" in tomorrow and "美联储" in tomorrow and "全天" in tomorrow and "预计" in tomorrow
    assert "javascript:" not in r
    assert "董事拟减持" in tomorrow and "拟减持" in tomorrow and "盘后" in tomorrow
    assert "不在监控范围" not in r and "后天的日程" not in r
    assert today.index("美光") < today.index("韩国")  # 按时间排


# ───────── 今日异动 ─────────


async def test_today_alerts_summary(settings):
    st = Storage(":memory:")
    st.record_alert(mk_alert("MU", NOW - timedelta(minutes=90), move=-3.2), "2026-10-09")
    st.record_alert(mk_alert("MU", NOW - timedelta(minutes=20), direction=1, move=5.6), "2026-10-09")
    st.record_alert(mk_alert("SKHY", NOW - timedelta(minutes=40), move=-2.0), "2026-10-09")
    st.record_alert(mk_alert("SNDK", utc(2026, 10, 9, 4, 30)), "2026-10-08")  # 芝加哥 10-08 23:30：昨天
    insert_raw_alert(st, symbol="SKHY", ts=(NOW - timedelta(minutes=5)).isoformat(), triggers_json="[]", move_pct=-6.5)
    insert_raw_alert(st, symbol="basket:memory", ts=(NOW - timedelta(minutes=70)).isoformat(), triggers_json="oops",
                     move_pct=None, direction=1)
    ctx = make_ctx(settings, storage=st, now=NOW)
    async with client(ctx) as c:
        r = (await c.get("/partials/overview/alerts")).text
    assert "SNDK" not in r
    assert 'href="/alerts?symbol=MU"' in r and "2 次" in r and "+5.6 个点" in r
    assert "-6.5%" in r  # 旧记录：没有触发明细时用 move_pct
    assert "存储芯片篮子" in r and "basket%3Amemory" in r
    assert r.index("SKHY") < r.index("MU") < r.index("存储芯片篮子")  # 最近有异动的在上面
    assert "10-09 · 3 只 · 共 5 次" in r  # 日期在片段里，随片段刷新
    data = overview_panels.today_alerts(ctx)
    assert data["total"] == 5 and data["day"] == "10-09"


def test_headline_move_prefers_rule_priority():
    row = {"triggers_json": json.dumps([{"rule": "day", "direction": -1, "move_pct": -5.5, "anchor_pct": 1.0},
                                        {"rule": "rapid", "direction": -1, "move_pct": -3.0}]), "move_pct": -9}
    assert overview_panels.headline_move(row) == (-3.0, "%")
    assert overview_panels.headline_move({"triggers_json": None, "move_pct": "x"}) == (None, "%")


# ───────── 实时推送（SSE） ─────────


async def test_stream_generator_sends_new_messages_and_pings(settings):
    st = Storage(":memory:")
    a = st.record_alert(mk_alert("MU", NOW), "2026-10-09")
    first = st.record_message(Message(title="🔻 MU 急跌", body="- x", kind="alert", alert_ids=(a,)), channel="feishu",
                              message_id="om_1", at=NOW)
    app = create_app(make_ctx(settings, storage=st, now=NOW))
    state = {"n": 0}

    async def disconnected():
        state["n"] += 1
        return state["n"] > 12

    chunks = []
    async for chunk in page_mod.stream_events(app, first, disconnected, poll=0.005, ping=0.0):
        chunks.append(chunk)
        if len(chunks) == 3:  # 连接后插入一条归因
            st.record_message(Message(title="🧭 MU 异动归因", body="**原因**", kind="attribution", reply_to="om_1",
                                      alert_ids=(a,)), channel="feishu", message_id="om_2", at=NOW)
    assert chunks[0].startswith("retry: 3000")
    assert any(c == ": ping\n\n" for c in chunks)
    events = [c for c in chunks if c.startswith("id: ")]
    assert len(events) == 1  # 只发新的那条，first 本身不重发
    head, data = events[0].split("data: ", 1)
    assert head.startswith(f"id: {first + 1}\nevent: message\n")
    payload = json.loads(data)
    assert payload["kind"] == "attribution" and payload["reply_to"] == "om_1" and payload["alert_ids"] == [a]
    assert "fc-reply" in payload["thread_html"] and "<article" in payload["html"] and "原因" in payload["html"]


async def test_stream_endpoint_over_asgi(settings, monkeypatch):
    """走完整的 ASGI 调用（含安全中间件）：拿到第一条消息后断开。"""
    monkeypatch.setattr(page_mod, "POLL_SECONDS", 0.01)
    st = Storage(":memory:")
    st.record_message(Message(title="旧消息", body="x", kind="info"), channel="console", message_id="c-1", at=NOW)
    st.record_message(Message(title="新消息 <b>", body="y", kind="info"), channel="console", message_id="c-2", at=NOW)
    app = create_app(make_ctx(settings, storage=st, now=NOW))
    gone = asyncio.Event()
    sent: list[dict] = []
    body = []

    async def receive():
        if not sent:
            sent.append({"type": "started"})
            return {"type": "http.request", "body": b"", "more_body": False}
        await gone.wait()
        return {"type": "http.disconnect"}

    async def send(msg):
        sent.append(msg)
        if msg["type"] == "http.response.body":
            body.append(msg.get("body", b"").decode())
            if "event: message" in "".join(body):
                gone.set()

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": "/api/stream", "raw_path": b"/api/stream", "query_string": b"after=1", "root_path": "",
             "headers": [(b"host", b"127.0.0.1:8765"), (b"last-event-id", b"0")], "client": ("127.0.0.1", 5000),
             "server": ("127.0.0.1", 8765)}
    await asyncio.wait_for(app(scope, receive, send), 5)
    start = next(m for m in sent if m.get("type") == "http.response.start")
    headers = {k.decode(): v.decode() for k, v in start["headers"]}
    assert start["status"] == 200 and headers["content-type"].startswith("text/event-stream")
    assert headers["cache-control"] == "no-cache"
    text = "".join(body)
    assert "id: 2\n" in text and "id: 1\n" not in text and "新消息 &lt;b&gt;" in text


async def test_stream_rejects_foreign_host(settings):
    ctx = make_ctx(settings, now=NOW)
    async with client(ctx) as c:
        r = await c.get("/api/stream", headers={"Host": "evil.example:8765"})
        assert r.status_code == 400



# ───────── 宏观看板的小走势线（决策 21） ─────────


def spark_store():
    """最近 24 小时的 ^TNX（往上走）和 CL=F（往下走）分钟线；ES=F、KRW=X 库里没有。"""
    import numpy as np
    import pandas as pd

    from tickersentinel.barstore import BarStore

    st = BarStore(":memory:")
    idx = pd.date_range(NOW - timedelta(hours=24), NOW, freq="1min", tz="UTC", inclusive="left")
    st.upsert("^TNX", pd.DataFrame({"close": np.linspace(4.0, 4.2, len(idx))}, index=idx))
    st.upsert("CL=F", pd.DataFrame({"close": np.linspace(80.0, 78.0, len(idx))}, index=idx))
    return st


async def test_macro_panel_sparklines_from_bar_store(settings):
    ctx = make_ctx(settings, now=NOW, macro=FakeMacro(macro_snapshot()), bars=spark_store())
    async with client(ctx) as c:
        r = (await c.get("/partials/overview/macro")).text
        page = (await c.get("/")).text
    assert r.count('<svg class="spark') == 2
    assert 'class="spark up"' not in r  # 走势线用中性色：线是 24 小时，旁边的涨跌是较昨收，方向可能相反
    assert "<polyline points=" in r and 'stroke="currentColor"' in r
    assert r.count('class="mq-spark"') == 4  # 每行都有这一列（没数据的行空着），列不会错位
    assert r.count('href="/macro"') == 4 and "小走势线为各自最后一笔往前 24 小时" in r  # 每行都能点进 /macro
    assert "美债10年" in r and "+3bp" in r  # 原来的内容照旧
    assert 'href="/macro"' in page and "看趋势" in page and 'hx-trigger="load, every 60s"' in page


async def test_macro_panel_treasury_curve_rows_align_with_the_others(settings):
    """财政部收益率曲线和其他行用同一套列：每个期限一行，近 90 天日线走势线，较前日（bp）。"""
    from datetime import date

    from tickersentinel.timeutil import utcnow

    st = spark_store()
    today = utcnow().date()
    st.upsert_curve((today - timedelta(days=i), {"2Y": 3.5 + i * 0.01, "10Y": 4.0, "30Y": 4.6}) for i in range(10, 0, -1))
    ctx = make_ctx(settings, now=NOW, macro=FakeMacro(macro_snapshot()), bars=st)
    async with client(ctx) as c:
        r = (await c.get("/partials/overview/macro")).text
    assert "美债2年" in r and "2s10s 利差" in r and "较前日" in r and "近 90 天" in r
    assert "3.51%" in r and "-1bp" in r  # 最近一天 2Y = 3.51%，较前一天 −1bp
    assert r.count('<col class="c-name">') >= 2  # 每组同一套列宽
    assert isinstance(today, date)


async def test_macro_panel_without_bar_store_has_no_spark_column(settings, monkeypatch):
    ctx = make_ctx(settings, now=NOW, macro=FakeMacro(macro_snapshot()))
    async with client(ctx) as c:
        r = (await c.get("/partials/overview/macro")).text
    assert "<svg" not in r and "小走势线" not in r  # 走势线那一列留空（各组上下对齐）
    assert "美债10年" in r and 'href="/macro"' in r

    # 读库出错：只是不画，看板照常
    from tickersentinel.web import macro_data

    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(macro_data, "sparkline", boom)
    ctx = make_ctx(settings, now=NOW, macro=FakeMacro(macro_snapshot()), bars=spark_store())
    async with client(ctx) as c:
        r = await c.get("/partials/overview/macro")
    assert r.status_code == 200 and "<svg" not in r.text and "美债10年" in r.text
