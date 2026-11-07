"""网页的异动页：列表与筛选、CSV、详情各卡片、标注、走势图数据。全部离线（合成行情 + 内存库）。"""

import json
from datetime import date, datetime, timedelta

import pytest

pytest.importorskip("fastapi")

from tickersentinel.models import Alert, Event, Trigger  # noqa: E402
from tickersentinel.storage import Storage  # noqa: E402
from tickersentinel.timeutil import UTC  # noqa: E402
from tickersentinel.web import alerts_chart  # noqa: E402
from tickersentinel.web.alerts_data import Filters, parse_attribution, parse_macro, to_view  # noqa: E402

from conftest import FakePriceSource, TARGET_DAY, at, build_market, isolate, utc  # noqa: E402
from web_support import client, make_ctx, write_headers  # noqa: E402

NOW = utc(2026, 10, 9, 20, 0)  # 10-09 周五 15:00 CDT（美股收盘前）


def _session(calendar):
    return calendar.session(TARGET_DAY)


def _rapid(direction=-1, move=-3.2, anchor=None, anchor_pct=1.0, minutes=20, window=30):
    return Trigger(rule="rapid", direction=direction, move_pct=move, window_minutes=window, z=-5.1 * direction * -1,
                   anchor_ts=anchor, anchor_pct=anchor_pct, minutes=minutes)


def _alert(symbol, ts, *, triggers, price=104.0, prev_close=110.0, segment="regular", benchmark="SOXX", beta=1.3,
           bench=-1.0, abn=-1.9, move=-3.2, peers=None, window=20, escalation=False):
    return Alert(symbol=symbol, ts=ts, price=price, prev_close=prev_close, triggers=triggers, window_minutes=window,
                 move_pct=move, benchmark_move_pct=bench, beta=beta, abnormal_move_pct=abn, volume_ratio=2.4,
                 peers_move_pct=peers if peers is not None else {"SNDK": -2.0, "SOXX": -1.0}, is_escalation=escalation,
                 segment=segment, benchmark=benchmark)


ATTR_TEXT = ("**结论**：同业盘中下调指引，板块普跌（主导：板块）\n"
             "1. 🟢[高] WDC 下调季度指引（异动前 10 分钟） 来源：[Reuters](https://reuters.example/a) [日程 美国 CPI]\n"
             "2. ⚪[低] 获利了结（盘中）\n"
             "**关注**：明天美光的业绩说明会")
ATTR_JSON = {
    "summary": "同业盘中下调指引，板块普跌", "driver": "板块",
    "causes": [
        {"cause": "WDC 下调季度指引", "confidence": "高", "evidence": ["#1", "https://search.example/b", "日程：美国 CPI"],
         "timing": "异动前 10 分钟"},
        {"cause": "获利了结", "confidence": "低", "evidence": ["#4"], "timing": "盘中"},
    ],
    "watch": "明天美光的业绩说明会",
}
MACRO = {
    "at": "2026-10-09T15:30:00+00:00",
    "us10y": {"symbol": "^TNX", "value": 4.12, "change": 3.0, "change_label": "较昨收", "change_1h": 1.0,
              "ts": "2026-10-09T15:29:00+00:00", "off_hours": False},
    "spx": {"symbol": "ES=F", "value": 6512.3, "change": -0.8, "change_label": "较昨收", "change_1h": None,
            "ts": "2026-10-09T15:29:00+00:00", "off_hours": True},
    "gold": {"symbol": "GC=F", "value": 4012.5, "change": 0.4, "change_label": "24h", "change_1h": None,
             "ts": "2026-10-09T15:29:00+00:00", "off_hours": False},
    "memory.krw": {"symbol": "KRW=X", "value": 1402.1, "change": 0.2, "change_label": "24h", "change_1h": None,
                   "ts": "2026-10-09T15:29:00+00:00", "off_hours": False},
    "weird": "not a dict",
    "treasury": {"day": "2026-10-08", "2Y": 3.61, "10Y": 4.10, "30Y": 4.70},
}


def seed(st: Storage, calendar) -> dict[str, int]:
    """一组有代表性的记录：结构化归因 + 宏观快照 + 标注、同一波的跟帖、盘后、夜间信号、旧版关口、损坏的行、很久以前的。"""
    s = _session(calendar)
    ids = {}
    ts1 = at(s, 120)
    a1 = _alert("MU", ts1, triggers=[
        _rapid(anchor=ts1 - timedelta(minutes=20)),
        Trigger(rule="day", direction=-1, move_pct=-5.4, level_pct=5.0),
    ])
    ids["mu"] = st.record_alert(a1, "2026-10-09")
    st.save_attribution(ids["mu"], ATTR_TEXT, ATTR_JSON)
    st.set_alert_macro(ids["mu"], MACRO)
    st.set_alert_label(ids["mu"], "WDC 指引, 板块\"联动\"", "=cmd|' /C calc'!A0")
    st.set_alert_message_id(ids["mu"], "m-1")

    a2 = _alert("SKHY", ts1 + timedelta(minutes=2), triggers=[_rapid(move=-3.0, anchor=ts1 - timedelta(minutes=15))])
    ids["skhy"] = st.record_alert(a2, "2026-10-09")
    st.save_attribution(ids["skhy"], "同一波行情（10:30 那一条的跟帖），原因见那一条的归因",
                        {"summary": "同一波行情（10:30 那一条的跟帖），原因见那一条的归因", "same_wave_as": ids["mu"]})

    post_ts = calendar.session(date(2026, 10, 8)).close + timedelta(minutes=12)
    a3 = _alert("SNDK", post_ts, triggers=[_rapid(direction=1, move=4.1, anchor=post_ts - timedelta(minutes=10),
                                                  anchor_pct=-0.5, minutes=10, window=15)],
                price=63.0, prev_close=60.0, segment="post", bench=0.5, abn=3.4, move=4.1, escalation=True)
    ids["sndk_post"] = st.record_alert(a3, "2026-10-08:post")
    st.save_attribution(ids["sndk_post"], "**结论**：盘后财报超预期\n- 来源：[公司公告](https://ir.example/q3)", None)

    night_ts = utc(2026, 10, 8, 1, 30)  # 首尔 10:30
    a4 = Alert(symbol="000660.KS", ts=night_ts, price=250000.0, prev_close=240000.0,
               triggers=[Trigger(rule="day", direction=1, move_pct=5.2, level_pct=5.0)], window_minutes=None,
               move_pct=5.2, benchmark_move_pct=None, beta=None, abnormal_move_pct=None, volume_ratio=None,
               segment="exchange", benchmark=None)
    ids["night"] = st.record_alert(a4, "XKRX:2026-10-08")

    # 旧版“关口”：没有 segment / benchmark / macro_json
    st._exec("INSERT INTO alerts (created_at, symbol, ts, session_day, rule, direction, price, prev_close, window_minutes,"
             " move_pct, benchmark_move_pct, beta, abnormal_move_pct, volume_ratio, peers_json, triggers_json,"
             " is_escalation) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             ("2026-10-05T15:00:00+00:00", "MU", "2026-10-05T15:00:00+00:00", "2026-10-05", "level", -1, 100.0, 103.2,
              None, -3.1, None, None, None, None, None,
              json.dumps([{"rule": "level", "direction": -1, "move_pct": -3.1, "level_pct": 3}]), 0))
    ids["legacy"] = _last_id(st)
    # 损坏的行：触发条件读不通
    st._exec("INSERT INTO alerts (created_at, symbol, ts, session_day, rule, direction, triggers_json, is_escalation)"
             " VALUES (?,?,?,?,?,?,?,?)",
             ("2026-10-06T15:00:00+00:00", "SNDK", "2026-10-06T15:00:00+00:00", "2026-10-06", "rapid", 1, "not json", 0))
    ids["broken"] = _last_id(st)
    # 很久以前（30 天之外）
    old = _alert("MU", utc(2026, 8, 20, 15, 0), triggers=[_rapid()])
    ids["old"] = st.record_alert(old, "2026-08-20")
    return ids


def _last_id(st: Storage) -> int:
    return int(st.conn.execute("SELECT MAX(id) FROM alerts").fetchone()[0])


@pytest.fixture
def seeded(settings, calendar):
    st = Storage(":memory:")
    ids = seed(st, calendar)
    return st, ids


# ───────── 列表 ─────────


async def test_list_renders_grouped_rows(settings, calendar, seeded):
    st, ids = seeded
    ctx = make_ctx(settings, storage=st, now=NOW)
    async with client(ctx) as c:
        r = await c.get("/alerts")
    assert r.status_code == 200
    html = r.text
    # 默认 7 天：10-03 之后的都在，08-20 的不在
    assert f'data-href="/alerts/{ids["mu"]}"' in html and f'data-href="/alerts/{ids["old"]}"' not in html
    assert f'data-href="/alerts/{ids["legacy"]}"' in html and f'data-href="/alerts/{ids["broken"]}"' in html
    # 按本地日期分组，新的在前
    assert html.index("2026-10-09 周五") < html.index("2026-10-08 周四") < html.index("2026-10-05 周一")
    assert "同一波" in html and f'href="/alerts/{ids["mu"]}" class="small">见 #{ids["mu"]}' in html
    assert "同业盘中下调指引" in html and "badge-accent\">板块" in html
    assert '板块 <span class="down">-1.3%</span> · 个股 <span class="down">-1.9%</span>' in html  # β 1.3 × 基准 -1.0
    assert "盘后" in html and "首尔" in html and "夜间信号" in html  # 时段、只盯盘的标签
    assert "扩大" in html
    assert "关口" in html  # 旧版规则照常显示
    assert "WDC 指引" in html  # 标注
    assert 'href="/alerts.csv"' in html


async def test_list_filters(settings, calendar, seeded):
    st, ids = seeded
    ctx = make_ctx(settings, storage=st, now=NOW)

    def present(html, key):
        return f'data-href="/alerts/{ids[key]}"' in html

    async with client(ctx) as c:
        today = (await c.get("/alerts?range=today")).text
        assert present(today, "mu") and present(today, "skhy") and not present(today, "sndk_post")
        allr = (await c.get("/alerts?range=all")).text
        assert present(allr, "old")
        thirty = (await c.get("/alerts?range=30d")).text
        assert not present(thirty, "old") and present(thirty, "legacy")
        mu = (await c.get("/alerts?symbol=MU&range=all")).text
        assert present(mu, "mu") and present(mu, "legacy") and present(mu, "old") and not present(mu, "skhy")
        assert 'value="MU" selected' in mu
        post = (await c.get("/alerts?seg=post")).text
        assert present(post, "sndk_post") and not present(post, "mu")
        regular = (await c.get("/alerts?seg=regular")).text
        assert present(regular, "legacy") and present(regular, "mu")  # 旧记录没有 segment：算盘中
        day = (await c.get("/alerts?rule=day")).text
        assert present(day, "mu") and present(day, "legacy") and present(day, "night") and not present(day, "skhy")
        rapid = (await c.get("/alerts?rule=rapid")).text
        assert present(rapid, "skhy") and present(rapid, "mu") and not present(rapid, "legacy")
        idio = (await c.get("/alerts?rule=idio")).text
        assert "这个筛选条件下没有异动" in idio
        yes = (await c.get("/alerts?labeled=yes")).text
        assert present(yes, "mu") and not present(yes, "skhy")
        no = (await c.get("/alerts?labeled=no")).text
        assert not present(no, "mu") and present(no, "skhy")
        # 不认识的参数值当作没写
        odd = await c.get("/alerts?range=99y&seg=moon&rule=level&labeled=maybe&symbol=" + "X" * 300)
        assert odd.status_code == 200
        # 筛选保留在链接里：切换范围时带着代码
        assert "/alerts?range=30d&amp;symbol=MU" in mu
        assert "/alerts.csv?range=all&amp;symbol=MU" in mu


def test_filters_query_and_since(settings):
    f = Filters.parse({"range": "30d", "symbol": "MU", "seg": "pre", "rule": "day", "labeled": "no"})
    assert f.query() == "?range=30d&symbol=MU&seg=pre&rule=day&labeled=no"
    assert f.query(symbol="", range="7d") == "?seg=pre&rule=day&labeled=no"
    assert Filters.parse(None).range == "7d"
    # “今天”按本地日期：10-09 15:00 CDT → 10-09 00:00 CDT = 05:00 UTC
    assert Filters(range="today").since(NOW, settings.tz) == utc(2026, 10, 9, 5, 0)
    assert Filters(range="all").since(NOW, settings.tz) is None


async def test_list_empty_db_and_cap(settings, calendar, monkeypatch):
    ctx = make_ctx(settings, now=NOW)
    async with client(ctx) as c:
        r = await c.get("/alerts")
        assert r.status_code == 200 and "还没有异动记录" in r.text
        csv = await c.get("/alerts.csv")
        assert csv.status_code == 200 and csv.content.startswith(b"\xef\xbb\xbf") and csv.text.count("\n") == 1
    st = Storage(":memory:")
    seed(st, calendar)
    from tickersentinel.web.pages import alerts as page

    monkeypatch.setattr(page, "MAX_ROWS", 2)
    async with client(make_ctx(settings, storage=st, now=NOW)) as c:
        html = (await c.get("/alerts?range=all")).text
    assert "只显示最近 2 条" in html and html.count('class="clickable') == 2


# ───────── CSV ─────────


async def test_csv_export(settings, calendar, seeded):
    st, ids = seeded
    ctx = make_ctx(settings, storage=st, now=NOW)
    async with client(ctx) as c:
        r = await c.get("/alerts.csv?range=all")
        mu = await c.get("/alerts.csv?symbol=MU&range=all")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="alerts-20261009.csv"' == r.headers["content-disposition"]
    assert r.content.startswith(b"\xef\xbb\xbf")
    import csv
    import io

    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert rows[0][:6] == ["id", "本地时间", "代码", "名称", "时段", "规则"] and rows[0][-2:] == ["标注", "备注"]
    assert len(rows) == 1 + 7
    by_id = {int(x[0]): x for x in rows[1:]}
    mu_row = by_id[ids["mu"]]
    assert mu_row[1] == "2026-10-09 10:30 CDT" and mu_row[2] == "MU" and mu_row[4] == "盘中" and mu_row[5] == "急涨急跌"
    assert mu_row[6] == "跌" and mu_row[7] == "-3.20" and mu_row[8] == f"{(104 / 110 - 1) * 100:.2f}"
    assert mu_row[10] == "SOXX" and mu_row[11] == "1.30" and mu_row[12] == "-1.30" and mu_row[13] == "-1.90"
    assert mu_row[15] == "同业盘中下调指引，板块普跌" and mu_row[16] == "板块"
    assert mu_row[17] == 'WDC 指引, 板块"联动"'  # 逗号、引号由 csv 转义，读回来一字不差
    assert mu_row[18].startswith("'=")  # 公式注入防护
    assert by_id[ids["legacy"]][5] == "关口（旧）" and by_id[ids["legacy"]][4] == "盘中"
    assert by_id[ids["broken"]][2] == "SNDK"  # 损坏的行也导出（字段尽量填）
    mu_rows = list(csv.reader(io.StringIO(mu.content.decode("utf-8-sig"))))[1:]
    assert {x[2] for x in mu_rows} == {"MU"} and len(mu_rows) == 3


# ───────── 详情 ─────────


async def test_detail_renders_all_cards(settings, calendar, seeded):
    st, ids = seeded
    # 前后 24 小时的日程：一条已经发生的宏观数据、一条明天的财报（日期而已）
    st.upsert_event(Event(kind="macro", symbol="US", day=TARGET_DAY, title="美国 9 月 CPI", key="macro:cpi",
                          at=utc(2026, 10, 9, 12, 30), confirmed=True, source="bls"), now=NOW)
    st.upsert_event(Event(kind="earnings", symbol="WDC", day=date(2026, 10, 10), title="西部数据 季度业绩",
                          key="earnings:WDC:2026Q4", time_hint="盘后", source="yfinance",
                          url="https://ir.example/wdc"), now=NOW)
    st.upsert_event(Event(kind="earnings", symbol="ZZZZ", day=TARGET_DAY, title="不在范围里的公司", key="earnings:ZZZZ:x",
                          source="yfinance"), now=NOW)
    ctx = make_ctx(settings, storage=st, now=NOW)
    async with client(ctx) as c:
        r = await c.get(f"/alerts/{ids['mu']}")
    assert r.status_code == 200
    html = r.text
    # 头部
    assert "<h1>MU</h1>" in html and "美光 Micron" in html and "2026-10-09 10:30 CDT" in html and "美东 11:30 EDT" in html
    assert "20分钟急跌 3.2 个点" in html  # 标题触发
    assert "全天：较昨收跌超 5%" in html  # 第二个触发
    assert "$104.00" in html
    # 上一条 / 下一条（按时间）
    assert f'href="/alerts/{ids["skhy"]}"' in html  # 下一条（更晚）
    assert f'href="/alerts/{ids["sndk_post"]}"' in html  # 上一条（更早）
    # 拆分
    assert "-1.30%" in html and "SOXX -1.00% × β1.30" in html and "-1.90%" in html and "2.4 倍" in html
    assert "以个股因素为主" in html or "板块与个股因素兼有" in html
    assert "SNDK" in html and "-2.0%" in html  # 同期
    # 宏观：名称来自配置，板块指标单列，休市时显示期货的叫法
    assert "美债10年" in html and "4.12%" in html and "+3bp" in html
    assert "标普期货" in html and "较美股收盘" in html
    assert "美元/韩元" in html and "存储芯片" in html
    assert "2s10s" in html and "+49bp" in html
    # 归因：结构化 + 链接从推送原文里还原 + 日程 + 无法还原的 #n
    assert "同业盘中下调指引，板块普跌" in html and "主导：板块" in html
    assert 'href="https://reuters.example/a" target="_blank" rel="noopener noreferrer">Reuters' in html
    assert 'href="https://search.example/b"' in html
    assert "日程：美国 CPI" in html and "候选新闻 #4" in html
    assert "明天美光的业绩说明会" in html and "推送里的归因原文" in html
    # 跟帖
    assert "同一波的跟帖" in html and f'href="/alerts/{ids["skhy"]}">#{ids["skhy"]} SKHY' in html
    # 前后日程
    assert "美国 9 月 CPI" in html and "异动前 3 小时" in html
    assert "西部数据 季度业绩" in html and 'href="https://ir.example/wdc"' in html and "（预计）" in html
    assert "不在范围里的公司" not in html
    # 标注表单（已有内容回填、转义）
    assert f'hx-post="/api/alerts/{ids["mu"]}/label"' in html
    assert "WDC 指引, 板块&#34;联动&#34;" in html or "WDC 指引, 板块&quot;联动&quot;" in html
    assert 'id="alert-chart"' in html and "lightweight-charts" in html


async def test_detail_same_wave_post_legacy_broken_and_404(settings, calendar, seeded):
    st, ids = seeded
    ctx = make_ctx(settings, storage=st, now=NOW)
    async with client(ctx) as c:
        follow = (await c.get(f"/alerts/{ids['skhy']}")).text
        assert "这条是同一波行情的跟帖" in follow and f'href="/alerts/{ids["mu"]}">#{ids["mu"]} MU' in follow
        assert "同业盘中下调指引" in follow  # 顺着取第一条的结论
        post = (await c.get(f"/alerts/{ids['sndk_post']}")).text
        assert "盘后" in post and "较今日收盘" in post and "扩大" in post and "未送达" in post
        assert 'href="https://ir.example/q3"' in post  # 非结构化归因按 Markdown 显示
        assert "推送这条时没有附宏观快照" in post
        night = await c.get(f"/alerts/{ids['night']}")
        assert night.status_code == 200 and "₩250,000" in night.text and "没有板块拆分" in night.text
        assert "还没有归因" in night.text
        legacy = await c.get(f"/alerts/{ids['legacy']}")
        assert legacy.status_code == 200 and "跌破较昨收 -3% 关口" in legacy.text
        broken = await c.get(f"/alerts/{ids['broken']}")
        assert broken.status_code == 200 and "记录不完整" in broken.text
        for bad in ("/alerts/999999", "/alerts/abc", "/alerts/-1"):
            r = await c.get(bad)
            assert r.status_code == 404 and "页面不存在" in r.text, bad
        assert (await c.get("/api/alerts/999999/bars")).status_code == 404
        assert (await c.get("/api/alerts/x/bars")).status_code == 404


def test_parse_helpers_tolerate_odd_data(settings):
    assert parse_macro(None, settings) is None and parse_macro("{bad", settings) is None
    assert parse_macro(json.dumps({"at": "x", "foo": {"value": "nan?"}}), settings) is None
    m = parse_macro(json.dumps({"unknown": {"symbol": "ZZ", "value": 12.5, "change": None}}), settings)
    assert m["groups"][0][1][0]["name"] == "unknown" and m["groups"][0][1][0]["change"] == "—"
    a = parse_attribution({"attribution_json": json.dumps({"causes": "oops", "same_wave_as": "x"}),
                           "attribution_text": "**结论**：只有原文"})
    assert a["summary"] == "只有原文" and a["same_wave_as"] is None and a["causes"] == []
    a = parse_attribution({"attribution_json": "[1, 2]", "attribution_text": None})
    assert a["summary"] is None
    v = to_view({"id": 1, "symbol": "MU", "ts": "garbage", "triggers_json": "[]", "direction": None}, settings)
    assert v["broken"] is True


# ───────── 标注 ─────────


async def test_label_post_requires_token_and_persists(settings, calendar, seeded):
    st, ids = seeded
    ctx = make_ctx(settings, storage=st, now=NOW)
    url = f"/api/alerts/{ids['skhy']}/label"
    async with client(ctx) as c:
        assert (await c.post(url, data={"label": "x"})).status_code == 403
        assert (await c.post(url, data={"label": "x"}, headers={"X-Tsen-Token": "wrong"})).status_code == 403
        r = await c.post(url, data={"label": "  海力士跟随美光  ", "notes": "归因对"}, headers=write_headers(ctx))
        assert r.status_code == 200 and "已保存 15:00 CDT" in r.text and "预览模式" not in r.text
        row = st.alert_row(ids["skhy"])
        assert row["label"] == "海力士跟随美光" and row["notes"] == "归因对"
        # 清空 = 删除（NULL）
        r = await c.post(url, data={"label": "", "notes": "   "}, headers=write_headers(ctx))
        assert r.status_code == 200
        row = st.alert_row(ids["skhy"])
        assert row["label"] is None and row["notes"] is None
        # JSON 也收
        r = await c.post(url, json={"label": "<b>粗</b>", "notes": None},
                         headers={**write_headers(ctx), "Accept": "application/json"})
        assert r.status_code == 200 and r.json()["label"] == "<b>粗</b>" and r.json()["persisted"] is True
        page = (await c.get(f"/alerts/{ids['skhy']}")).text
        assert "&lt;b&gt;粗&lt;/b&gt;" in page and "<b>粗</b>" not in page  # 转义
        # 太长、类型不对、坏 JSON、不存在
        r = await c.post(url, data={"label": "长" * 2001}, headers=write_headers(ctx))
        assert r.status_code == 422 and "最多 2000 字" in r.json()["detail"]
        r = await c.post(url, data={"label": "长" * 2001}, headers={**write_headers(ctx), "HX-Request": "true"})
        assert r.status_code == 422 and r.text.startswith("真实原因太长")  # htmx：短文字直接显示
        assert (await c.post(url, json={"label": 5}, headers=write_headers(ctx))).status_code == 422
        r = await c.post(url, content=b"{bad", headers={**write_headers(ctx), "Content-Type": "application/json"})
        assert r.status_code == 400
        assert (await c.post("/api/alerts/999999/label", data={"label": "x"}, headers=write_headers(ctx))).status_code == 404
        assert st.alert_row(ids["skhy"])["label"] == "<b>粗</b>"  # 失败的请求没有改动


async def test_label_post_rejects_unreadable_bodies_and_keeps_missing_fields(settings, calendar, seeded):
    st, ids = seeded
    ctx = make_ctx(settings, storage=st, now=NOW)
    aid = ids["skhy"]
    url = f"/api/alerts/{aid}/label"
    st.set_alert_label(aid, "原有的真实原因", "原有备注")

    def kept():
        row = st.alert_row(aid)
        return row["label"] == "原有的真实原因" and row["notes"] == "原有备注"

    async with client(ctx) as c:
        # multipart（fetch + FormData）读不懂：415，不能当成“两项都清空”
        r = await c.post(url, files={"label": (None, "新的原因")}, headers=write_headers(ctx))
        assert r.status_code == 415 and kept()
        r = await c.post(url, content=b"label=x", headers={**write_headers(ctx), "Content-Type": "text/plain"})
        assert r.status_code == 415 and kept()
        r = await c.post(url, content=b"", headers=write_headers(ctx))  # 没有 Content-Type
        assert r.status_code == 415 and kept()
        # 空表单、空 JSON、只有无关字段：400
        for body, ctype in ((b"", "application/x-www-form-urlencoded"), (b"{}", "application/json"),
                            (b"foo=1", "application/x-www-form-urlencoded"), (b"", "application/json")):
            r = await c.post(url, content=body, headers={**write_headers(ctx), "Content-Type": ctype})
            assert r.status_code == 400, (body, ctype)
            assert kept()
        r = await c.post(url, content=b"", headers={**write_headers(ctx), "HX-Request": "true",
                                                    "Content-Type": "application/x-www-form-urlencoded"})
        assert r.status_code == 400 and "label" in r.text
        # 只带一项：另一项保持原样
        r = await c.post(url, json={"label": "只改原因"}, headers=write_headers(ctx))
        assert r.status_code == 200
        row = st.alert_row(aid)
        assert row["label"] == "只改原因" and row["notes"] == "原有备注"
        r = await c.post(url, data={"notes": "只改备注"},
                         headers={**write_headers(ctx), "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
        assert r.status_code == 200
        row = st.alert_row(aid)
        assert row["label"] == "只改原因" and row["notes"] == "只改备注"
        # 明确给 null / 空字符串才清空
        r = await c.post(url, json={"notes": None}, headers=write_headers(ctx))
        assert r.status_code == 200
        row = st.alert_row(aid)
        assert row["label"] == "只改原因" and row["notes"] is None
        r = await c.post(url, data={"label": ""}, headers=write_headers(ctx))
        assert r.status_code == 200 and st.alert_row(aid)["label"] is None


async def test_nearby_events_split_by_actual_order(settings, calendar, seeded):
    """归因把异动后 5 分钟内的日程算作“已发生”；页面按实际先后放进“异动后”。"""
    from tickersentinel.web.alerts_data import nearby_events

    st, ids = seeded
    row = st.alert_row(ids["mu"])
    alert = to_view(row, settings)["alert"]
    st.upsert_event(Event(kind="macro", symbol="US", day=TARGET_DAY, title="稍后的讲话", key="macro:late",
                          at=alert.ts + timedelta(minutes=3), confirmed=True, source="fed"), now=NOW)
    st.upsert_event(Event(kind="macro", symbol="US", day=TARGET_DAY, title="同时的数据", key="macro:same",
                          at=alert.ts + timedelta(seconds=20), confirmed=True, source="bls"), now=NOW)
    st.upsert_event(Event(kind="macro", symbol="US", day=TARGET_DAY, title="早些的数据", key="macro:early",
                          at=alert.ts - timedelta(hours=1), confirmed=True, source="bls"), now=NOW)
    ctx = make_ctx(settings, storage=st, now=NOW)
    before, ahead = nearby_events(ctx, alert)
    assert [e["title"] for e in before if e["kind"] == "macro"] == ["早些的数据", "同时的数据"]
    assert ahead[0]["title"] == "稍后的讲话" and ahead[0]["rel"] == "异动后 3 分钟"
    assert all("异动后" not in e["rel"] for e in before)


async def test_label_in_preview_mode_says_not_persisted(settings, calendar, seeded):
    st, ids = seeded
    ctx = make_ctx(settings, storage=st, now=NOW, mode="preview")
    async with client(ctx) as c:
        r = await c.post(f"/api/alerts/{ids['mu']}/label", data={"label": "x"}, headers=write_headers(ctx))
    assert r.status_code == 200 and "预览模式" in r.text


# ───────── 走势图数据 ─────────


def _shifted(ts: datetime, settings) -> int:
    return int(ts.timestamp() + ts.astimezone(settings.tz).utcoffset().total_seconds())


async def test_bars_symbol_benchmark_peers_and_refs(settings, calendar):
    market = build_market(calendar)
    sess = market.target
    st = Storage(":memory:")
    ts = at(sess, 120)
    prev_close = float(market.daily["MU"]["close"].iloc[-2])  # 10-08 收盘
    alert = _alert("MU", ts, triggers=[_rapid(anchor=ts - timedelta(minutes=20), window=30),
                                       Trigger(rule="day", direction=-1, move_pct=-5.4, level_pct=5.0)],
                   prev_close=prev_close, price=float(market.intraday["MU"]["close"].loc[ts]))
    aid = st.record_alert(alert, "2026-10-09")
    prices = FakePriceSource(market)
    ctx = make_ctx(settings, storage=st, prices=prices, now=NOW)
    async with client(ctx) as c:
        r = await c.get(f"/api/alerts/{aid}/bars")
    d = r.json()
    assert d["available"] is True and d["tz_label"] == "CDT" and d["ref_label"] == "较昨收"
    roles = {s["key"]: s["role"] for s in d["series"]}
    assert roles["MU"] == "symbol" and roles["SOXX"] == "benchmark"
    assert [s["role"] for s in d["series"]][0] == "symbol"
    assert 1 <= sum(1 for r_ in roles.values() if r_ == "peer") <= 3  # 同主题的 SNDK / SKHY / WDC，最多 3 只
    mu = d["series"][0]
    # 这只资产按入库的参照价换算
    p0 = mu["points"][0]
    first_ts = datetime.fromtimestamp(p0["time"], UTC) - (sess.open.astimezone(settings.tz).utcoffset())
    assert mu["ref"] == pytest.approx(prev_close, rel=1e-6)
    close0 = float(market.intraday["MU"]["close"].loc[first_ts])
    assert p0["value"] == pytest.approx((close0 / prev_close - 1) * 100, abs=1e-3)
    # 窗口：异动前 3 小时（这里被开盘截住，合成行情没有盘前）到异动后 90 分钟
    assert p0["time"] == _shifted(sess.open, settings)
    assert mu["points"][-1]["time"] == _shifted(ts + timedelta(minutes=90), settings)
    # 基准按日线的昨收
    soxx = next(s for s in d["series"] if s["key"] == "SOXX")
    assert soxx["ref"] == pytest.approx(float(market.daily["SOXX"]["close"].iloc[-2]))
    # 标记：异动时刻、起点、检测窗口起点（都已经换成本地墙上时间）
    assert d["marker"]["time"] == _shifted(ts, settings) and d["marker"]["direction"] == -1
    assert d["marker"]["text"] == "20分钟急跌 3.2 个点"
    assert d["anchor_time"] == _shifted(ts - timedelta(minutes=20), settings)
    assert d["window_start"] == _shifted(ts - timedelta(minutes=30), settings) and d["window_minutes"] == 30
    assert d["marker"]["time"] - int(ts.timestamp()) == -5 * 3600  # CDT = UTC-5
    # 全天累计的档位：方向向下，越过了 5%，再多画下一档
    assert [x["value"] for x in d["levels"]] == [-5.0, -8.0] and d["levels"][0]["hit"] and not d["levels"][1]["hit"]
    assert d["ref_note"] is None and d["complete"] is True

    # 缓存：数据源换掉之后仍返回同一份
    ctx.prices = None
    async with client(ctx) as c:  # 新的 app：缓存跟着 app 走
        r2 = await c.get(f"/api/alerts/{aid}/bars")
    assert r2.json()["available"] is False
    cache = alerts_chart.ChartCache()
    cache.put(aid, d)
    assert cache.get(aid) is d
    cache.put(aid + 1, {"available": False})
    assert cache.get(aid + 1) is None


async def test_bars_cache_within_app(settings, calendar):
    market = build_market(calendar)
    sess = market.target
    st = Storage(":memory:")
    ts = at(sess, 200)
    aid = st.record_alert(_alert("MU", ts, triggers=[_rapid()], prev_close=None), "2026-10-09")
    calls = []

    class Counting(FakePriceSource):
        async def intraday_bars(self, symbols, start, end=None, *, prepost=False):
            calls.append(1)
            return await super().intraday_bars(symbols, start, end, prepost=prepost)

    ctx = make_ctx(settings, storage=st, prices=Counting(market), now=NOW)
    async with client(ctx) as c:
        d1 = (await c.get(f"/api/alerts/{aid}/bars")).json()
        n = len(calls)
        d2 = (await c.get(f"/api/alerts/{aid}/bars")).json()
    assert d1 == d2 and len(calls) == n and d1["available"]
    # 没有入库的参照价（prev_close 为空）：这只资产按日线昨收
    assert d1["series"][0]["ref"] == pytest.approx(float(market.daily["MU"]["close"].iloc[-2]))


async def test_bars_with_basket_benchmark(settings, calendar):
    isolate(settings, ["MU"])
    th = settings.universe.themes["memory"]
    th.peers = {"WDC": "西部数据", "SKHY": "海力士"}
    th.benchmark = "basket"
    assert settings.universe.benchmark_for("MU") == "basket:memory"
    market = build_market(calendar)
    sess = market.target
    st = Storage(":memory:")
    ts = at(sess, 150)
    aid = st.record_alert(_alert("MU", ts, triggers=[_rapid()], benchmark="basket:memory",
                                 prev_close=float(market.daily["MU"]["close"].iloc[-2])), "2026-10-09")
    ctx = make_ctx(settings, storage=st, prices=FakePriceSource(market), now=NOW)
    async with client(ctx) as c:
        d = (await c.get(f"/api/alerts/{aid}/bars")).json()
    bench = next(s for s in d["series"] if s["role"] == "benchmark")
    assert bench["key"] == "basket:memory" and bench["label"] == "存储芯片篮子" and bench["ref"] == pytest.approx(100.0)
    # 篮子 = 成分较昨收的平均
    t0 = sess.open
    rel = [float(market.intraday[m]["close"].loc[t0]) / float(market.daily[m]["close"].iloc[-2]) - 1 for m in ("WDC", "SKHY")]
    assert bench["points"][0]["value"] == pytest.approx(100 * sum(rel) / 2, abs=1e-3)


async def test_bars_unavailable_paths(settings, calendar):
    market = build_market(calendar)
    st = Storage(":memory:")
    ts = at(market.target, 100)
    aid = st.record_alert(_alert("MU", ts, triggers=[_rapid()]), "2026-10-09")
    old = st.record_alert(_alert("MU", utc(2026, 8, 3, 15, 0), triggers=[_rapid()]), "2026-08-03")
    gone = st.record_alert(_alert("XYZQ", ts, triggers=[_rapid()], benchmark="basket:nope"), "2026-10-09")
    # 没有行情源
    async with client(make_ctx(settings, storage=st, now=NOW)) as c:
        d = (await c.get(f"/api/alerts/{aid}/bars")).json()
        assert d["available"] is False and "没有行情数据源" in d["reason"]
        page = (await c.get(f"/alerts/{aid}")).text
        assert 'data-has-prices="0"' in page
    ctx = make_ctx(settings, storage=st, prices=FakePriceSource(market), now=NOW)
    async with client(ctx) as c:
        d = (await c.get(f"/api/alerts/{old}/bars")).json()
        assert d["available"] is False and "30 天" in d["reason"]
        d = (await c.get(f"/api/alerts/{gone}/bars")).json()  # 已经移出范围、数据源也没有
        assert d["available"] is False and "没有返回" in d["reason"]

    class Boom(FakePriceSource):
        async def intraday_bars(self, *a, **k):
            raise RuntimeError("网络断了")

    async with client(make_ctx(settings, storage=st, prices=Boom(market), now=NOW)) as c:
        d = (await c.get(f"/api/alerts/{aid}/bars")).json()
    assert d["available"] is False and "网络断了" in d["reason"] and "RuntimeError" not in d["reason"]

    # yfinance 取不到分钟线时是抛 SourceError，不是返回空表：太久以前的异动照样给出“只保留约 30 天”
    from tickersentinel.sources.base import SourceError

    class NoMinutes(FakePriceSource):
        async def intraday_bars(self, *a, **k):
            raise SourceError("[yfinance] 分钟线没有返回任何数据（MU, SOXX）")

    async with client(make_ctx(settings, storage=st, prices=NoMinutes(market), now=NOW)) as c:
        d = (await c.get(f"/api/alerts/{old}/bars")).json()
        assert d["available"] is False and d["reason"] == alerts_chart.TOO_OLD_REASON
        d = (await c.get(f"/api/alerts/{aid}/bars")).json()  # 近期的：照实说取行情失败，不带异常类名
        assert d["reason"].startswith("取行情失败：[yfinance]") and "SourceError" not in d["reason"]


async def test_bars_timeout(settings, calendar):
    import asyncio

    market = build_market(calendar)
    ts = at(market.target, 100)
    alert = _alert("MU", ts, triggers=[_rapid()])

    class Slow(FakePriceSource):
        async def intraday_bars(self, *a, **k):
            await asyncio.sleep(5)

    d = await alerts_chart.build(settings, calendar, Slow(market), alert, NOW, timeout=0.05)
    assert d["available"] is False and "超时" in d["reason"]


async def test_bars_post_segment_uses_today_close(settings, calendar):
    """盘后段：基准的参照价是当天收盘（日线有当天就用日线）。合成行情没有盘后分钟线，所以只验证参照价的取法。"""
    market = build_market(calendar)
    sess = market.target
    alert = _alert("MU", sess.close + timedelta(minutes=5), triggers=[_rapid()], segment="post",
                   prev_close=float(market.daily["MU"]["close"].iloc[-1]))
    p = alerts_chart.plan(settings, calendar, alert, NOW + timedelta(hours=3))
    assert p["seg"].kind == "post" and p["bench"] == "SOXX"
    ref = alerts_chart._ref_for("SOXX", p["seg"], {}, market.daily, {})
    assert ref == pytest.approx(float(market.daily["SOXX"]["close"].iloc[-1]))


# ───────── 美股夜盘（hours: 24x5） ─────────


async def test_night_alert_chart_uses_overnight_prices_and_list_filter(settings, calendar):
    from tickersentinel.web import alerts_chart
    from test_overnight import MON, TUE, et, night_monitor

    mon, storage, _, prices = night_monitor(settings, calendar, {(("night", TUE), 200 + i): -0.007 for i in range(5)})
    now = et(MON, 20) + timedelta(minutes=206)
    (alert,) = await mon.tick(now=now)
    assert alert.segment == "night"
    before = prices.night_calls
    out = await alerts_chart.build(settings, calendar, prices, alert, now + timedelta(minutes=30))
    assert out["available"], out["reason"]
    assert prices.night_calls > before  # 夜盘的分钟线来自 overnight_bars，和检测同一套取数
    sym = next(s for s in out["series"] if s["role"] == "symbol")
    assert sym["points"] and out["marker"] is not None
    ctx = make_ctx(settings, storage=storage, calendar=calendar, now=now + timedelta(minutes=30))
    async with client(ctx) as c:
        r = await c.get("/alerts?range=all&seg=night")
        assert r.status_code == 200 and "夜盘" in r.text and f"/alerts/{alert.id}" in r.text
        assert (await c.get(f"/alerts/{alert.id}")).status_code == 200



# ───────── 宏观同期变化、走势图先查本地库（决策 21） ─────────


def _walk(idx, base, sigma, seed, *, yield_=False):
    import numpy as np
    import pandas as pd

    steps = np.random.default_rng(seed).normal(0, sigma, len(idx))
    close = base + np.cumsum(steps) if yield_ else base * np.exp(np.cumsum(steps))
    return pd.DataFrame({"close": close}, index=idx)


def macro_store(end: datetime, *, shock_from: datetime | None = None, shock: float = 0.0):
    """15 天的 ^TNX、CL=F 分钟线；shock_from 到 end 之间原油的对数收益线性加上 shock，之后保持。"""
    import numpy as np
    import pandas as pd

    from tickersentinel.barstore import BarStore

    st = BarStore(":memory:")
    idx = pd.date_range(end - timedelta(days=15), end + timedelta(minutes=30), freq="1min", tz=UTC, inclusive="left")
    tnx = _walk(idx, 4.0, 0.0004, 1, yield_=True)
    cl = _walk(idx, 80.0, 0.0004, 2)
    if shock_from is not None:
        m = (idx >= pd.Timestamp(shock_from)) & (idx <= pd.Timestamp(end))
        cl.loc[m, "close"] *= np.exp(np.linspace(0, shock, int(m.sum())))
        cl.loc[idx > pd.Timestamp(end), "close"] *= np.exp(shock)
    st.upsert("^TNX", tnx)
    st.upsert("CL=F", cl)
    return st


async def test_detail_macro_moves_card(settings, calendar, seeded, monkeypatch):
    st, ids = seeded
    row = st.alert_row(ids["mu"])
    ts = datetime.fromisoformat(row["ts"])
    # 没有分钟线库：卡片照样在，写明库没打开，链接到宏观页
    async with client(make_ctx(settings, storage=st, now=NOW)) as c:
        html = (await c.get(f"/alerts/{ids['mu']}")).text
    assert "宏观同期变化" in html and f'href="/macro?alert={ids["mu"]}"' in html
    assert "分钟线库没有打开" in html
    # 库开着但没有这段时间的数据
    from tickersentinel.barstore import BarStore

    async with client(make_ctx(settings, storage=st, now=NOW, bars=BarStore(":memory:"))) as c:
        html = (await c.get(f"/alerts/{ids['mu']}")).text
    assert "分钟线库里还没有这段时间的宏观数据" in html
    assert "10:10 – 10:30 CDT" in html  # 时间段 = [这一波的起点（异动前 20 分钟）, 异动时刻]

    # 有库：原油在这一波里涨了 2%，排第一、z 加粗；利率按 bp
    store = macro_store(ts, shock_from=ts - timedelta(minutes=20), shock=0.02)
    async with client(make_ctx(settings, storage=st, now=NOW, bars=store)) as c:
        html = (await c.get(f"/alerts/{ids['mu']}")).text
    assert "分钟线库里还没有" not in html and "z = 变化 ÷ 平常的波动" in html
    card = html[html.index("<h2>宏观同期变化"):html.index('id="label-card"')]
    assert card.index("WTI原油") < card.index("美债10年")
    assert "+2.0" in card and 'class="mm-strong"' in card and "<strong>+" in card
    assert "bp</td>" in card

    # 持仓也在库里：异动的这只（MU）本身不列，其他持仓（SNDK）照列、标“持仓”
    import pandas as pd

    idx = pd.date_range(ts - timedelta(days=15), ts + timedelta(minutes=30), freq="1min", tz=UTC, inclusive="left")
    store.upsert("MU", _walk(idx, 100.0, 0.0006, 7))
    store.upsert("SNDK", _walk(idx, 50.0, 0.0006, 8))
    async with client(make_ctx(settings, storage=st, now=NOW, bars=store)) as c:
        html = (await c.get(f"/alerts/{ids['mu']}")).text
    card = html[html.index("<h2>宏观同期变化"):html.index('id="label-card"')]
    assert "（持仓）" in card and 'title="MU' not in card and 'title="SNDK' in card

    # 计算出错：只影响这张卡片
    from tickersentinel.web import macro_data

    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(macro_data, "movers", boom)
    async with client(make_ctx(settings, storage=st, now=NOW, bars=store)) as c:
        r = await c.get(f"/alerts/{ids['mu']}")
    assert r.status_code == 200 and "宏观同期变化算不出来" in r.text and "database is locked" in r.text
    assert "<h1>MU</h1>" in r.text


def test_macro_window_bounds():
    from tickersentinel.web.pages.alerts import macro_window

    ts = utc(2026, 10, 9, 16, 0)
    a = _alert("MU", ts, triggers=[_rapid(anchor=ts - timedelta(minutes=20)),
                                   Trigger(rule="day", direction=-1, move_pct=-5.4, level_pct=5.0,
                                           anchor_ts=ts - timedelta(minutes=90), anchor_pct=0.5)])
    assert macro_window(a, ts) == (ts - timedelta(minutes=90), ts)  # 取最早的起点
    a = _alert("MU", ts, triggers=[Trigger(rule="day", direction=-1, move_pct=-5.4, level_pct=5.0)])
    assert macro_window(a, ts) == (ts - timedelta(minutes=60), ts)  # 没有起点：前 60 分钟
    a = _alert("MU", ts, triggers=[_rapid(anchor=ts - timedelta(hours=20))])
    assert macro_window(a, ts) == (ts - timedelta(hours=6), ts)  # 起点太早：最多 6 小时
    assert macro_window(None, ts) == (ts - timedelta(minutes=60), ts)


class CountingPrices(FakePriceSource):
    def __init__(self, market, *, fail: bool = False):
        super().__init__(market)
        self.intraday_calls = 0
        self.fail = fail

    async def intraday_bars(self, symbols, start, end=None, *, prepost=False):
        self.intraday_calls += 1
        if self.fail:
            from tickersentinel.sources.base import SourceError

            raise SourceError("[yfinance] 分钟线没有返回任何数据")
        return await super().intraday_bars(symbols, start, end, prepost=prepost)


def bar_store_from(market, symbols=None):
    from tickersentinel.barstore import BarStore

    st = BarStore(":memory:")
    for s, df in market.intraday.items():
        if symbols is None or s in symbols:
            st.upsert(s, df)
    return st


async def test_chart_reads_bar_store_when_it_covers(settings, calendar):
    market = build_market(calendar)
    sess = market.target
    ts = at(sess, 120)
    prev_close = float(market.daily["MU"]["close"].iloc[-2])
    alert = _alert("MU", ts, triggers=[_rapid(anchor=ts - timedelta(minutes=20))], prev_close=prev_close)
    p = alerts_chart.plan(settings, calendar, alert, NOW)
    assert set(p["fetch"]) <= set(market.intraday)
    live = await alerts_chart.build(settings, calendar, CountingPrices(market), alert, NOW)
    assert live["available"] and live["source"] == "Yahoo"

    prices = CountingPrices(market)
    d = await alerts_chart.build(settings, calendar, prices, alert, NOW, store=bar_store_from(market))
    assert d["available"] and d["source"] == "本地库" and prices.intraday_calls == 0
    assert [s["key"] for s in d["series"]] == [s["key"] for s in live["series"]]
    assert d["series"][0]["points"] == live["series"][0]["points"]
    soxx = next(s for s in d["series"] if s["key"] == "SOXX")
    assert soxx["ref"] == pytest.approx(float(market.daily["SOXX"]["close"].iloc[-2]))  # 参照价照旧用日线

    # 超过 30 天：行情源已经取不到分钟线，库里有就照样能画
    later = NOW + timedelta(days=40)
    gone = CountingPrices(market, fail=True)
    d = await alerts_chart.build(settings, calendar, gone, alert, later)
    assert not d["available"] and d["reason"] == alerts_chart.TOO_OLD_REASON
    d = await alerts_chart.build(settings, calendar, gone, alert, later, store=bar_store_from(market))
    assert d["available"] and d["source"] == "本地库" and gone.intraday_calls == 1  # 只有上面那一次

    # 库盖不住（只有 MU）：照旧找行情源
    prices = CountingPrices(market)
    d = await alerts_chart.build(settings, calendar, prices, alert, NOW, store=bar_store_from(market, {"MU"}))
    assert d["available"] and d["source"] == "Yahoo" and prices.intraday_calls > 0

    # 没有行情源、库里也没有：说明原因
    from tickersentinel.barstore import BarStore

    d = await alerts_chart.build(settings, calendar, None, alert, NOW, store=BarStore(":memory:"))
    assert not d["available"] and "本地分钟线库" in d["reason"]


async def test_chart_api_passes_bar_store(settings, calendar):
    market = build_market(calendar)
    st = Storage(":memory:")
    ts = at(market.target, 120)
    aid = st.record_alert(_alert("MU", ts, triggers=[_rapid()],
                                 prev_close=float(market.daily["MU"]["close"].iloc[-2])), "2026-10-09")
    prices = CountingPrices(market)
    ctx = make_ctx(settings, storage=st, prices=prices, now=NOW, bars=bar_store_from(market))
    async with client(ctx) as c:
        d = (await c.get(f"/api/alerts/{aid}/bars")).json()
        page = (await c.get(f"/alerts/{aid}")).text
    assert d["available"] and d["source"] == "本地库" and prices.intraday_calls == 0
    assert 'data-has-prices="1"' in page
    # 只有库、没有行情源：页面照样去取走势
    ctx = make_ctx(settings, storage=st, now=NOW, bars=bar_store_from(market))
    async with client(ctx) as c:
        assert 'data-has-prices="1"' in (await c.get(f"/alerts/{aid}")).text
