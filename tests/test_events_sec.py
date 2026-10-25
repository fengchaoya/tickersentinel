"""SEC 事件源（Form 144 内部人拟减持、6-K 业绩预告）的解析测试：真实原文删减成的夹具 + httpx.MockTransport，不联网。"""

from datetime import date
from pathlib import Path

import httpx
import pytest

from tickersentinel.models import earnings_key
from tickersentinel.sources.base import SourceError
from tickersentinel.sources.events_sec import (
    Earnings6KEvents, Form144Events, display_person, fmt_shares, fmt_usd, html_to_text, parse_earnings_announcement,
    parse_form144, parse_period, period_cn, raw_xml_name, relationship_cn,
)
from tickersentinel.sources.filings_sec_edgar import SecEdgarFilings

from conftest import utc

FIXTURES = Path(__file__).parent / "fixtures" / "sec_events"
NAMES = {"MU": "美光", "SNDK": "闪迪", "SKHY": "SK海力士", "000660.KS": "SK海力士"}
CIKS = {"MU": 723125, "SNDK": 2023554, "SKHY": 2120882}
TODAY = date(2026, 10, 20)
XSL = "xsl144X01/primary_doc.xml"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def submissions(rows: list[tuple[str, str, str, str]]) -> dict:
    """rows：(form, accession, filingDate, primaryDocument)，新的在前（与 EDGAR 一致）。"""
    return {"filings": {"recent": {
        "form": [r[0] for r in rows], "accessionNumber": [r[1] for r in rows], "filingDate": [r[2] for r in rows],
        # 故意写成“多转换一次”的错误时间：事件源不应该用到它
        "acceptanceDateTime": [f"{r[2]}T23:59:00.000Z" for r in rows],
        "primaryDocument": [r[3] for r in rows], "primaryDocDescription": [""] * len(rows), "items": [""] * len(rows),
    }}}


def sec_client(subs: dict[str, dict], files: dict[str, bytes], calls: list[str],
               indexes: dict[str, list[str]] | None = None) -> SecEdgarFilings:
    """模拟 EDGAR：company_tickers / submissions（按代码）/ Archives 下的文件（键为 "<cik>/<accession无横线>/<文件名>"）。"""
    indexes = indexes or {}

    def handler(request: httpx.Request):
        path = request.url.path
        calls.append(path)
        if path.endswith("company_tickers.json"):
            return httpx.Response(200, json={str(i): {"cik_str": cik, "ticker": sym, "title": sym}
                                             for i, (sym, cik) in enumerate(CIKS.items())})
        for sym, cik in CIKS.items():
            if path.endswith(f"/CIK{cik:010d}.json"):
                return httpx.Response(200, json=subs[sym]) if sym in subs else httpx.Response(404)
        if path.startswith("/Archives/edgar/data/"):
            rel = path.removeprefix("/Archives/edgar/data/")
            if rel in files:
                return httpx.Response(200, content=files[rel])
            folder, _, name = rel.rpartition("/")
            if name == "index.json" and folder in indexes:
                items = [{"name": n, "type": "text.gif", "size": ""} for n in indexes[folder]]
                return httpx.Response(200, json={"directory": {"item": items, "name": f"/Archives/edgar/data/{folder}"}})
        return httpx.Response(404)

    return SecEdgarFilings("test me@example.org", [], min_interval=0,
                           client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


# ───────────────────────── Form 144 ─────────────────────────

def test_parse_form144_sndk():
    f = parse_form144(fixture("sndk_144_0001950047-26-009626.xml"))
    assert (f.form, f.amended, f.previous_accession) == ("144", False, None)
    assert f.issuer_cik == 2023554 and f.person == "DAVID GOECKELER"
    assert f.relationships == ("Director", "Officer")
    assert (f.shares, f.market_value, f.shares_outstanding) == (33841, 51437304.77, 146419001)
    assert f.approx_sale_date == date(2026, 9, 17) and f.notice_date == date(2026, 9, 17)
    assert f.plan_adoption_dates == (date(2026, 5, 29),)
    assert f.brokers == ("Morgan Stanley Smith Barney LLC Executive Financial Services",)
    assert f.exchange == "NASDAQ" and f.security_class == "Common"
    assert f.remarks.startswith("The securities to be sold were acquired upon the vesting")
    assert f.pct_outstanding == pytest.approx(0.02311, abs=1e-5)


def test_parse_form144_prefixed_amended_and_multi_broker():
    # 所有元素都带 ns2: 前缀的写法
    f = parse_form144(fixture("prefixed_144_0001958244-26-000662.xml"))
    assert (f.person, f.relationships, f.shares, f.market_value) == ("Jane Doe", ("Officer",), 29727, 8174925)
    assert f.brokers == ("Goldman Sachs & Co. LLC",) and f.approx_sale_date == date(2026, 10, 15)
    # 144/A：带被修订那份的 accession
    a = parse_form144(fixture("amended_144a_0001959173-26-007376.xml"))
    assert a.form == "144/A" and a.amended and a.previous_accession == "0001959173-26-007374"
    # 多个券商：数量、市值相加，出售日期取最早，券商去重
    text = fixture("sndk_144_0001950047-26-009626.xml").decode()
    block = text[text.index("<securitiesInformation>"):text.index("</securitiesInformation>") + len("</securitiesInformation>")]
    second = block.replace("33841", "1000").replace("51437304.77", "1500000").replace("09/17/2026", "09/16/2026")
    m = parse_form144(text.replace(block, block + second))
    assert (m.shares, m.market_value, m.approx_sale_date, len(m.brokers)) == (34841, 52937304.77, date(2026, 9, 16), 1)
    # 不是 144 / 坏 XML / 带 DOCTYPE 的一律 ValueError
    for bad in (b"<edgarSubmission><headerData><submissionType>4</submissionType></headerData><formData/></edgarSubmission>",
                b"<html>not xml", b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><edgarSubmission/>'):
        with pytest.raises(ValueError):
            parse_form144(bad)


def test_form144_formatting_helpers():
    assert raw_xml_name(XSL) == "primary_doc.xml" and raw_xml_name("") == "primary_doc.xml"
    assert raw_xml_name("primary_doc.xml") == "primary_doc.xml"
    assert fmt_shares(6328) == "6,328 股" and fmt_shares(33841) == "约 3.4 万股" and fmt_shares(40000) == "4 万股"
    assert fmt_shares(125_000_000) == "1.25 亿股"
    assert fmt_usd(8500) == "$8,500" and fmt_usd(175789) == "$17.6 万" and fmt_usd(51437304.77) == "$5,144 万"
    assert fmt_usd(1.25e8) == "$1.25 亿"
    assert relationship_cn(("Officer", "Director")) == "董事兼高管" and relationship_cn(("Officer",)) == "高管"
    assert relationship_cn(("Director", "Officer", "10% Stockholder")) == "董事、高管、持股 10% 以上股东"
    assert display_person("DAVID GOECKELER") == "David Goeckeler" and display_person("Deboer Scott J") == "Deboer Scott J"


async def test_form144_events_collect():
    sndk = fixture("sndk_144_0001950047-26-009626.xml")
    amended = (sndk.replace(b"<submissionType>144</submissionType>",
                            b"<submissionType>144/A</submissionType>"
                            b"<previousAccessionNumber>0001950047-26-009626</previousAccessionNumber>")
               .replace(b"<noOfUnitsSold>33841<", b"<noOfUnitsSold>30000<"))
    subs = {
        "SNDK": submissions([
            ("4", "0001950047-26-009700", "2026-09-19", "xslF345X06/form4.xml"),
            ("144/A", "0001950047-26-009650", "2026-09-18", XSL),  # 虚构的修订：数量改成 3 万股
            ("144", "0001950047-26-009626", "2026-09-17", XSL),
            ("144", "0001950047-26-005387", "2026-06-01", XSL),  # 超出 lookback：不应去下载
        ]),
        "MU": submissions([
            ("144", "0001965301-26-000160", "2026-10-15", XSL),
            ("144", "0001958244-26-000662", "2026-10-15", XSL),  # 发行人 CIK 不是美光：跳过
        ]),
        "SKHY": submissions([("6-K", "0001193125-26-420934", "2026-10-14", "d413172d6k.htm")]),
    }
    files = {
        "2023554/000195004726009626/primary_doc.xml": sndk,
        "2023554/000195004726009650/primary_doc.xml": amended,
        # MU 这份的 XML 不叫 primary_doc.xml：先 404，再从目录清单 index.json 里找到
        "723125/000196530126000160/form144.xml": fixture("mu_144_0001965301-26-000160.xml"),
        "723125/000195824426000662/primary_doc.xml": fixture("prefixed_144_0001958244-26-000662.xml"),
    }
    indexes = {"723125/000196530126000160": ["0001965301-26-000160-index-headers.html", "form144.xml"]}
    calls: list[str] = []
    sec = sec_client(subs, files, calls, indexes)
    src = Form144Events(sec, NAMES, lookback_days=45, today=lambda: TODAY)
    events = await src.collect(["MU", "SNDK", "SKHY", "000660.KS"], date(2026, 9, 1), date(2026, 12, 31))

    assert [(e.symbol, e.day) for e in events] == [("SNDK", date(2026, 9, 17)), ("MU", date(2026, 10, 16))]
    sndk_ev, mu_ev = events
    # 修订覆盖原通知：key 沿用原 accession，内容取修订版
    assert sndk_ev.key == "insider_sale:SNDK:0001950047-26-009626"
    assert sndk_ev.title == "闪迪 内部人拟减持（修订）：David Goeckeler（董事兼高管）3 万股（约 $5,144 万）"
    assert sndk_ev.details["accession"] == "0001950047-26-009650" and sndk_ev.details["amends"] == "0001950047-26-009626"
    assert sndk_ev.kind == "insider_sale" and sndk_ev.confirmed and sndk_ev.source == "sec_144" and sndk_ev.at is None
    # 事件日期是“大约出售日期”（10-16），不是提交日（10-15）
    assert mu_ev.key == "insider_sale:MU:0001965301-26-000160"
    assert mu_ev.title == "美光 内部人拟减持：Deboer Scott J（高管）约 2.9 万股（约 $2,605 万）"
    assert mu_ev.url == f"https://www.sec.gov/Archives/edgar/data/723125/000196530126000160/{XSL}"
    d = mu_ev.details
    assert (d["shares"], d["market_value_usd"], d["approx_sale_date"], d["filed"]) == (28545, 26050061.0, "2026-10-16", "2026-10-15")
    assert d["pct_outstanding"] == pytest.approx(0.0025) and d["plan_adoption_dates"] == ["2026-07-17"]
    assert d["broker"] == "Commonwealth Financial Network" and d["relationship"] == "高管"
    assert d["xml_url"].endswith("/000196530126000160/primary_doc.xml")
    assert not any("000195004726005387" in c or "000195004726009700" in c for c in calls)

    # 按日期过滤；原文按 accession 缓存：第二次只请求 submissions
    n = len(calls)
    later = await src.collect(["MU", "SNDK"], date(2026, 10, 1), date(2026, 10, 31))
    assert [e.symbol for e in later] == ["MU"]
    assert len(calls) - n == 2

    # 市值门槛：$3,000 万以下的不返回
    big = Form144Events(sec, NAMES, min_value_usd=3e7, lookback_days=45, today=lambda: TODAY)
    assert [e.symbol for e in await big.collect(["MU", "SNDK"], date(2026, 9, 1), date(2026, 12, 31))] == ["SNDK"]


async def test_missing_documents_are_cached_not_fatal():
    # 144 的 XML 与目录清单都不存在、6-K 主文档 404：不报错、不出事件，并且记住结果，下次不再请求
    subs = {"MU": submissions([("144", "0001965301-26-000999", "2026-10-15", XSL)]),
            "SKHY": submissions([("6-K", "0001193125-26-499999", "2026-10-15", "d1d6k.htm"),
                                 ("6-K", "0001193125-26-499998", "2026-10-15", "scan.pdf")])}  # 非 HTML：不下载
    calls: list[str] = []
    sec = sec_client(subs, {}, calls)
    f144 = Form144Events(sec, NAMES, today=lambda: TODAY)
    f6k = Earnings6KEvents(sec, NAMES, today=lambda: TODAY)
    for _ in range(2):
        assert await f144.collect(["MU"], date(2026, 9, 1), date(2026, 12, 31)) == []
        assert await f6k.collect(["SKHY"], date(2026, 9, 1), date(2026, 12, 31)) == []
    archive = [c for c in calls if c.startswith("/Archives/")]
    assert archive == ["/Archives/edgar/data/723125/000196530126000999/primary_doc.xml",
                       "/Archives/edgar/data/723125/000196530126000999/index.json",
                       "/Archives/edgar/data/2120882/000119312526499999/d1d6k.htm"]


async def test_sec_events_whole_source_failures():
    calls: list[str] = []
    # 所有代码的 submissions 都失败 → 整个来源不可用
    src = Form144Events(sec_client({}, {}, calls), NAMES, today=lambda: TODAY)
    with pytest.raises(SourceError):
        await src.collect(["MU", "SNDK"], date(2026, 9, 1), date(2026, 12, 31))
    # 只有部分代码失败 → 照常返回
    ok = Form144Events(sec_client({"MU": submissions([])}, {}, calls), NAMES, today=lambda: TODAY)
    assert await ok.collect(["MU", "SNDK"], date(2026, 9, 1), date(2026, 12, 31)) == []
    # 没有任何美股代码：什么都不做
    assert await ok.collect(["000660.KS"], date(2026, 9, 1), date(2026, 12, 31)) == []


# ───────────────────────── 6-K 业绩预告 ─────────────────────────

def test_parse_real_skhy_6ks():
    oct_ = parse_earnings_announcement(html_to_text(fixture("skhy_6k_0001193125-26-420934.htm")), date(2026, 10, 14))
    assert oct_ is not None and oct_.local_time == "2026-10-28 09:00 KST"
    assert oct_.at == utc(2026, 10, 28, 0, 0) and oct_.period == "2026Q3" and oct_.conference_call
    jul = parse_earnings_announcement(html_to_text(fixture("skhy_6k_0001193125-26-303983.htm")), date(2026, 7, 15))
    assert jul is not None and jul.at == utc(2026, 7, 29, 0, 0) and jul.period == "2026Q2"
    # 业绩当天的初步业绩 6-K 也写了“(Expected) Date / Time of Provision July 29, 2026 / 9:00 a.m.”：同一事件
    pre = parse_earnings_announcement(html_to_text(fixture("skhy_6k_0001193125-26-321989.htm")), date(2026, 7, 29))
    assert pre is not None and pre.at == jul.at and pre.period == "2026Q2"
    # 澄清传闻的 6-K：不是业绩预告
    assert parse_earnings_announcement(html_to_text(fixture("skhy_6k_0001193125-26-311226.htm")), date(2026, 7, 22)) is None
    # 签名栏的日期（10-14）不会被当成事件日期；提交日晚于电话会时不算预告
    assert parse_earnings_announcement(html_to_text(fixture("skhy_6k_0001193125-26-420934.htm")), date(2026, 10, 29)) is None


@pytest.mark.parametrize("text, local_time, at, period", [
    ("The Company will hold its earnings conference call for the third quarter of 2026 at 9:00 a.m. (Seoul Time) "
     "on October 28, 2026, followed by a Q&A at 10:00 a.m.", "2026-10-28 09:00 KST", utc(2026, 10, 28, 0, 0), "2026Q3"),
    ("XYZ will report Q3 2026 financial results after market close on October 28, 2026 and host a conference call "
     "at 4:30 p.m. Eastern Time.", "2026-10-28 16:30 EDT", utc(2026, 10, 28, 20, 30), "2026Q3"),
    ("ABC plans to hold a conference call for its 2026 4Q and annual earnings results. Date: Jan. 28 (Wed), 2027, "
     "9:00 AM KST", "2027-01-28 09:00 KST", utc(2027, 1, 28, 0, 0), "2026Q4"),
    ("Fourth-quarter fiscal 2026 results will be released on December 17, 2026 at 4:00 PM ET, followed by a webcast.",
     "2026-12-17 16:00 EST", utc(2026, 12, 17, 21, 0), "2026Q4"),  # 夏令时已结束
    ("Earnings release 2026 3Q. Date and time: 2026-10-28 16:00 (KST)", "2026-10-28 16:00 KST", utc(2026, 10, 28, 7, 0), "2026Q3"),
    ("Results for the first half of 2026 will be announced on 28 October 2026 at 08:00 CET.",
     "2026-10-28 08:00 CET", utc(2026, 10, 28, 7, 0), "2026H1"),
    ("The Company will announce its 3Q26 earnings on November 5, 2026.", "2026-11-05", None, "2026Q3"),  # 只有日期
])
def test_parse_earnings_announcement_variants(text, local_time, at, period):
    a = parse_earnings_announcement(text, date(2026, 10, 14))
    assert a is not None and (a.local_time, a.at, a.period) == (local_time, at, period)


@pytest.mark.parametrize("text", [
    "The Company held its earnings conference call on October 1, 2026 at 9:00 a.m. (Seoul Time).",  # 过去的电话会
    "Clarification regarding media reports on October 20, 2026 at 9:00 a.m. about a new fab.",  # 与业绩无关
    "The Company plans to hold a conference call for its 2026 3Q earnings results.",  # 没有日期
])
def test_parse_earnings_announcement_rejects(text):
    assert parse_earnings_announcement(text, date(2026, 10, 14)) is None


def test_period_helpers():
    assert parse_period("conference call for the 2026 3Q earnings results") == "2026Q3"
    assert parse_period("results for the fourth quarter of fiscal year 2026") == "2026Q4"
    assert parse_period("Q1 FY27 results") == "2027Q1"
    assert parse_period("FY2026 results") == "FY2026" and parse_period("nothing here") is None
    assert period_cn("2026Q3") == "2026 年三季度" and period_cn("2026H2") == "2026 年下半年" and period_cn("FY2026") == "2026 财年"


async def test_earnings_6k_events_collect():
    subs = {"SKHY": submissions([
        ("6-K", "0001193125-26-420934", "2026-10-14", "d413172d6k.htm"),
        ("6-K", "0001193125-26-394998", "2026-09-18", "d140793d6k.htm"),  # 澄清传闻
        ("6-K", "0001193125-26-321989", "2026-07-29", "d115239d6k.htm"),  # 超出 lookback：不应去下载
    ])}
    files = {
        "2120882/000119312526420934/d413172d6k.htm": fixture("skhy_6k_0001193125-26-420934.htm"),
        "2120882/000119312526394998/d140793d6k.htm": fixture("skhy_6k_0001193125-26-311226.htm"),
    }
    calls: list[str] = []
    src = Earnings6KEvents(sec_client(subs, files, calls), NAMES, lookback_days=45, today=lambda: TODAY)
    events = await src.collect(["MU", "SKHY", "000660.KS"], date(2026, 10, 1), date(2026, 12, 31))

    assert len(events) == 1
    ev = events[0]
    # 首尔 10-28 09:00 = 美东 10-27 20:00：day 取美东日期，key 按它所在的日历季度
    assert (ev.symbol, ev.day, ev.at) == ("SKHY", date(2026, 10, 27), utc(2026, 10, 28, 0, 0))
    assert ev.key == earnings_key("SKHY", date(2026, 10, 27)) == "earnings:SKHY:2026Q4"
    assert ev.title == "SK海力士 2026 年三季度业绩发布"
    assert ev.kind == "earnings" and ev.confirmed and ev.source == "sec_6k"
    assert ev.url == "https://www.sec.gov/Archives/edgar/data/2120882/000119312526420934/d413172d6k.htm"
    assert ev.details == {"period": "2026Q3", "local_time": "2026-10-28 09:00 KST", "conference_call": True,
                          "accession": "0001193125-26-420934", "filed": "2026-10-14", "url": ev.url}
    assert not any("000119312526321989" in c for c in calls)

    # 识别结果按 accession 缓存（包括“不是预告”）：第二次只请求 submissions；日期区间之外的不返回
    n = len(calls)
    assert await src.collect(["SKHY"], date(2026, 10, 1), date(2026, 10, 26)) == []
    assert len(calls) - n == 1


async def test_earnings_6k_same_quarter_merges_to_latest_filing():
    # 7-15 的电话会预告与 7-29 的初步业绩 6-K 指向同一时刻：只出一条，取最新提交的那份
    subs = {"SKHY": submissions([
        ("6-K", "0001193125-26-321989", "2026-07-29", "d115239d6k.htm"),
        ("6-K", "0001193125-26-311226", "2026-07-22", "d139467d6k.htm"),
        ("6-K", "0001193125-26-303983", "2026-07-15", "d19380d6k.htm"),
    ])}
    files = {
        "2120882/000119312526321989/d115239d6k.htm": fixture("skhy_6k_0001193125-26-321989.htm"),
        "2120882/000119312526311226/d139467d6k.htm": fixture("skhy_6k_0001193125-26-311226.htm"),
        "2120882/000119312526303983/d19380d6k.htm": fixture("skhy_6k_0001193125-26-303983.htm"),
    }
    src = Earnings6KEvents(sec_client(subs, files, []), NAMES, today=lambda: date(2026, 7, 29))
    events = await src.collect(["SKHY"], date(2026, 7, 1), date(2026, 8, 31))
    assert [(e.day, e.at, e.key, e.title) for e in events] == [
        (date(2026, 7, 28), utc(2026, 7, 29, 0, 0), "earnings:SKHY:2026Q3", "SK海力士 2026 年二季度业绩发布")]
    assert events[0].details["accession"] == "0001193125-26-321989"
