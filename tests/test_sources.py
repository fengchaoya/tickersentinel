"""数据源解析测试：用 httpx.MockTransport 模拟接口返回，不联网。"""

from datetime import date, timedelta

import httpx
import numpy as np
import pandas as pd
import pytest

from tickersentinel.config import GoogleNewsEdition
from tickersentinel.sources.base import NewsQuery, SourceError
from tickersentinel.sources.filings_sec_edgar import (
    SecEdgarFilings, base_form, guess_extra_shifts, infer_extra_shifts, parse_header_acceptance, parse_json_acceptance,
)
from tickersentinel.sources.news_finnhub import FinnhubSource
from tickersentinel.sources.news_google_rss import GoogleNewsRSS, parse_feed, when_operator
from tickersentinel.sources.prices_alpaca import AlpacaPriceSource
from tickersentinel.sources.prices_yfinance import build_quotes, split_by_symbol

from conftest import utc

NOW = utc(2026, 10, 9, 18, 0)

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Micron</title>
<item><title>Micron rises on HBM demand - Reuters</title><link>https://news.google.com/rss/articles/abc?oc=5</link>
<pubDate>Fri, 09 Oct 2026 16:30:00 GMT</pubDate><source url="https://www.reuters.com">Reuters</source></item>
<item><title>Old Micron story - Bloomberg</title><link>https://news.google.com/rss/articles/old</link>
<pubDate>Mon, 05 Oct 2026 10:00:00 GMT</pubDate><source url="https://www.bloomberg.com">Bloomberg</source></item>
</channel></rss>"""


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def query(**kw) -> NewsQuery:
    base = dict(start=NOW - timedelta(hours=6), end=NOW, symbols=("MU",), queries={"MU": {"en": ["Micron"]}}, langs=("en",))
    base.update(kw)
    return NewsQuery(**base)


def test_parse_google_rss():
    items = parse_feed(RSS, symbol="MU", lang="en")
    assert items[0].title == "Micron rises on HBM demand" and items[0].source == "Reuters"
    assert items[0].published_at == utc(2026, 10, 9, 16, 30)
    assert when_operator(NOW - timedelta(hours=6), NOW) == "when:6h"
    assert when_operator(NOW - timedelta(days=7), NOW) == "when:7d"


async def test_google_rss_fetch_filters_time_and_raises_when_all_fail():
    seen = []

    def ok(request: httpx.Request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, content=RSS)

    src = GoogleNewsRSS({"en": GoogleNewsEdition(hl="en-US", gl="US", ceid="US:en")}, client=mock_client(ok))
    items = await src.fetch(query())
    assert [i.title for i in items] == ["Micron rises on HBM demand"]  # 旧新闻被时间窗口过滤
    assert seen[0]["q"].startswith("Micron when:") and seen[0]["ceid"] == "US:en"

    bad = GoogleNewsRSS({"en": GoogleNewsEdition(hl="en-US", gl="US", ceid="US:en")},
                        client=mock_client(lambda r: httpx.Response(403, text="nope")))
    with pytest.raises(SourceError):
        await bad.fetch(query())


async def test_finnhub_news_and_earnings():
    def handler(request: httpx.Request):
        if request.url.path.endswith("/company-news"):
            return httpx.Response(200, json=[
                {"datetime": int(utc(2026, 10, 9, 15, 0).timestamp()), "headline": "Micron upgraded", "url": "https://x.com/1",
                 "source": "Benzinga", "summary": "Analyst raises PT", "related": "MU"},
                {"datetime": int(utc(2026, 10, 1, 15, 0).timestamp()), "headline": "Old", "url": "https://x.com/0", "source": "Y"},
            ])
        if request.url.path.endswith("/calendar/earnings"):
            return httpx.Response(200, json={"earningsCalendar": [
                {"date": "2026-12-16", "hour": "amc", "symbol": "MU", "epsEstimate": 3.1, "revenueEstimate": 1.5e10}]})
        return httpx.Response(404)

    src = FinnhubSource("k", client=mock_client(handler), names={"MU": "美光"})
    items = await src.fetch(query())
    assert [i.title for i in items] == ["Micron upgraded"] and items[0].summary == "Analyst raises PT"
    ev = await src.collect(["MU", "000660.KS"], date(2026, 10, 9), date(2026, 12, 31))
    assert [(e.kind, e.day, e.time_hint, e.title, e.key, e.confirmed) for e in ev] == [
        ("earnings", date(2026, 12, 16), "盘后", "美光 财报", "earnings:MU:2026Q4", False)]
    assert ev[0].details["epsEstimate"] == 3.1


async def test_alpaca_bars_pagination_and_daily_index():
    calls = []

    def handler(request: httpx.Request):
        params = dict(request.url.params)
        calls.append(params)
        assert request.headers["APCA-API-KEY-ID"] == "id"
        if params["timeframe"] == "1Day":
            return httpx.Response(200, json={"bars": {"MU": [
                {"t": "2026-10-08T04:00:00Z", "o": 1, "h": 1, "l": 1, "c": 100.0, "v": 10},
                {"t": "2026-10-09T04:00:00Z", "o": 1, "h": 1, "l": 1, "c": 101.0, "v": 10}]}, "next_page_token": None})
        if "page_token" not in params:
            return httpx.Response(200, json={"bars": {"MU": [{"t": "2026-10-09T13:30:00Z", "o": 1, "h": 1, "l": 1, "c": 100.5, "v": 300}]},
                                             "next_page_token": "p2"})
        return httpx.Response(200, json={"bars": {"MU": [{"t": "2026-10-09T13:31:00Z", "o": 1, "h": 1, "l": 1, "c": 100.7, "v": 200}],
                                                  "SOXX": [{"t": "2026-10-09T13:31:00Z", "o": 1, "h": 1, "l": 1, "c": 250.0, "v": 50}]},
                                         "next_page_token": None})

    src = AlpacaPriceSource("id", "secret", client=mock_client(handler))
    bars = await src.intraday_bars(["MU", "SOXX"], start=utc(2026, 10, 9, 13, 30))
    assert list(bars["MU"]["close"]) == [100.5, 100.7] and len(bars["SOXX"]) == 1
    assert bars["MU"].index[0] == pd.Timestamp("2026-10-09 13:30", tz="UTC")
    assert calls[0]["feed"] == "iex" and calls[1]["page_token"] == "p2"
    daily = await src.daily_bars(["MU"], lookback_days=5)
    assert list(daily["MU"].index) == [pd.Timestamp("2026-10-08"), pd.Timestamp("2026-10-09")]
    with pytest.raises(SourceError):
        AlpacaPriceSource(None, None)


def _sec_handler(subs: dict, headers: dict[str, str], calls: list[str], headers_status: int = 404):
    """模拟 EDGAR：company_tickers / submissions / 各公告的 index-headers（headers: accession → 美东受理时间）。
    headers 里没有的 index-headers 请求返回 headers_status。"""

    def handler(request: httpx.Request):
        path = request.url.path
        calls.append(path)
        if path.endswith("company_tickers.json"):
            return httpx.Response(200, json={"0": {"cik_str": 723125, "ticker": "MU", "title": "MICRON TECHNOLOGY INC"}})
        if "CIK0000723125" in path:
            return httpx.Response(200, json=subs)
        for accession, stamp in headers.items():
            if path.endswith(f"/{accession.replace('-', '')}/{accession}-index-headers.html"):
                return httpx.Response(200, text=f"<SEC-HEADER>{accession}.hdr.sgml : 20261009\n<ACCEPTANCE-DATETIME>{stamp}\n<TYPE>8-K")
        return httpx.Response(headers_status if path.endswith("-index-headers.html") else 404)

    return handler


async def test_sec_edgar_filings():
    # 时间关系照 2026-10-11 的真实返回：MU 的 JSON 时间 = 美东时间 + 2×4h（多转换了一次 UTC）；13G/A 与 S-8 两行是为覆盖分支虚构的
    subs = {"filings": {"recent": {
        "form": ["10-K", "4", "8-K", "SCHEDULE 13G/A", "S-8", "8-K"],
        "accessionNumber": ["0000723125-26-000023", "0002058769-26-000005", "0000723125-26-000018",
                            "0001422849-26-000099", "0001104659-26-044896", "0000723125-26-000013"],
        "filingDate": ["2026-10-09", "2026-10-02", "2026-09-30", "2026-09-30", "2026-09-30", "2026-06-24"],
        "acceptanceDateTime": ["2026-10-09T16:21:00.000Z", "2026-10-03T02:49:06.000Z", "2026-10-01T00:02:22.000Z",
                               "2026-09-30T21:55:35.000Z", "2026-10-01T00:05:38.000Z", "2026-06-25T00:02:01.000Z"],
        "primaryDocument": ["mu-20260903.htm", "xslF345X06/primarydocument.xml", "mu-20260930.htm",
                            "xslSCHEDULE_13G_X02/primary_doc.xml", "s8.htm", "mu-20260624.htm"],
        "primaryDocDescription": ["10-K", "PRIMARY DOCUMENT", "8-K", "", "S-8", "8-K"],
        "items": ["", "", "2.02,9.01", "", "", "2.02,9.01"],
    }}}
    headers = {"0000723125-26-000023": "20261009082100", "0000723125-26-000018": "20260930160222",
               "0001422849-26-000099": "20260930135535"}  # Form 4 的 index-headers 返回 404 → 走退路
    calls: list[str] = []
    src = SecEdgarFilings("test me@example.org", ["8-K", "4", "10-K", "SC 13G"], min_interval=0,
                          client=mock_client(_sec_handler(subs, headers, calls)))
    q = query(start=utc(2026, 9, 30, 0, 0), end=utc(2026, 10, 10, 0, 0), symbols=("MU", "SKHY"))
    items = await src.fetch(q)

    assert [i.title for i in items] == [
        "MU 提交 10-K（年报）",
        "MU 提交 4（内部人交易）",
        "MU 提交 8-K（重大事项公告）：业绩发布",
        "MU 提交 SCHEDULE 13G/A（大股东持股(13G)·修订）",
    ]
    assert all(i.kind == "filing" for i in items)
    assert items[0].url == "https://www.sec.gov/Archives/edgar/data/723125/000072312526000023/mu-20260903.htm"
    # 受理时间取自 <ACCEPTANCE-DATETIME>（美东）：08:21 EDT = 12:21 UTC，而不是 JSON 里的 16:21Z
    assert items[0].published_at == utc(2026, 10, 9, 12, 21, 0)
    assert items[2].published_at == utc(2026, 9, 30, 20, 2, 22)  # 盘后 16:02 EDT 的业绩 8-K
    # 读不到 index-headers 的 Form 4：按同一份文件对照出的偏移（多转了一次）修正 JSON → 18:49 EDT
    assert items[1].published_at == utc(2026, 10, 2, 22, 49, 6)
    # S-8 不在关心的表格里；6 月的 8-K 被粗筛掉，不会去请求它的 index-headers
    assert not any("000072312526000013" in c or "000110465926044896" in c for c in calls)

    n = len(calls)
    await src.fetch(q)  # 受理时间按 accession 缓存：第二次只多请求 submissions 和那份 404
    assert len(calls) - n == 2


async def test_sec_edgar_acceptance_parsing():
    assert parse_header_acceptance("<ACCEPTANCE-DATETIME>20261009082100\n") == utc(2026, 10, 9, 12, 21, 0)  # 夏令时 EDT
    assert parse_header_acceptance("<ACCEPTANCE-DATETIME>20261201160512") == utc(2026, 12, 1, 21, 5, 12)  # 冬令时 EST
    assert parse_header_acceptance("<html>no header</html>") is None
    # JSON：0 = 真 UTC（SKHY）；1 = 多转换一次（MU，夏令时多 4h、冬令时多 5h）；-1 = 美东时间直接贴 Z
    assert parse_json_acceptance("2026-09-18T10:02:35.000Z") == utc(2026, 9, 18, 10, 2, 35)
    assert parse_json_acceptance("2026-10-09T16:21:00.000Z", 1) == utc(2026, 10, 9, 12, 21, 0)
    assert parse_json_acceptance("2026-01-30T05:12:44.000Z", 1) == utc(2026, 1, 30, 0, 12, 44)
    assert parse_json_acceptance("2026-10-09T08:21:00.000Z", -1) == utc(2026, 10, 9, 12, 21, 0)
    assert infer_extra_shifts("2026-10-09T16:21:00.000Z", utc(2026, 10, 9, 12, 21, 0)) == 1
    assert infer_extra_shifts("2026-01-30T05:12:44.000Z", utc(2026, 1, 30, 0, 12, 44)) == 1
    assert infer_extra_shifts("2026-09-18T10:02:35.000Z", utc(2026, 9, 18, 10, 2, 35)) == 0
    assert base_form("SCHEDULE 13G/A") == "SC 13G" and base_form("8-K/A") == "8-K" and base_form("4") == "4"


async def test_sec_edgar_unverified_fallback_is_utc():
    # 一份 index-headers 都读不到时：JSON 按真 UTC 处理（并打日志），不至于整条丢掉
    subs = {"filings": {"recent": {
        "form": ["6-K"], "accessionNumber": ["0001193125-26-394998"], "filingDate": ["2026-09-18"],
        "acceptanceDateTime": ["2026-09-18T10:02:35.000Z"], "primaryDocument": ["d140793d6k.htm"],
        "primaryDocDescription": ["FORM 6-K"], "items": [""],
    }}}
    src = SecEdgarFilings("test me@example.org", ["6-K"], min_interval=0, client=mock_client(_sec_handler(subs, {}, [])))
    items = await src.fetch(query(start=utc(2026, 9, 18, 0, 0), end=utc(2026, 9, 19, 0, 0)))
    assert [(i.title, i.published_at) for i in items] == [("MU 提交 6-K（外国发行人临时报告）", utc(2026, 9, 18, 10, 2, 35))]


async def test_sec_edgar_filing_date_rolls_over_holidays():
    # 真实案例的时间关系（IBRX 8-K）：2025-12-23 19:25 ET 受理，之后 EDGAR 连休，filingDate 记到 12-29（晚 6 天）。
    # 粗筛按 JSON 时间而不是 filingDate，所以当晚的检索就能看到；JSON 是“多转一次”的冬令时版本（+10h）
    subs = {"filings": {"recent": {
        "form": ["8-K"], "accessionNumber": ["0001193125-25-331244"], "filingDate": ["2025-12-29"],
        "acceptanceDateTime": ["2025-12-24T05:25:40.000Z"], "primaryDocument": ["d8k.htm"],
        "primaryDocDescription": ["8-K"], "items": ["8.01"],
    }}}
    calls: list[str] = []
    src = SecEdgarFilings("test me@example.org", ["8-K"], min_interval=0,
                          client=mock_client(_sec_handler(subs, {"0001193125-25-331244": "20251223192540"}, calls)))
    items = await src.fetch(query(start=utc(2025, 12, 23, 22, 0), end=utc(2025, 12, 24, 4, 30)))
    assert [(i.title, i.published_at) for i in items] == [("MU 提交 8-K（重大事项公告）：其他事项", utc(2025, 12, 24, 0, 25, 40))]


async def test_sec_edgar_headers_down_uses_offline_shift_guess():
    # index-headers 整体不可用（503）：只试一次就停，其余公告用 JSON 时间；偏移由“EDGAR 只在工作日 06-22 点受理”判出
    days = [d for d in range(1, 31) if date(2026, 9, d).weekday() < 5][:12]
    stamps = [(d, hh, mm) for d in reversed(days) for hh, mm in ((19, 0), (16, 5))]  # 美东 16:05 / 19:00，新的在前
    doubled = [(utc(2026, 9, d, hh, mm) + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%S.000Z") for d, hh, mm in stamps]
    n = len(stamps)
    subs = {"filings": {"recent": {
        "form": ["4"] * n, "accessionNumber": [f"0000000001-26-{i:06d}" for i in range(n)],
        "filingDate": [f"2026-09-{d:02d}" for d, _, _ in stamps], "acceptanceDateTime": doubled,
        "primaryDocument": ["doc.xml"] * n, "primaryDocDescription": ["PRIMARY DOCUMENT"] * n, "items": [""] * n,
    }}}
    assert guess_extra_shifts(doubled) == 1
    # 真 UTC 的文件：06:30 / 16:05 EDT = 10:30Z / 20:05Z（若再减一次偏移，清晨那些会落到 02:30，EDGAR 还没开门）
    true_utc = [utc(2026, 9, d, hh, mm).strftime("%Y-%m-%dT%H:%M:%S.000Z") for d in days for hh, mm in ((10, 30), (20, 5))]
    assert guess_extra_shifts(true_utc) == 0
    assert guess_extra_shifts(doubled[:5]) is None  # 行数太少不下结论

    calls: list[str] = []
    src = SecEdgarFilings("test me@example.org", ["4"], min_interval=0,
                          client=mock_client(_sec_handler(subs, {}, calls, headers_status=503)))
    last = days[-1]
    items = await src.fetch(query(start=utc(2026, 9, last, 0, 0), end=utc(2026, 9, last, 23, 59)))
    # 当天两份：16:05 EDT = 20:05Z、19:00 EDT = 23:00Z（若把 JSON 当真 UTC，会晚 4 小时并落到窗口外）
    assert [i.published_at for i in items] == [utc(2026, 9, last, 23, 0), utc(2026, 9, last, 20, 5)]
    assert sum(c.endswith("-index-headers.html") for c in calls) == 1


def test_yfinance_split_by_symbol_both_layouts():
    idx = pd.date_range("2026-10-08", periods=2, freq="D")
    fields = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
    data = np.arange(24, dtype=float).reshape(2, 12)
    by_ticker = pd.DataFrame(data, index=idx, columns=pd.MultiIndex.from_product([["MU", "SOXX"], fields]))
    by_price = by_ticker.swaplevel(axis=1).sort_index(axis=1)
    for df in (by_ticker, by_price):
        out = split_by_symbol(df, ["MU", "SOXX", "XXX"])
        assert set(out) == {"MU", "SOXX"}
        assert list(out["MU"].columns) == ["open", "high", "low", "close", "adj_close", "volume"]
        assert out["MU"]["close"].iloc[0] == by_ticker[("MU", "Close")].iloc[0]


def test_build_quotes_premarket():
    daily = {
        "MU": pd.DataFrame({"close": [100.0, 102.0]}, index=pd.DatetimeIndex(["2026-10-08", "2026-10-09"])),
        "000660.KS": pd.DataFrame({"close": [2_200_000.0, 2_250_000.0]}, index=pd.DatetimeIndex(["2026-10-08", "2026-10-12"])),
    }
    pre = pd.DataFrame({"close": [103.0]}, index=pd.DatetimeIndex([pd.Timestamp("2026-10-12 12:25", tz="UTC")]))
    q = build_quotes(daily, {"MU": pre}, now=utc(2026, 10, 12, 12, 30))
    assert q["MU"].session == "pre" and q["MU"].prev_close == 102.0 and q["MU"].change_pct == pytest.approx(0.98, abs=0.01)
    assert q["000660.KS"].day == date(2026, 10, 12) and q["000660.KS"].change_pct == pytest.approx(2.27, abs=0.01)


def test_build_quotes_after_midnight_keeps_same_reference():
    """盘后价在美东午夜之后仍和“它所在交易日的前一收盘”比，不因日期翻篇而换口径。"""
    daily = {"MU": pd.DataFrame({"close": [904.90, 896.07]}, index=pd.DatetimeIndex(["2026-10-19", "2026-10-20"]))}
    post = pd.DataFrame({"close": [915.08]}, index=pd.DatetimeIndex([pd.Timestamp("2026-10-20 23:59", tz="UTC")]))
    before = build_quotes(daily, {"MU": post}, now=utc(2026, 10, 21, 3, 50))   # 10-20 22:50 CT
    after = build_quotes(daily, {"MU": post}, now=utc(2026, 10, 21, 4, 10))    # 10-20 23:10 CT = 美东已过午夜
    assert before["MU"].prev_close == after["MU"].prev_close == 904.90
    assert before["MU"].session == after["MU"].session == "post"
