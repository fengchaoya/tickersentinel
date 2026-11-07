"""财报日程数据源（yfinance / JPX）的解析测试：夹具取自 2026-10-20 的真实返回并裁剪，不联网。"""

import io
import json
import logging
import zipfile
from datetime import date, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

import httpx
import pandas as pd
import pytest

import tickersentinel.sources.events_earnings as ee
from tickersentinel.sources.base import SourceError
from tickersentinel.sources.events_earnings import (
    JPX_PAGE_URL, JPXEarningsCalendar, JPXScheduleRow, YahooEarningsRow, YFinanceEarnings, build_yahoo_events,
    calendar_to_events, excel_serial_to_date, find_xlsx_links, jpx_code, jpx_row_to_event, merge_jpx_files,
    parse_jpx_schedule, parse_yahoo_schedule, read_xlsx, us_time_hint, yahoo_real_time, yahoo_row_to_event,
)

from conftest import utc

FIX = Path(__file__).parent / "fixtures" / "earnings"
NOW = utc(2026, 10, 20, 18, 0)
NAMES = {"MU": "美光", "SNDK": "闪迪", "SKHY": "SK海力士 ADR", "000660.KS": "SK海力士", "285A.T": "铠侠"}
SYMBOLS = ["MU", "SNDK", "SKHY", "WDC", "STX", "000660.KS", "285A.T", "TSM"]
MU_CALENDAR = {  # Ticker.calendar 的真实形状（2026-10-20）
    "Dividend Date": date(2026, 10, 28), "Earnings Date": [date(2026, 12, 23)],
    "Earnings High": 40.47, "Earnings Low": 35.85, "Earnings Average": 38.27861,
    "Revenue High": 65610000000, "Revenue Low": 58065000000, "Revenue Average": 61744344580,
}


def yahoo_payload() -> dict:
    return json.loads((FIX / "yahoo_schedule.json").read_text(encoding="utf-8"))


def by_key(events) -> dict:
    return {(e.symbol, e.day): e for e in events}


# ───────────────────────────── yfinance ─────────────────────────────

def test_parse_yahoo_schedule_fixture():
    rows = parse_yahoo_schedule(yahoo_payload())
    assert len(rows) == 11
    mu = next(r for r in rows if r.symbol == "MU" and r.event_type == "EAD")
    assert mu.start == utc(2026, 12, 23, 20, 0) and mu.timing == "AMC" and mu.is_estimate is True
    assert mu.event_name.startswith("Q1 2027") and mu.eps_estimate == 38.28 and mu.utc_offset == timedelta(hours=-5)
    kioxia = next(r for r in rows if r.symbol == "285A.T" and r.reported)
    assert kioxia.start == utc(2026, 7, 31, 6, 35, 2) and kioxia.eps_actual == 513.3


def test_parse_yahoo_schedule_errors():
    error = {"code": "Bad Request", "description": "include field is not a field: x"}
    with pytest.raises(SourceError, match="include field"):
        parse_yahoo_schedule({"finance": {"result": None, "error": error}})
    with pytest.raises(SourceError, match="格式"):
        parse_yahoo_schedule({"finance": {"result": []}})
    # 时间无法解析的行跳过，其余照常
    doc = {"columns": [{"id": "ticker"}, {"id": "startdatetime"}],
           "rows": [["MU", "bad"], ["WDC", "2026-10-22T20:00:00.000Z"]]}
    assert [r.symbol for r in parse_yahoo_schedule({"finance": {"result": [{"documents": [doc]}]}})] == ["WDC"]


def test_yahoo_real_time_only_for_reported_non_midnight():
    def row(start, timing="TAS", offset=-4):
        return YahooEarningsRow("MU", start, timing=timing, utc_offset=timedelta(hours=offset))

    assert yahoo_real_time(row(utc(2026, 9, 30, 20, 1)), NOW) == utc(2026, 9, 30, 20, 1)  # 已发布、精确到分
    assert yahoo_real_time(row(utc(2026, 12, 23, 20, 0), "AMC"), NOW) is None  # 未来的占位时间
    assert yahoo_real_time(row(utc(2026, 11, 3, 19, 0)), NOW) is None  # 未来的 TAS 也是占位（19:00Z）
    assert yahoo_real_time(row(utc(2026, 7, 29, 0, 0), offset=9), NOW) is None  # UTC 零点
    assert yahoo_real_time(row(utc(2026, 7, 29, 4, 0)), NOW) is None  # 美东零点
    assert yahoo_real_time(row(utc(2026, 7, 28, 15, 0), offset=9), NOW) is None  # 上市地（首尔）零点


def test_us_time_hint_boundaries():
    assert us_time_hint(utc(2026, 10, 22, 13, 29)) == "盘前"  # 09:29 EDT
    assert us_time_hint(utc(2026, 10, 22, 13, 30)) == "盘中"
    assert us_time_hint(utc(2026, 10, 22, 19, 59)) == "盘中"
    assert us_time_hint(utc(2026, 10, 22, 20, 0)) == "盘后"  # 16:00 EDT
    # 冬令时 20:00Z 是 15:00 EST（盘中）—— 所以 Yahoo 的 AMC 占位时间（20:00Z）不能拿来推算时段
    assert us_time_hint(utc(2026, 12, 23, 20, 0)) == "盘中"
    assert us_time_hint(utc(2026, 12, 23, 21, 0)) == "盘后"


def test_yahoo_events_match_public_announcements():
    rows = parse_yahoo_schedule(yahoo_payload())
    ev = by_key(build_yahoo_events(SYMBOLS, rows, {"MU": MU_CALENDAR}, NAMES, NOW))

    wdc = ev[("WDC", date(2026, 10, 22))]
    assert (wdc.time_hint, wdc.confirmed, wdc.at, wdc.title) == ("盘后", True, None, "WDC 财报")
    assert wdc.key == "earnings:WDC:2026Q4" and wdc.source == "yfinance" and wdc.details["period"] == "Q1 2027"
    assert ev[("STX", date(2026, 10, 27))].time_hint == "盘后"
    assert ev[("SNDK", date(2026, 10, 29))].confirmed and ev[("SNDK", date(2026, 10, 29))].title == "闪迪 财报"

    skhy = ev[("SKHY", date(2026, 10, 27))]  # TNS：日期（美东）对，时间不给
    assert (skhy.time_hint, skhy.confirmed, skhy.at) == ("", True, None)
    hynix = ev[("000660.KS", date(2026, 10, 27))]  # 韩国本地代码：Yahoo 的日期不可靠，不算确认
    assert hynix.confirmed is False and hynix.title == "SK海力士 财报"
    assert ev[("285A.T", date(2026, 10, 30))].confirmed is False

    mu = ev[("MU", date(2026, 12, 23))]  # 尚未公布：估计、盘后（来自 AMC，而不是 15:00 EST 的占位时间）
    assert (mu.confirmed, mu.time_hint, mu.at) == (False, "盘后", None)
    assert mu.details["eps_avg"] == 38.27861 and mu.details["revenue_avg"] == 61744344580.0  # calendar 的精确值
    assert mu.details["eps_low"] == 35.85 and mu.details["eps_high"] == 40.47
    assert ev[("TSM", date(2027, 1, 14))].time_hint == "盘前"

    mu_q3 = ev[("MU", date(2026, 9, 30))]  # 已发布：真实时间
    assert mu_q3.at == utc(2026, 9, 30, 20, 1) and mu_q3.time_hint == "盘后" and mu_q3.confirmed
    assert mu_q3.details["eps_actual"] == 33.42 and "revenue_avg" not in mu_q3.details  # 一致预期只挂在未发布的那次
    hynix_q2 = ev[("000660.KS", date(2026, 7, 29))]  # 00:00:00Z 视为只有日期，日期按首尔
    assert hynix_q2.at is None and hynix_q2.confirmed


def test_calendar_consensus_only_when_dates_match():
    row = YahooEarningsRow("MU", utc(2026, 12, 23, 20, 0), timing="AMC", is_estimate=True, event_type="EAD",
                           eps_estimate=38.28, utc_offset=timedelta(hours=-5))
    far = dict(MU_CALENDAR, **{"Earnings Date": [date(2027, 3, 20)]})
    assert "revenue_avg" not in yahoo_row_to_event(row, NAMES, NOW, far).details
    ranged = dict(MU_CALENDAR, **{"Earnings Date": [date(2026, 12, 21), date(2026, 12, 23)]})
    assert yahoo_row_to_event(row, NAMES, NOW, ranged).details["day_range"] == ["2026-12-21", "2026-12-23"]


def test_calendar_fallback_events():
    cal = {"Earnings Date": [pd.Timestamp("2026-12-24"), date(2026, 12, 17)], "Earnings Average": float("nan"),
           "Revenue Average": 6.1e10}
    (ev,) = calendar_to_events("MU", cal, NAMES)
    assert ev.day == date(2026, 12, 17) and ev.details["day_range"] == ["2026-12-17", "2026-12-24"]
    assert ev.confirmed is False and ev.time_hint == "" and ev.key == "earnings:MU:2026Q4"
    assert ev.details == {"revenue_avg": 6.1e10, "day_range": ["2026-12-17", "2026-12-24"]}  # NaN 丢弃
    assert calendar_to_events("SOXX", {}, NAMES) == []


def test_reported_row_wins_over_scheduled_row_with_same_key():
    sched = YahooEarningsRow("WDC", utc(2026, 10, 22, 20, 0), timing="AMC", is_estimate=False, event_type="EAD")
    done = YahooEarningsRow("WDC", utc(2026, 10, 22, 20, 2, 13), timing="TAS", event_type="ERA",
                            eps_estimate=4.08, eps_actual=4.4, utc_offset=timedelta(hours=-4))
    (ev,) = build_yahoo_events(["WDC"], [sched, done], {}, NAMES, now=utc(2026, 10, 23))
    assert ev.at == utc(2026, 10, 22, 20, 2, 13) and ev.details["eps_actual"] == 4.4


async def test_yfinance_collect_merges_filters_and_skips(monkeypatch):
    calls = {"schedule": [], "calendar": []}

    def fake_schedule(symbols, start, end, timeout):
        calls["schedule"].append((list(symbols), start, end))
        return yahoo_payload()

    def fake_calendar(symbol):
        calls["calendar"].append(symbol)
        if symbol == "STX":
            raise RuntimeError("boom")  # 单个代码失败只记日志
        return MU_CALENDAR if symbol == "MU" else {}

    monkeypatch.setattr(ee, "fetch_yahoo_schedule", fake_schedule)
    monkeypatch.setattr(ee, "fetch_yahoo_calendar", fake_calendar)
    monkeypatch.setattr(ee, "utcnow", lambda: NOW)
    src = YFinanceEarnings(NAMES, timeout=5)
    events = await src.collect(SYMBOLS + ["KRW=X", "^SOX"], date(2026, 10, 20), date(2026, 12, 19))
    assert calls["schedule"] == [(SYMBOLS, date(2026, 10, 20), date(2026, 12, 19))]  # 汇率、指数不查
    assert sorted(calls["calendar"]) == sorted(SYMBOLS)
    got = [(e.symbol, e.day.isoformat(), e.time_hint, e.confirmed) for e in events]
    assert got == [
        ("WDC", "2026-10-22", "盘后", True),
        ("000660.KS", "2026-10-27", "", False),
        ("SKHY", "2026-10-27", "", True),
        ("STX", "2026-10-27", "盘后", True),
        ("SNDK", "2026-10-29", "盘后", True),
        ("285A.T", "2026-10-30", "", False),
    ]  # MU 12-23、TSM 1-14 在窗口之外；已发布的历史财报也在窗口之外
    await src.aclose()


async def test_yfinance_collect_falls_back_to_calendar(monkeypatch, caplog):
    def broken_schedule(symbols, start, end, timeout):
        raise ValueError("Expecting value")  # 例如返回了非 JSON

    monkeypatch.setattr(ee, "fetch_yahoo_schedule", broken_schedule)
    monkeypatch.setattr(ee, "fetch_yahoo_calendar", lambda s: MU_CALENDAR)
    with caplog.at_level(logging.WARNING, logger="tickersentinel.sources.events_earnings"):
        events = await YFinanceEarnings(NAMES).collect(["MU"], date(2026, 12, 1), date(2026, 12, 31))
    (ev,) = events
    assert (ev.day, ev.confirmed, ev.time_hint, ev.details["eps_avg"]) == (date(2026, 12, 23), False, "", 38.27861)
    assert "改用 Ticker.calendar" in caplog.text

    def broken_calendar(symbol):
        raise ConnectionError("down")

    monkeypatch.setattr(ee, "fetch_yahoo_calendar", broken_calendar)
    with pytest.raises(SourceError, match="全部获取失败"):
        await YFinanceEarnings(NAMES).collect(["MU", "SNDK"], date(2026, 12, 1), date(2026, 12, 31))
    assert await YFinanceEarnings(NAMES).collect(["KRW=X"], date(2026, 12, 1), date(2026, 12, 31)) == []


# ───────────────────────────── JPX ─────────────────────────────

def make_xlsx(rows: list[list], *, date1904: bool = False, inline: bool = False) -> bytes:
    """最小 xlsx。字符串默认放共享字符串（inline=True 时用内联字符串）；("文字", "ふりがな") 生成带注音 rPh 的共享字符串。"""
    shared: list[str] = []

    def cell(ref: str, v) -> str:
        if v is None:
            return ""
        if isinstance(v, (int, float)):
            return f'<c r="{ref}"><v>{v}</v></c>'
        if isinstance(v, tuple):
            si = f"<si><t>{escape(v[0])}</t><rPh sb=\"0\" eb=\"1\"><t>{escape(v[1])}</t></rPh></si>"
        elif inline:
            return f'<c r="{ref}" t="inlineStr"><is><t>{escape(v)}</t></is></c>'
        else:
            si = f"<si><t>{escape(v)}</t></si>"
        shared.append(si)
        return f'<c r="{ref}" t="s"><v>{len(shared) - 1}</v></c>'

    body = "".join(
        f'<row r="{i}">' + "".join(cell(f"{chr(65 + j)}{i}", v) for j, v in enumerate(row)) + "</row>"
        for i, row in enumerate(rows, start=1)
    )
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel_ns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    pr = '<workbookPr date1904="1"/>' if date1904 else "<workbookPr/>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {ns} {rel_ns}>{pr}'
                                      '<sheets><sheet name="List" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                   'worksheet" Target="worksheets/data.xml"/></Relationships>')
        z.writestr("xl/worksheets/data.xml", f"<worksheet {ns}><sheetData>{body}</sheetData></worksheet>")
        z.writestr("xl/sharedStrings.xml", f"<sst {ns}>{''.join(shared)}</sst>")
    return buf.getvalue()


HEADER = ["決算発表予定日\nScheduled Dates for Earnings Announcements", "コード\nCode", "会社名", "Issue Name",
          "決算期末\nFiscal Year-end", "業種名", "Industry", "種別", "Fiscal Year/Quarter", "市場区分", "Market Segment"]


def daily_file(rows: list[list], **kw) -> bytes:
    """仿 kessan.xlsx（日更文件，没有 As of）。"""
    return make_xlsx([["９月に四半期末又は期末を迎えた決算発表予定会社の一覧"], [], [], [], HEADER, *rows,
                      [], ["※ 決算期変更による変則決算期中の会社を含む場合があります。"]], **kw)


def test_jpx_code():
    assert jpx_code("285A.T") == "285A" and jpx_code("6758.t") == "6758"
    assert jpx_code("MU") is None and jpx_code("000660.KS") is None and jpx_code("12345.T") is None


def test_find_xlsx_links_from_real_page():
    html = (FIX / "jpx_index.html").read_text(encoding="utf-8")
    base = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/"
    assert find_xlsx_links(html, JPX_PAGE_URL) == [
        base + "tvdivq0000001ofb-att/kessan.xlsx",
        base + "tvdivq0000001ofb-att/kessan08_1002.xlsx",
        base + "tvdivq0000001ofb-att/kessan09_1016.xlsx",
    ]


def test_parse_jpx_real_file():
    as_of, rows = parse_jpx_schedule((FIX / "jpx_kessan09_trimmed.xlsx").read_bytes())
    assert as_of == date(2026, 10, 15)
    codes = {r.code: r for r in rows}
    assert set(codes) == {"2753", "2408", "285A", "8952", "2531", "254A"}  # 数字代码存成数字；表尾注释跳过
    kioxia = codes["285A"]
    assert kioxia.day == date(2026, 10, 30) and kioxia.fiscal_year_end == date(2027, 3, 31)
    assert (kioxia.company, kioxia.company_en) == ("キオクシアホールディングス", "Kioxia Holdings Corporation")
    assert (kioxia.period, kioxia.period_en, kioxia.market, kioxia.as_of) == ("第２四半期", "Second quarter", "Prime", as_of)
    assert codes["2531"].day is None  # 未定_Undecided
    assert codes["8952"].period == "-"  # REIT


def test_read_xlsx_inline_strings_phonetics_and_1904():
    data = make_xlsx([[("会社名", "カイシャメイ"), 2753, None, "x"]], inline=False)
    rows, date1904 = read_xlsx(data)
    assert rows == [["会社名", 2753.0, None, "x"]] and date1904 is False  # 注音不混进文字；空列补 None
    rows, date1904 = read_xlsx(make_xlsx([["インライン", 1.5]], inline=True, date1904=True))
    assert rows == [["インライン", 1.5]] and date1904 is True
    assert excel_serial_to_date(46325) == date(2026, 10, 30)
    assert excel_serial_to_date(44863, date1904=True) == date(2026, 10, 30)
    with pytest.raises(ValueError):
        read_xlsx(b"not a zip")


def test_parse_jpx_daily_file_with_text_dates_and_inline_strings():
    data = daily_file([["2026/10/21", "296A", "令和アカウンティング・ホールディングス", "Reiwa", 46477.0, "サービス業",
                        "Services", "第２四半期", "Second quarter", "グロース", "Growth"],
                       [46316.0, 4929, "アジュバン", "ADJUVANT", 46466.0, "化学", "Chemicals", "第２四半期",
                        "Second quarter", "スタンダード", "Standard"]], inline=True)
    as_of, rows = parse_jpx_schedule(data)
    assert as_of is None
    assert [(r.code, r.day, r.fiscal_year_end) for r in rows] == [
        ("296A", date(2026, 10, 21), date(2027, 3, 31)), ("4929", date(2026, 10, 21), date(2027, 3, 20))]
    with pytest.raises(ValueError, match="表头"):
        parse_jpx_schedule(make_xlsx([["no header here"]]))


def test_merge_jpx_files_newest_wins():
    def r(code, day, as_of, period="第２四半期"):
        return JPXScheduleRow(code, day, fiscal_year_end=date(2027, 3, 31), period=period, as_of=as_of)

    old = (date(2026, 10, 1), [r("285A", date(2026, 11, 13), date(2026, 10, 1))])
    new = (date(2026, 10, 15), [r("285A", date(2026, 10, 30), date(2026, 10, 15)), r("2531", None, date(2026, 10, 15))])
    daily = (None, [r("2531", date(2026, 10, 21), None)])
    merged = merge_jpx_files([daily, new, old])  # 顺序无关
    assert [x.day for x in merged["285A"]] == [date(2026, 10, 30)]
    assert [x.day for x in merged["2531"]] == [date(2026, 10, 21)]  # 日更文件最新：未定 → 已定
    other_period = merge_jpx_files([new, (None, [r("285A", date(2027, 1, 29), None, period="第３四半期")])])
    assert len(other_period["285A"]) == 2  # 不同的期分别保留


def test_jpx_row_to_event():
    row = JPXScheduleRow("285A", date(2026, 10, 30), company="キオクシアホールディングス",
                         fiscal_year_end=date(2027, 3, 31), period="第２四半期", period_en="Second quarter",
                         market="Prime", as_of=date(2026, 10, 15))
    ev = jpx_row_to_event("285A.T", row, NAMES)
    assert ev.title == "铠侠 财报（2027年3月期 第2季度）" and ev.key == "earnings:285A.T:2026Q4"
    assert (ev.day, ev.at, ev.time_hint, ev.confirmed, ev.source) == (date(2026, 10, 30), None, "", True, "jpx")
    assert ev.details["fiscal_year_end"] == "2027-03-31" and ev.details["as_of"] == "2026-10-15"
    reit = JPXScheduleRow("8952", date(2026, 11, 16), fiscal_year_end=date(2026, 9, 30), period="-")
    assert jpx_row_to_event("8952.T", reit, {}).title == "8952.T 财报（2026年9月期）"
    assert jpx_row_to_event("2531.T", JPXScheduleRow("2531", None), {}) is None


def jpx_handler(files: dict[str, bytes], seen: list[str]):
    page = (FIX / "jpx_index.html").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("index.html"):
            return httpx.Response(200, content=page, headers={"content-type": "text/html; charset=utf-8"})
        name = request.url.path.rsplit("/", 1)[-1]
        if name in files:
            return httpx.Response(200, content=files[name])
        return httpx.Response(404, text="not found")

    return handler


async def test_jpx_collect_downloads_merges_and_caches(caplog):
    daily = daily_file([[46325.0, 6758, "ソニーグループ", "Sony Group", 46477.0, "電気機器", "Electric Appliances",
                         "第２四半期", "Second quarter", "プライム", "Prime"]])
    files = {"kessan.xlsx": daily, "kessan08_1002.xlsx": b"broken",
             "kessan09_1016.xlsx": (FIX / "jpx_kessan09_trimmed.xlsx").read_bytes()}
    seen: list[str] = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(jpx_handler(files, seen)))
    src = JPXEarningsCalendar(NAMES, client=client)
    with caplog.at_level(logging.WARNING, logger="tickersentinel.sources.events_earnings"):
        events = await src.collect(["MU", "285A.T", "6758.T", "2531.T"], date(2026, 10, 20), date(2026, 12, 19))
    assert "kessan08_1002.xlsx" in caplog.text  # 坏文件只记日志
    assert [(e.symbol, e.day, e.confirmed) for e in events] == [
        ("285A.T", date(2026, 10, 30), True), ("6758.T", date(2026, 10, 30), True)]  # 2531 未定，不出事件
    assert events[0].title == "铠侠 财报（2027年3月期 第2季度）" and events[1].title == "6758.T 财报（2027年3月期 第2季度）"
    n = len(seen)
    assert n == 4  # 页面 + 3 个文件
    assert await src.collect(["285A.T"], date(2026, 11, 1), date(2026, 12, 31)) == []  # 窗口外
    assert len(seen) == n  # 走缓存，不重复下载
    assert await src.collect(["MU", "000660.KS"], date(2026, 10, 1), date(2026, 12, 31)) == []  # 不是东京代码：不联网
    await client.aclose()


async def test_jpx_errors_and_stale_cache(caplog):
    seen: list[str] = []
    files = {"kessan09_1016.xlsx": (FIX / "jpx_kessan09_trimmed.xlsx").read_bytes()}
    client = httpx.AsyncClient(transport=httpx.MockTransport(jpx_handler(files, seen)))
    src = JPXEarningsCalendar(NAMES, client=client, cache_ttl=0)  # 每次都刷新
    assert [e.day for e in await src.collect(["285A.T"], date(2026, 10, 1), date(2026, 10, 31))] == [date(2026, 10, 30)]

    files.clear()  # 之后所有文件都 404：沿用旧缓存
    with caplog.at_level(logging.WARNING, logger="tickersentinel.sources.events_earnings"):
        events = await src.collect(["285A.T"], date(2026, 10, 1), date(2026, 10, 31))
    assert [e.day for e in events] == [date(2026, 10, 30)] and "沿用" in caplog.text

    fresh = JPXEarningsCalendar(NAMES, client=client)  # 从来没成功过：整个来源不可用
    with pytest.raises(SourceError, match="全部下载或解析失败"):
        await fresh.collect(["285A.T"], date(2026, 10, 1), date(2026, 10, 31))

    empty = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html></html>")))
    with pytest.raises(SourceError, match="没有找到 .xlsx"):
        await JPXEarningsCalendar(NAMES, client=empty).collect(["285A.T"], date(2026, 10, 1), date(2026, 10, 31))
    await client.aclose()
    await empty.aclose()
