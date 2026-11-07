"""事件日历页：月历的日期归属（按本地时区）、列表分组、分类筛选、预计日期的样式、详情片段、参数容错、/api/events。全部离线。"""

import re
from datetime import date, datetime, timedelta

import pytest

pytest.importorskip("fastapi")

from tickersentinel.events.hub import LAST_REFRESH_KEY, EventHub  # noqa: E402
from tickersentinel.models import Event, earnings_key  # noqa: E402
from tickersentinel.storage import Storage  # noqa: E402
from tickersentinel.web import calendar_view as cv  # noqa: E402

from conftest import utc  # noqa: E402
from web_support import client, make_ctx  # noqa: E402

NOW = utc(2026, 10, 26, 15, 0)  # 周一 10:00 CDT


def macro(group, key, title, *, at=None, day=None, confirmed=True, source="fed", **details):
    if at is not None:
        day = day or at.date()
    return Event(kind="macro", symbol=group, day=day, title=title, key=f"macro:{key}", at=at, confirmed=confirmed,
                 source=source, details={"group": group, **details})


SKHY_EARN = Event(kind="earnings", symbol="SKHY", day=date(2026, 10, 28), title="SK海力士 三季度业绩",
                  key=earnings_key("SKHY", date(2026, 10, 28)), at=utc(2026, 10, 28, 0, 0), confirmed=True,
                  source="sec_6k", url="https://www.sec.gov/x", details={"local_time": "2026-10-28 09:00 KST",
                                                                       "period": "Q3 2026", "conference_call": True})
WDC_EARN = Event(kind="earnings", symbol="WDC", day=date(2026, 10, 29), title="西部数据 财报",
                 key=earnings_key("WDC", date(2026, 10, 29)), time_hint="盘后", confirmed=False, source="yfinance",
                 url="https://finance.yahoo.com/calendar/earnings?symbol=WDC",
                 details={"eps_avg": 1.53, "eps_low": 1.4, "eps_high": 1.7, "revenue_avg": 2.61e9})
MU_144 = Event(kind="insider_sale", symbol="MU", day=date(2026, 10, 20), title="美光 内部人拟减持：张三（CEO）10 万股",
               key="insider_sale:MU:0001", confirmed=True, source="sec_144",
               details={"person": "张三", "relationship": "CEO", "shares": 100000, "market_value_usd": 12_500_000,
                        "approx_sale_date": "2026-10-21"})
FOMC = macro("FED", "fomc:2026-7", "美联储议息决议 + 记者会", at=utc(2026, 10, 28, 18, 0), meeting="Oct 27-28")
CPI = macro("US", "cpi:2026-10", "美国 CPI（10 月）", at=utc(2026, 11, 12, 13, 30), source="bls")
HOLIDAY = macro("MKT", "us_holiday:2026-11-26", "美股休市（感恩节）", day=date(2026, 11, 26), source="calendar_rules")
KR_EXP = macro("KR", "kr_exports:2026-10", "韩国 10 月进出口", at=utc(2026, 11, 1, 0, 0), confirmed=False,
               source="calendar_rules", note="按惯例推算")
INTC = Event(kind="earnings", symbol="INTC", day=date(2026, 10, 29), title="英特尔 财报（不在范围）",
             key=earnings_key("INTC", date(2026, 10, 29)), confirmed=True, source="yfinance")
EVIL = Event(kind="custom", symbol="MU", day=date(2026, 10, 30), title="<script>alert(1)</script>发布会",
             key="custom:MU:evil", confirmed=True, source="config", url="javascript:alert(1)",
             details={"note": "<b>粗</b>"})

ALL = [SKHY_EARN, WDC_EARN, MU_144, FOMC, CPI, HOLIDAY, KR_EXP, INTC, EVIL]


def seeded(events=ALL) -> Storage:
    st = Storage(":memory:")
    for e in events:
        st.upsert_event(e, now=NOW - timedelta(days=10))
    st.kv_set(LAST_REFRESH_KEY, (NOW - timedelta(hours=3)).isoformat())
    return st


def ctx_for(settings, st=None, *, hub=True, now=NOW):
    st = st or seeded()
    return make_ctx(settings, storage=st, events=EventHub(settings, [], st) if hub else None, now=now)


def cell(html: str, day: str) -> str:
    """月历里某一天那一格的 HTML。"""
    i = html.rindex('<div class="cal-cell', 0, html.index(f'data-day="{day}"'))
    j = html.find('<div class="cal-cell', i + 10)
    return html[i: j if j > 0 else html.index("</dialog>")]


# ───────── 数据整理 ─────────


def test_classify_and_local_day(settings):
    tz = settings.tz
    c = cv.classify(SKHY_EARN, settings, tz)
    assert c.local_day == date(2026, 10, 27)  # 首尔 10-28 09:00 = 芝加哥 10-27 19:00
    assert (c.category, c.tone, c.holding, c.time_label) == ("company", "hold", True, "19:00")
    assert "CDT" in c.tooltip
    w = cv.classify(WDC_EARN, settings, tz)
    assert (w.tone, w.holding, w.time_label, w.confirmed) == ("co", False, "盘后", False)
    assert w.company.startswith("WDC") and "西部数据" in w.company and "预计" in w.tooltip
    f = cv.classify(FOMC, settings, tz)
    assert (f.category, f.tone, f.tag, f.time_label) == ("macro", "fed", "美联储", "13:00")
    assert cv.classify(HOLIDAY, settings, tz).sort_minutes == -1  # 全天的排最前
    assert cv.classify(MU_144, settings, tz).tone == "insider"


def test_period_names():
    assert cv.period_cn("Q3 2026") == "2026 财年三季度" and cv.period_cn("2026Q3") == "2026 财年三季度"
    assert cv.period_cn("FY 2027") == "2027 财年全年" and cv.period_cn("2027年3月期") == "2027年3月期"


def test_params_fall_back_to_defaults():
    today = date(2026, 10, 26)
    assert cv.parse_month("2026-11", today) == (date(2026, 11, 1), True)
    assert cv.parse_month("2026-1", today) == (date(2026, 1, 1), True)
    for bad in ("2026-13", "abc", "1999-01", "2026-00", "2026-11-01"):
        assert cv.parse_month(bad, today) == (date(2026, 10, 1), False), bad
    assert cv.parse_month(None, today) == (date(2026, 10, 1), True)
    assert cv.parse_days("60") == 60 and cv.parse_days("7") == 30 and cv.parse_days("x") == 30
    assert cv.parse_cat("macro") == "macro" and cv.parse_cat("evil") == "all"
    assert cv.add_months(date(2026, 12, 1), 1) == date(2027, 1, 1)
    assert cv.add_months(date(2026, 1, 1), -1) == date(2025, 12, 1)


def test_month_grid_shape():
    weeks = cv.month_grid(date(2026, 11, 1), date(2026, 10, 26), [])
    assert weeks[0][0].day == date(2026, 10, 26) and weeks[0][0].weekend is False  # 周一开头
    assert weeks[-1][-1].day == date(2026, 12, 6) and len(weeks) == 6
    assert weeks[0][0].is_today and not weeks[0][0].in_month and weeks[0][6].weekend
    feb = cv.month_grid(date(2027, 2, 1), date(2026, 10, 26), [])  # 2027-02-01 是周一，正好 4 周
    assert len(feb) == 4 and all(d.in_month for w in feb for d in w)


# ───────── 月历 ─────────


async def test_month_view_places_events_by_local_day(settings):
    async with client(ctx_for(settings)) as c:
        r = await c.get("/calendar")
    assert r.status_code == 200
    html = r.text
    assert "2026 年 10 月" in html and "/static/css/calendar.css" in html and "/static/js/calendar.js" in html
    # 首尔早上发布的业绩落在芝加哥前一天晚上
    c27 = cell(html, "2026-10-27")
    assert "SK海力士 三季度业绩" in c27 and "19:00" in c27 and "is-hold" in c27
    assert "SK海力士" not in cell(html, "2026-10-28")
    c28 = cell(html, "2026-10-28")
    assert "美联储议息决议" in c28 and "tone-fed" in c28 and "13:00" in c28
    # 只有时段的估计日期：虚线样式，悬停提示写“预计”
    c29 = cell(html, "2026-10-29")
    assert "西部数据 财报" in c29 and "is-est" in c29 and "（预计）" in c29 and "盘后" in c29
    assert "英特尔" not in html  # 不在监控范围
    assert 'aria-current="date"' in cell(html, "2026-10-26") and "is-today" in cell(html, "2026-10-26")
    assert "is-past" in cell(html, "2026-10-20") and "拟减持" in cell(html, "2026-10-20")
    # 下个月的 11-01 在网格末尾（相邻月份，淡化）
    assert "is-out" in cell(html, "2026-11-01") and "韩国 10 月进出口" in cell(html, "2026-10-31")
    assert "日历更新于" in html and "3 小时前" in html
    assert "&lt;script&gt;" in html and "<script>alert(1)" not in html


async def test_month_view_holiday_and_navigation(settings):
    async with client(ctx_for(settings)) as c:
        html = (await c.get("/calendar?month=2026-11&cat=macro")).text
    nov26 = cell(html, "2026-11-26")
    assert "is-holiday" in nov26 and "美股休市" in nov26 and "tone-mkt" in nov26
    assert "2026 年 11 月" in html
    assert 'href="/calendar?view=month&amp;month=2026-10&amp;cat=macro"' in html  # 上个月，保留分类
    assert 'href="/calendar?view=month&amp;month=2026-12&amp;cat=macro"' in html
    assert ">今天</a>" in html  # 不在本月时显示“今天”按钮
    assert "西部数据" not in html  # 宏观筛选不显示公司事件


async def test_month_view_more_chips(settings):
    day = date(2026, 11, 3)
    evs = [macro("UST", f"auction:{i}", f"美国国债拍卖 {i}", at=datetime(2026, 11, 3, 15 + i, tzinfo=utc(2026, 1, 1).tzinfo),
                 source="treasury") for i in range(6)]
    async with client(ctx_for(settings, seeded(evs))) as c:
        html = (await c.get("/calendar?month=2026-11")).text
    c3 = cell(html, day.isoformat())
    assert c3.count("data-ev-key") == 6 and "+2 更多" in c3 and "data-cal-more" in c3


async def test_bad_month_param_falls_back(settings):
    async with client(ctx_for(settings)) as c:
        for bad in ("2026-13", "<script>", "9999-99", "x" * 500):
            r = await c.get("/calendar", params={"month": bad, "view": "nope", "cat": "bad", "days": "-1"})
            assert r.status_code == 200 and "2026 年 10 月" in r.text and "月份参数格式不对" in r.text
            assert "<script>" not in r.text.split("<body")[1].split("<script src")[0]


async def test_horizon_note(settings):
    settings.events.lookahead_days = 14
    async with client(ctx_for(settings)) as c:
        html = (await c.get("/calendar?month=2026-12")).text
        assert "只收集到 11-09" in html
        assert "只收集到" not in (await c.get("/calendar?view=list&days=14")).text
        assert "只收集到" in (await c.get("/calendar?view=list&days=30")).text


# ───────── 列表 ─────────


async def test_list_view_groups_by_local_day(settings):
    async with client(ctx_for(settings)) as c:
        html = (await c.get("/calendar?view=list&days=30")).text
    assert "未来 30 天" in html
    heads = re.findall(r'<span class="cal-day-d">(\d\d-\d\d)</span>', html)
    assert heads == sorted(heads) and heads[0] == "10-27" and "10-20" not in heads  # 已过去的不列
    assert "10-28" in heads and "10-31" in heads and "11-01" not in heads  # 首尔 11-01 09:00 = 芝加哥 10-31 晚上
    i27, i28 = html.index(">10-27<"), html.index(">10-28<")
    assert "SK海力士 三季度业绩" in html[i27:i28]
    assert "明天" in html[i27:i28] and "后天" in html[i28:i28 + 400]
    # 同一天里时间早的在前；徽章：持仓、预计、类别
    block = html[i28:html.index(">10-29<")]
    assert "美联储" in block and "13:00" in block
    wdc = html[html.index("西部数据 财报") - 1200: html.index("西部数据 财报") + 600]
    assert "预计" in wdc and "盘后" in wdc and "badge-outline" in wdc
    assert "持仓</span>" in html[i27:i28]
    assert "感恩节" not in html  # 10-26 + 30 天 = 11-25，感恩节 11-26 不在范围


async def test_list_view_categories(settings):
    async with client(ctx_for(settings)) as c:
        macro_html = (await c.get("/calendar?view=list&days=60&cat=macro")).text
        company_html = (await c.get("/calendar?view=list&days=60&cat=company")).text
        hold_html = (await c.get("/calendar?view=list&days=60&cat=holdings")).text
    assert "美联储议息决议" in macro_html and "感恩节" in macro_html and "西部数据" not in macro_html
    assert "西部数据" in company_html and "SK海力士" in company_html and "美联储议息决议" not in company_html
    hold_list = hold_html.split('class="cal-list"')[1].split("cal-foot")[0]
    assert "SK海力士" in hold_list and "西部数据" not in hold_list and "美联储" not in hold_list
    assert 'aria-current="page">持仓' in hold_html


async def test_list_view_empty_filter(settings):
    async with client(ctx_for(settings, seeded([FOMC]))) as c:
        html = (await c.get("/calendar?view=list&days=14&cat=company")).text
    assert "没有「公司」类的事件" in html


# ───────── 空库、没有 EventHub ─────────


async def test_empty_database(settings):
    ctx = make_ctx(settings, storage=Storage(":memory:"), now=NOW)
    async with client(ctx) as c:
        for path in ("/calendar", "/calendar?view=list"):
            r = await c.get(path)
            assert r.status_code == 200 and "还没有事件" in r.text and "tsen events" in r.text
            assert "日历还没有刷新过" in r.text
        data = (await c.get("/api/events")).json()
    assert data["events"] == [] and data["last_refresh"] is None


async def test_without_event_hub_reads_storage(settings):
    ctx = ctx_for(settings, hub=False)
    async with client(ctx) as c:
        html = (await c.get("/calendar")).text
        assert "SK海力士 三季度业绩" in cell(html, "2026-10-27") and "英特尔" not in html
        assert "日历更新于" in html
        data = (await c.get("/api/events?start=2026-10-27&end=2026-10-27")).json()
    assert [e["key"] for e in data["events"]] == [SKHY_EARN.key]


async def test_stale_refresh_warning(settings):
    st = seeded()
    st.kv_set(LAST_REFRESH_KEY, (NOW - timedelta(days=3)).isoformat())
    async with client(ctx_for(settings, st)) as c:
        assert "超过 1 天没有刷新" in (await c.get("/calendar")).text
    st.kv_set(LAST_REFRESH_KEY, "not-a-date")
    async with client(ctx_for(settings, st, hub=False)) as c:
        r = await c.get("/calendar")
        assert r.status_code == 200 and "日历还没有刷新过" in r.text


async def test_odd_rows_do_not_break_page(settings):
    st = seeded()
    st.conn.execute("UPDATE events SET details_json = '[1,2]' WHERE key = ?", (WDC_EARN.key,))
    st.conn.execute("UPDATE events SET details_json = '{\"group\": \"XX\"}', symbol = 'XX' WHERE key = ?", (FOMC.key,))
    st.conn.commit()
    async with client(ctx_for(settings, st)) as c:
        assert (await c.get("/calendar")).status_code == 200
        r = await c.get("/partials/calendar/event", params={"key": WDC_EARN.key})
        assert r.status_code == 200 and "西部数据" in r.text
        r = await c.get("/partials/calendar/event", params={"key": FOMC.key})
        assert r.status_code == 200 and "宏观" in r.text


async def test_macro_row_with_non_dict_details(settings):
    """宏观事件的 details 不是字典：月历照样列出，详情不报 500。"""
    st = seeded()
    st.conn.execute("UPDATE events SET details_json = '[1,2]' WHERE key = ?", (FOMC.key,))
    st.conn.commit()
    async with client(ctx_for(settings, st)) as c:
        html = (await c.get("/calendar?month=2026-10")).text
        assert "美联储议息决议" in cell(html, "2026-10-28")
        r = await c.get("/partials/calendar/event", params={"key": FOMC.key})
        assert r.status_code == 200 and "美联储议息决议" in r.text


async def test_detail_falls_back_when_classify_breaks(settings, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bad row")

    monkeypatch.setattr(cv, "classify", boom)
    async with client(ctx_for(settings)) as c:
        r = await c.get("/partials/calendar/event", params={"key": FOMC.key})
    assert r.status_code == 404 and "数据有问题" in r.text and "<html" not in r.text


async def test_holiday_marker_survives_category_filter(settings):
    async with client(ctx_for(settings)) as c:
        for cat in ("company", "holdings"):
            html = (await c.get(f"/calendar?month=2026-11&cat={cat}")).text
            nov26 = cell(html, "2026-11-26")
            assert "is-holiday" in nov26 and "美股休市" in nov26
            assert "感恩节" not in nov26  # 事件本身照样按分类筛掉，只留格子上的标记


async def test_list_range_and_tz_label(settings):
    async with client(ctx_for(settings)) as c:
        html = (await c.get("/calendar?view=list&days=14")).text
        assert "10-26 – 11-08" in html  # 今天算第 1 天，共 14 天
        assert "（CDT/CST）" in html  # 11-01 夏令时结束，跨两个缩写
        oct_html = (await c.get("/calendar?month=2026-10")).text  # 网格 09-28 – 11-01：11-01 中午已是 CST
        assert "America/Chicago 本地时间" in oct_html
        dec = (await c.get("/calendar?month=2026-12")).text
        assert "本地时间（CST）" in dec  # 12 月整屏都是 CST（上面的“日历更新于”按更新那一刻，仍是 CDT）
        assert 'role="table"' in dec and 'role="cell"' in dec and 'role="grid' not in dec


# ───────── 详情片段 ─────────


async def test_event_detail_partial(settings):
    async with client(ctx_for(settings)) as c:
        r = await c.get("/partials/calendar/event", params={"key": SKHY_EARN.key})
        assert r.status_code == 200 and "<html" not in r.text  # 片段，不带页面骨架
        t = r.text
        assert "SK海力士 三季度业绩" in t and "财报" in t and "持仓" in t and "已确认" in t
        assert "2026-10-27 周二 19:00 CDT" in t and "美东" in t and "10-27 20:00 EDT" in t
        assert "2026-10-28 09:00 KST" in t and "SEC 6-K 公告" in t and "2026 财年三季度" in t
        assert 'href="https://www.sec.gov/x"' in t and 'rel="noopener noreferrer"' in t and "明天" in t

        t = (await c.get("/partials/calendar/event", params={"key": WDC_EARN.key})).text
        assert "预计日期" in t and "Yahoo Finance" in t and "1.53（区间 1.40 ~ 1.70）" in t and "26.1 亿美元" in t
        assert "盘后" in t and "badge-outline" in t

        t = (await c.get("/partials/calendar/event", params={"key": MU_144.key})).text
        assert "张三（CEO）" in t and "10.0 万股，约 1,250 万美元" in t and "全天" not in t and "2026-10-21" in t

        t = (await c.get("/partials/calendar/event", params={"key": FOMC.key})).text
        assert "宏观 · 美联储" in t and "Oct 27-28" in t and "美联储" in t

        t = (await c.get("/partials/calendar/event", params={"key": HOLIDAY.key})).text
        assert "全天" in t and "2026-11-26 周四" in t

        t = (await c.get("/partials/calendar/event", params={"key": INTC.key})).text  # 库里有、但不在范围
        assert "不在监控范围" in t


async def test_event_detail_escapes_and_blocks_js_links(settings):
    async with client(ctx_for(settings)) as c:
        t = (await c.get("/partials/calendar/event", params={"key": EVIL.key})).text
    assert "<script>" not in t and "&lt;script&gt;alert(1)&lt;/script&gt;发布会" in t
    assert "&lt;b&gt;粗&lt;/b&gt;" in t and "<b>" not in t
    assert "javascript:" not in t  # 非 http(s) 的链接不给按钮


async def test_event_detail_unknown_key(settings):
    async with client(ctx_for(settings)) as c:
        r = await c.get("/partials/calendar/event", params={"key": "nope:<b>x</b>"})
        assert r.status_code == 404 and "找不到这个事件" in r.text and "<html" not in r.text
        assert "&lt;b&gt;x&lt;/b&gt;" in r.text
        r = await c.get("/partials/calendar/event")
        assert r.status_code == 404


# ───────── /api/events ─────────


async def test_api_events(settings):
    async with client(ctx_for(settings)) as c:
        data = (await c.get("/api/events?start=2026-10-27&end=2026-10-31")).json()
        keys = [e["key"] for e in data["events"]]
        assert keys[0] == SKHY_EARN.key and FOMC.key in keys and WDC_EARN.key in keys and INTC.key not in keys
        sk = data["events"][0]
        assert sk["local_day"] == "2026-10-27" and sk["at"].startswith("2026-10-28T00:00") and sk["holding"] is True
        assert sk["category"] == "company" and sk["source_name"] == "SEC 6-K 公告" and sk["time_label"] == "19:00"
        wdc = next(e for e in data["events"] if e["key"] == WDC_EARN.key)
        assert wdc["confirmed"] is False and wdc["time_hint"] == "盘后" and wdc["at"] is None
        assert data["timezone"] == "America/Chicago" and data["last_refresh"]

        macro_only = (await c.get("/api/events?start=2026-10-01&end=2026-11-30&cat=macro")).json()["events"]
        assert macro_only and all(e["category"] == "macro" for e in macro_only)
        hold = (await c.get("/api/events?start=2026-10-01&end=2026-11-30&cat=holdings")).json()["events"]
        assert {e["symbol"] for e in hold} <= {"MU", "SNDK", "SKHY"} and hold

        default = (await c.get("/api/events")).json()
        assert default["start"] == "2026-10-26" and default["end"] == "2026-11-24"  # 今天起 30 天（含两端）

        assert (await c.get("/api/events?start=2026-13-01")).status_code == 400
        assert (await c.get("/api/events?start=2026-11-01&end=2026-10-01")).status_code == 400
        assert (await c.get("/api/events?start=2026-01-01&end=2028-01-01")).status_code == 400


async def test_page_has_dialog_and_controls(settings):
    async with client(ctx_for(settings)) as c:
        html = (await c.get("/calendar")).text
    assert '<dialog id="cal-dialog"' in html and "data-cal-close" in html
    assert 'data-ev-key="earnings:SKHY:2026Q4"' in html
    for name in ("月历", "列表", "全部", "宏观", "公司", "持仓", "美联储", "虚线 = 预计日期"):
        assert name in html
