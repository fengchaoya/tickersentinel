"""宏观日程（sources/events_macro.py）：各官方来源的解析、按规则推算的日程、接入每晚汇总 / 对话 / 归因。全部离线。

tests/fixtures/macro/ 里是 2026-10-21 从官方网站下载、截取过的真实文件。
"""

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from tickersentinel.agents.attribution import Attributor
from tickersentinel.agents.chat_tools import ChatTools
from tickersentinel.config import Secrets
from tickersentinel.events.digest import EventDigest
from tickersentinel.events.hub import EventHub
from tickersentinel.llm.client import LLMClient
from tickersentinel.market_calendar import MarketCalendar
from tickersentinel.models import Alert, Event, Trigger, earnings_key
from tickersentinel.news.hub import NewsHub
from tickersentinel.sources import build_event_sources
from tickersentinel.sources.base import EventSource, SourceError
from tickersentinel.sources.events_macro import (
    BOJEvents,
    bea_events,
    bls_events,
    boj_events,
    census_events,
    fed_events,
    korea_export_events,
    macro_event,
    opex_events,
    parse_boj_schedule,
    parse_census_listview,
    parse_fed_calendar,
    parse_fomc_page,
    parse_ics,
    third_friday,
    treasury_events,
    us_market_events,
)
from tickersentinel.storage import Storage
from tickersentinel.timeutil import ET

from conftest import utc

FIX = Path(__file__).parent / "fixtures" / "macro"


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8-sig")


def et(e: Event) -> str:
    return e.at.astimezone(ET).strftime("%Y-%m-%d %H:%M")


def by_key(events: list[Event]) -> dict[str, Event]:
    return {e.key: e for e in events}


# ───────── 美联储 ─────────


def test_fomc_page_meetings_and_dot_plot_flags():
    meetings = parse_fomc_page(read("fomc_calendars.html"))
    y26 = [m for m in meetings if m.year == 2026]
    y27 = [m for m in meetings if m.year == 2027]
    assert len(y26) == 8 and len(y27) == 8
    oct26 = y26[6]
    assert (oct26.n, oct26.start, oct26.end, oct26.sep) == (7, date(2026, 10, 27), date(2026, 10, 28), False)
    assert y26[7].end == date(2026, 12, 9) and y26[7].sep  # 12 月会议带 * = 有点阵图
    assert [m.sep for m in y27] == [False, True, False, True, False, True, False, True]


def test_fomc_page_cross_month_and_skips_unscheduled():
    page = """<h4>2030 FOMC Meetings</h4>
    <div class="fomc-meeting__month col"><strong>Apr/May</strong></div><div class="fomc-meeting__date col">30-1*</div>
    <div class="fomc-meeting__month col"><strong>June</strong></div><div class="fomc-meeting__date col">8 (unscheduled)</div>
    <div class="fomc-meeting__month col"><strong>July</strong></div><div class="fomc-meeting__date col">28-29</div>"""
    ms = parse_fomc_page(page)
    assert [(m.n, m.start, m.end, m.sep) for m in ms] == [
        (1, date(2030, 4, 30), date(2030, 5, 1), True), (2, date(2030, 7, 28), date(2030, 7, 29), False)]
    assert ms[0].label == "4 月 30 日–5 月 1 日"


def test_fed_events_decisions_minutes_chair_and_estimates():
    meetings = parse_fomc_page(read("fomc_calendars.html"))
    items = parse_fed_calendar((FIX / "fed_calendar.json").read_bytes())
    evs = by_key(fed_events(meetings, items, {"fomc", "minutes", "chair"}))
    dec = evs["macro:fomc:2026-7"]
    assert et(dec) == "2026-10-28 14:00" and dec.kind == "macro" and dec.symbol == "FED" and dec.confirmed
    assert dec.title == "美联储议息决议 + 记者会" and dec.day == date(2026, 10, 28)
    assert evs["macro:fomc:2026-8"].title.endswith("（含点阵图与经济预测）")
    # 纪要：calendar.json 里有的是确认日期，“Meeting of October 27-28” 对到 10-28 的会议
    mins = evs["macro:fomc_minutes:2026-10-28"]
    assert et(mins) == "2026-11-18 14:00" and mins.confirmed and mins.title == "美联储会议纪要（10 月会议）"
    # 还没列进 calendar.json 的（2027 年 1 月会议）：会后 21 天估计
    est = evs["macro:fomc_minutes:2027-01-27"]
    assert est.day == date(2027, 2, 17) and not est.confirmed
    # 主席讲话与证词：只要主席本人，副主席（Vice Chair）的不要
    chair = [e for e in evs.values() if e.key.startswith("macro:fed_chair:")]
    assert chair and all("美联储主席 Kevin Warsh" in e.title for e in chair)
    testimony = [e for e in chair if "国会作证" in e.title]
    assert testimony and "Semiannual Monetary Policy Report" in testimony[0].title
    assert not any("Bowman" in e.title or "Jefferson" in e.title for e in evs.values())
    assert not any(e.key.startswith("macro:beige:") for e in evs.values())  # 默认不要褐皮书
    assert any(e.key.startswith("macro:beige:") for e in fed_events(meetings, items, {"beige_book"}))


def test_fed_calendar_time_parsing():
    raw = json.dumps({"events": [
        {"type": "Speeches", "title": "Speech - Chairman Kevin Warsh", "description": "&lt;p&gt;Economic Outlook&lt;/p&gt;",
         "time": "11:30 p.m.", "month": "2026-10", "days": "15"},
        {"type": "Speeches", "title": "Speech -- Chair Jerome H. Powell", "description": "", "time": "Noon",
         "month": "2024-03", "days": "7"},
        {"type": "Stat", "title": "H.15", "time": "4:15 p.m.", "month": "2026-10", "days": "1, 2, 5"},
    ]})
    items = parse_fed_calendar(raw)
    assert [(i.day.isoformat(), i.at.astimezone(ET).strftime("%H:%M")) for i in items] == [
        ("2026-10-15", "23:30"), ("2024-03-07", "12:00"), ("2026-10-01", "16:15"), ("2026-10-02", "16:15"),
        ("2026-10-05", "16:15")]
    assert items[0].description == "Economic Outlook"
    evs = fed_events([], items, {"chair"})
    assert [e.title for e in evs] == ["美联储主席 Kevin Warsh 讲话：Economic Outlook", "美联储主席 Jerome H. Powell 讲话"]


# ───────── 美国经济数据 ─────────


def test_bls_ics_releases_and_reference_months():
    evs = by_key(bls_events(parse_ics(read("bls.ics")), {"cpi", "ppi", "jobs", "jolts", "eci"}))
    cpi = evs["macro:cpi:2026-10"]
    assert et(cpi) == "2026-11-10 08:30" and cpi.title == "美国 CPI（10 月）" and cpi.symbol == "US" and cpi.source == "bls"
    assert evs["macro:jobs:2026-10"].title == "美国非农就业与失业率（10 月）" and et(evs["macro:jobs:2026-10"]) == "2026-11-06 08:30"
    jolts = evs["macro:jolts:2026-09"]  # JOLTS 比发布月早两个月
    assert et(jolts) == "2026-11-03 10:00" and jolts.title == "美国 JOLTS 职位空缺（9 月）"
    assert evs["macro:eci:2026Q3"].title == "美国就业成本指数（三季度）"
    # 州一级的 JOLTS、实际收入等不要
    assert all(k.split(":")[1] in ("cpi", "ppi", "jobs", "jolts", "eci") for k in evs)
    only_cpi = bls_events(parse_ics(read("bls.ics")), {"cpi"})
    assert {e.key.split(":")[1] for e in only_cpi} == {"cpi"}


def test_ics_folding_and_utc_times():
    text = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART:20261110T133000Z\r\nSUMMARY:Consumer Price\r\n  Index\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT\r\nDTSTART;TZID=US-Eastern:20260306T083000\r\nSUMMARY:Employment Situation\r\nEND:VEVENT\r\n")
    entries = parse_ics(text)
    assert entries[0][1] == "Consumer Price Index" and entries[0][0] == utc(2026, 11, 10, 13, 30)
    assert entries[1][0] == utc(2026, 3, 6, 13, 30)  # 3 月 6 日还是冬令时：08:30 EST = 13:30 UTC


def test_bea_gdp_stages_and_pce_months():
    evs = by_key(bea_events(json.loads(read("bea_release_dates.json")), {"gdp", "pce"}))
    assert evs["macro:gdp:2026Q3:1"].title == "美国 GDP（2026 年三季度初值）" and et(evs["macro:gdp:2026Q3:1"]) == "2026-10-29 08:30"
    assert evs["macro:gdp:2026Q3:2"].title.endswith("三季度修正值）") and evs["macro:gdp:2026Q3:3"].title.endswith("三季度终值）")
    assert evs["macro:gdp:2026Q4:1"].day == date(2027, 1, 28)
    assert evs["macro:pce:2026-09"].title == "美国 PCE 物价与个人收支（9 月）"
    assert not any("trade" in k for k in evs)  # 贸易数据不在默认范围
    assert by_key(bea_events(json.loads(read("bea_release_dates.json")), {"pce"})).keys() == {
        k for k in evs if k.startswith("macro:pce:")}


def test_census_retail_sales():
    rows = parse_census_listview(read("census_listview_2027.html"))
    evs = by_key(census_events(rows, {"retail_sales"}))
    rs = evs["macro:retail_sales:2026-12"]
    assert rs.title == "美国零售销售（12 月）" and et(rs) == "2027-01-15 08:30" and rs.source == "census"
    assert all(k.startswith("macro:retail_sales:") for k in evs)
    assert "macro:durable_goods:2026-12" in by_key(census_events(rows, {"durable_goods"}))


# ───────── 国债拍卖 ─────────


def test_treasury_auctions_filter_terms_tips_and_frn():
    items = json.loads(read("treasury_upcoming.json"))
    evs = treasury_events(items, ["2Y", "5Y", "7Y"])
    titles = sorted((e.day.isoformat(), e.title) for e in evs)
    assert titles == [("2026-10-26", "美国 2 年期国债拍卖"), ("2026-10-27", "美国 5 年期国债拍卖"), ("2026-10-29", "美国 7 年期国债拍卖")]
    assert all(et(e).endswith("13:00") and e.symbol == "UST" for e in evs)  # 还没公告的拍卖按 13:00 记
    assert treasury_events(items, ["10Y", "20Y", "30Y"]) == []  # 这一周没有长端拍卖
    fake = [{"cusip": "X1", "securityType": "Note", "type": "Note", "originalSecurityTerm": "10-Year",
             "securityTerm": "9-Year 10-Month", "competitiveAuctionCloseDate": "2026-11-10T00:00:00",
             "competitiveAuctionCloseTime": "01:00 PM", "inflationProtected": "No", "reopening": "Yes",
             "offeringAmount": "42000000000"},
            {"cusip": "X2", "securityType": "Note", "type": "TIPS", "originalSecurityTerm": "10-Year",
             "competitiveAuctionCloseDate": "2026-11-19T00:00:00", "inflationProtected": "Yes"}]
    (e,) = treasury_events(fake, ["10Y"])
    assert e.title == "美国 10 年期国债拍卖（续发，420 亿美元）" and et(e) == "2026-11-10 13:00"
    assert e.key == "macro:auction:X1:2026-11-10"


# ───────── 日本央行 ─────────


def test_boj_schedule_rows_and_outlook_flag():
    rows = parse_boj_schedule(read("boj_mpm_schedule.html"))
    y26 = [r for r in rows if r[0] == 2026]
    assert len(y26) == 8 and len([r for r in rows if r[0] == 2027]) == 8
    assert y26[6] == (2026, 7, date(2026, 10, 30), True)  # 10 月会议发布展望报告
    assert y26[7] == (2026, 8, date(2026, 12, 18), False)
    (e,) = [x for x in boj_events(rows) if x.key == "macro:boj:2026-7"]
    assert et(e) == "2026-10-29 23:00" and e.day == date(2026, 10, 29)  # 东京中午 = 美东前一晚
    assert e.title.startswith("日本央行议息决议（含展望报告") and "约为东京中午" in e.title


def test_boj_cross_month_row():
    page = ('<table><caption>Table : 2031</caption><tbody><tr><td>Apr. 30 (Wed.), May 1 (Thurs.) [PDF 1KB]</td><td>-</td>'
            '</tr><tr><td>To be announced</td></tr></tbody></table>')
    assert parse_boj_schedule(page) == [(2031, 1, date(2031, 5, 1), False)]


async def test_cached_source_keeps_last_good_result():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, text=read("boj_mpm_schedule.html"))
        return httpx.Response(503, text="down")

    src = BOJEvents(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    first = await src.collect([], date(2026, 10, 1), date(2027, 1, 31))
    assert [e.key for e in first] == ["macro:boj:2026-7", "macro:boj:2026-8", "macro:boj:2027-1"]
    src._cached_at -= 7 * 3600  # 缓存过期，下一次要重新下载，但对方 503
    assert await src.collect([], date(2026, 10, 1), date(2027, 1, 31)) == first

    broken = BOJEvents(client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html/>"))))
    with pytest.raises(SourceError):
        await broken.collect([], date(2026, 10, 1), date(2027, 1, 31))


# ───────── 按规则推算 ─────────


def test_opex_third_friday_quad_witching_and_holiday(calendar):
    assert third_friday(2026, 11) == date(2026, 11, 20)
    evs = by_key(opex_events(calendar, date(2026, 11, 1), date(2026, 12, 31)))
    assert evs["macro:opex:2026-11"].day == date(2026, 11, 20) and evs["macro:opex:2026-11"].title == "美股月度期权到期"
    assert evs["macro:opex:2026-12"].title == "美股季度期权到期（四巫日）" and evs["macro:opex:2026-12"].time_hint == "收盘"
    # 2025-04-18 是耶稣受难日（休市）：提前到周四
    assert by_key(opex_events(calendar, date(2025, 4, 1), date(2025, 4, 30)))["macro:opex:2025-04"].day == date(2025, 4, 17)


def test_us_holidays_and_early_closes(calendar):
    evs = by_key(us_market_events(calendar, date(2026, 11, 20), date(2027, 1, 5)))
    assert evs["macro:us_holiday:2026-11-26"].title == "美股休市（感恩节）"
    early = evs["macro:us_early_close:2026-11-27"]
    assert et(early) == "2026-11-27 13:00" and "13:00 收盘" in early.title
    assert "macro:us_early_close:2026-12-24" in evs and evs["macro:us_holiday:2026-12-25"].title == "美股休市（圣诞节）"
    assert evs["macro:us_holiday:2027-01-01"].title == "美股休市（元旦）"
    assert all(e.confirmed and e.symbol == "MKT" for e in evs.values())


def test_korea_exports_estimates():
    krx = MarketCalendar("XKRX")
    evs = by_key(korea_export_events(krx, date(2026, 10, 15), date(2026, 12, 5)))
    monthly = evs["macro:kr_exports:2026-10"]  # 10 月数据在 11-01（周日）09:00 KST 公布 = 美东 10-31 20:00
    assert monthly.title == "韩国 10 月进出口" and et(monthly) == "2026-10-31 20:00" and not monthly.confirmed
    d20 = evs["macro:kr_exports_20d:2026-11"]  # 11-21 是周六：顺延到周一 11-23
    assert d20.at.astimezone(MarketCalendar("XKRX").tz).date() == date(2026, 11, 23)
    assert all(date(2026, 10, 15) <= e.day <= date(2026, 12, 5) for e in evs.values())


# ───────── 组装与接入 ─────────


def test_build_sources_needs_bls_contact(settings):
    settings.events.macro.enabled = True
    names = [s.name for s in build_event_sources(settings)]
    assert {"fed", "bea", "census", "treasury", "boj", "calendar_rules"} <= set(names) and "bls" not in names
    settings.secrets = Secrets(bls_user_agent="tickersentinel test@example.com")
    assert "bls" in [s.name for s in build_event_sources(settings)]
    settings.events.macro.us_data = ["pce"]
    settings.events.macro.fed = []
    names = [s.name for s in build_event_sources(settings)]
    assert "bls" not in names and "census" not in names and "fed" not in names and "bea" in names
    settings.events.macro.enabled = False
    assert not any(s.name in ("fed", "bea", "calendar_rules") for s in build_event_sources(settings))


class StaticEvents(EventSource):
    def __init__(self, name, events):
        self.name = name
        self.events = events

    async def collect(self, symbols, start, end):
        return [e for e in self.events if start <= e.day <= end]


NOW = utc(2026, 11, 8, 2, 0)  # 11-07 周六 20:00 CST（11-01 起冬令时）


def macro_set():
    cpi = macro_event("US", "cpi:2026-10", "美国 CPI（10 月）", at=utc(2026, 11, 10, 13, 30), source="bls")
    holiday = macro_event("MKT", "us_holiday:2026-11-26", "美股休市（感恩节）", day=date(2026, 11, 26), source="calendar_rules")
    fomc = macro_event("FED", "fomc:2026-8", "美联储议息决议 + 记者会（含点阵图与经济预测）", at=utc(2026, 12, 9, 19, 0), source="fed")
    return [cpi, holiday, fomc]


async def test_digest_splits_macro_and_reports_only_reschedules(settings):
    st = Storage(":memory:")
    nvda = Event(kind="earnings", symbol="NVDA", day=date(2026, 11, 18), title="英伟达 财报", key=earnings_key("NVDA", date(2026, 11, 18)),
                 time_hint="盘后", source="yfinance")
    macro = StaticEvents("bls", macro_set())
    hub = EventHub(settings, [macro, StaticEvents("yfinance", [nvda])], st)
    digest = EventDigest(settings, hub, st, LLMClient(settings.llm, providers={}))
    await hub.refresh(NOW)
    msg = await digest.build(NOW)  # 首次汇总
    assert "**未来 14 天 · 宏观**\n- 11-10 周二 07:30 CST｜美国 CPI（10 月）" in msg.body
    assert "**未来 14 天 · 公司**\n- 11-18 周三 盘后｜英伟达 财报（预计）" in msg.body
    assert msg.body.index("· 宏观") < msg.body.index("· 公司")
    digest.mark_sent(NOW)

    # 新加的宏观日程（例如新公告的国债拍卖）不进“新增 / 变更”，只出现在日历里；明天没有事件 → 不推送
    later = NOW + timedelta(hours=12)
    macro.events.append(macro_event("UST", "auction:X:2026-11-12", "美国 30 年期国债拍卖", at=utc(2026, 11, 12, 18, 0), source="bls"))
    await hub.refresh(later)
    assert await digest.build(later) is None
    # 改期要报（例如政府停摆推迟 CPI）
    macro.events[0] = macro_event("US", "cpi:2026-10", "美国 CPI（10 月）", at=utc(2026, 11, 13, 13, 30), source="bls")
    await hub.refresh(later + timedelta(hours=1))
    msg = await digest.build(later + timedelta(hours=2))
    assert "- 改期（原 11-10）：11-13 周五 07:30 CST｜美国 CPI（10 月）" in msg.body
    assert "30 年期国债拍卖" in msg.body.split("**未来 14 天 · 宏观**")[1]


def make_alert(ts):
    tr = Trigger("rapid", -1, -2.4, window_minutes=5, z=-7.0)
    return Alert(symbol="MU", ts=ts, price=97.6, prev_close=100.0, triggers=[tr], window_minutes=5, move_pct=-2.4,
                 benchmark_move_pct=-1.5, beta=1.3, abnormal_move_pct=-0.45, volume_ratio=None, segment="pre")


async def test_attribution_prompt_lists_schedule_around_alert(settings):
    st = Storage(":memory:")
    nvda = Event(kind="earnings", symbol="NVDA", day=date(2026, 11, 9), title="英伟达 财报", key=earnings_key("NVDA", date(2026, 11, 9)),
                 time_hint="盘后", source="yfinance")
    hub = EventHub(settings, [StaticEvents("bls", macro_set()), StaticEvents("yfinance", [nvda])], st)
    await hub.refresh(NOW)
    attr = Attributor(settings, NewsHub([], settings.universe), LLMClient(settings.llm, providers={}), events=hub)
    alert = make_alert(utc(2026, 11, 10, 13, 36))  # CPI 公布 6 分钟后（盘前）
    before, ahead = attr.nearby_events(alert)
    assert [e.title for e in before] == ["英伟达 财报", "美国 CPI（10 月）"]  # 前一天盘后的财报也算
    assert ahead == []
    prompt = attr.build_prompt(alert, [])
    assert "【异动前 24 小时内的日程】" in prompt and "11-10 07:30（异动前6分钟）美国 CPI（10 月）" in prompt
    assert "[NVDA] 英伟达 财报" in prompt and "（具体时间未知）" in prompt
    # 议息前一天的异动：列在“接下来 24 小时”里
    eve = make_alert(utc(2026, 12, 8, 20, 0))
    _, ahead = attr.nearby_events(eve)
    assert [e.key for e in ahead] == ["macro:fomc:2026-8"]
    assert "【接下来 24 小时的日程】" in attr.build_prompt(eve, [])
    # 没有事件日历时不报错
    assert Attributor(settings, NewsHub([], settings.universe), LLMClient(settings.llm, providers={})).nearby_events(alert) == ([], [])


async def test_chat_tool_lists_macro_events_with_category(settings):
    st = Storage(":memory:")
    nvda = Event(kind="earnings", symbol="NVDA", day=date(2026, 11, 18), title="英伟达 财报", key=earnings_key("NVDA", date(2026, 11, 18)),
                 time_hint="盘后", source="yfinance")
    hub = EventHub(settings, [StaticEvents("bls", macro_set()), StaticEvents("yfinance", [nvda])], st)
    await hub.refresh(NOW)
    t = ChatTools(settings, st, clock=lambda: NOW, events=hub)
    out = (await t.session().execute("upcoming_events", {})).content
    assert "- 宏观 11-10 周二 07:30 CST 美国 CPI（10 月）" in out and "- NVDA 11-18 周三 盘后 英伟达 财报" in out
    macro_only = (await t.session().execute("upcoming_events", {"category": "macro", "days": 30})).content
    assert "宏观事件" in macro_only and "英伟达" not in macro_only and "感恩节" in macro_only
    company = (await t.session().execute("upcoming_events", {"category": "company"})).content
    assert "CPI" not in company and "英伟达" in company
    nvda_only = (await t.session().execute("upcoming_events", {"symbol": "英伟达"})).content
    assert "CPI" not in nvda_only and "英伟达 财报" in nvda_only
    bad = await t.session().execute("upcoming_events", {"category": "fx"})
    assert bad.is_error
