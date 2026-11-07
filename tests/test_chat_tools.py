"""对话工具：行情、盘中拆分、新闻检索、异动记录、事件日历。全部离线（合成行情、假数据源、内存数据库）。"""

import asyncio
import json
from datetime import date, datetime, timedelta

import pytest

from tickersentinel.agents import chat_tools
from tickersentinel.agents.chat_tools import ChatTools, detect_lang, market_status
from tickersentinel.events.hub import EventHub
from tickersentinel.models import SECTOR, Alert, Event, NewsItem, Quote, Trigger, earnings_key
from tickersentinel.news.hub import NewsHub
from tickersentinel.sources.base import NewsSource, PriceSource, SourceError
from tickersentinel.storage import Storage

from conftest import TARGET_DAY, FakeNewsSource, FakePriceSource, at, build_market, utc


class QuoteSource(PriceSource):
    name = "quotes"

    def __init__(self, quotes=None, error=None, delay=0.0):
        self.quotes = quotes or {}
        self.error = error
        self.delay = delay
        self.asked = []

    async def intraday_bars(self, symbols, start, end=None):  # pragma: no cover - 本测试不用
        return {}

    async def daily_bars(self, symbols, lookback_days):  # pragma: no cover
        return {}

    async def latest_quotes(self, symbols):
        self.asked.append(list(symbols))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return {s: q for s, q in self.quotes.items() if s in symbols}


def tools_for(settings, *, clock, storage=None, **deps):
    return ChatTools(settings, storage or Storage(":memory:"), clock=lambda: clock, **deps)


# ───────── 代码解析与小工具 ─────────


def test_resolve_names_terms_and_tickers(settings):
    t = tools_for(settings, clock=utc(2026, 10, 9, 15))
    assert t.resolve("mu") == "MU"
    assert t.resolve("美光") == "MU" and t.resolve("海力士") == "SKHY" and t.resolve("SK hynix") == "SKHY"
    assert t.resolve("三星电子") == "005930.KS" and t.resolve("soxx") == "SOXX" and t.resolve("英伟达") == "NVDA"
    # 检索词只做完全匹配：行业词不会被当成某只股票
    assert t.resolve("NAND", allow_unknown=False) is None and t.resolve("HBM 价格", allow_unknown=False) is None
    assert t.resolve("intc") == "INTC" and t.resolve("intc", allow_unknown=False) is None
    assert t.resolve("  ") is None
    assert detect_lang("HBM 价格") == "zh" and detect_lang("SK하이닉스") == "ko" and detect_lang("DRAM prices") == "en"


def test_market_status(calendar):
    s = calendar.session(TARGET_DAY)
    tz = "America/Chicago"
    assert market_status(calendar, at(s, 60), tz).startswith("盘中（15:00 CDT 收盘")
    assert market_status(calendar, s.open - timedelta(hours=1), tz) == "盘前（今天 08:30 CDT 开盘）"
    assert market_status(calendar, s.close + timedelta(hours=1), tz).startswith("盘后")
    assert market_status(calendar, utc(2026, 10, 10, 15), tz).startswith("休市（下一个交易日 10-12 周一 08:30 CDT")


def test_specs_follow_dependencies(settings, calendar):
    names = lambda t: [s.name for s in t.specs()]  # noqa: E731
    clock = utc(2026, 10, 9, 15)
    assert names(tools_for(settings, clock=clock)) == ["recent_alerts", "upcoming_events"]
    assert names(tools_for(settings, clock=clock, macro=FakeMacro())) == ["get_macro", "recent_alerts", "upcoming_events"]
    full = tools_for(settings, clock=clock, prices=QuoteSource(), calendar=calendar, macro=FakeMacro(),
                     news=NewsHub([FakeNewsSource([])], settings.universe))
    assert names(full) == ["get_macro", "get_intraday", "search_news", "recent_alerts", "upcoming_events"]
    assert "get_quotes" not in names(full)  # 个股最新价不做成工具（用户在券商 App 里看）
    for spec in full.specs():  # 都是 object 类型的 JSON Schema，必填参数在 properties 里
        assert spec.parameters["type"] == "object" and set(spec.parameters["required"]) <= set(spec.parameters["properties"])


# ───────── get_macro ─────────


class FakeMacro:
    def __init__(self, snap=None, error=None, delay=0.0):
        self.snap, self.error, self.delay = snap, error, delay

    async def snapshot(self, now=None, *, use_cache=True):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.snap


def macro_snap(now, off_hours=False):
    from tickersentinel.sources.macro import MacroQuote, MacroSnapshot, TreasuryCurve

    q = [MacroQuote("us10y", "美债10年", "^TNX", "yield", "利率", 5.11, now, -1.2, "较昨收", 0.4),
         MacroQuote("wti", "WTI原油", "CL=F", "price", "大宗商品", 97.05, now, -0.9, "24h", None),
         MacroQuote("spx", "标普期货" if off_hours else "标普500", "ES=F" if off_hours else "^GSPC", "level", "股指",
                    7766.0, now, 0.7, "较美股收盘" if off_hours else "较昨收", 0.1, off_hours=off_hours)]
    return MacroSnapshot(at=now, quotes=q, curve=TreasuryCurve(date(2026, 10, 20), {"2Y": 4.79, "10Y": 5.12, "30Y": 5.42}))


async def test_get_macro_text_and_errors(settings, calendar, monkeypatch):
    now = at(calendar.session(TARGET_DAY), 90)
    out = await tools_for(settings, clock=now, calendar=calendar, macro=FakeMacro(macro_snap(now))).session().execute("get_macro", {})
    assert not out.is_error
    assert "美债10年 5.11%（-1bp，1h ±0bp）" in out.content and "WTI原油 $97.05（24h -0.9%）" in out.content
    assert "2s10s +33bp" in out.content and "美股现在：盘中" in out.content

    later = utc(2026, 10, 10, 15)  # 周六：股指换成期货
    out = await tools_for(settings, clock=later, calendar=calendar,
                          macro=FakeMacro(macro_snap(later, off_hours=True))).session().execute("get_macro", {})
    assert "标普期货 7,766（较美股收盘 +0.7%" in out.content and "股指显示的是期货" in out.content

    out = await tools_for(settings, clock=now, macro=FakeMacro(error=SourceError("宏观数据全部取不到"))).session().execute("get_macro", {})
    assert out.is_error and out.content.startswith("数据源失败")
    monkeypatch.setattr(chat_tools, "TOOL_TIMEOUT", 0.01)
    out = await tools_for(settings, clock=now, macro=FakeMacro(macro_snap(now), delay=1)).session().execute("get_macro", {})
    assert out.is_error and "超时" in out.content
    out = await tools_for(settings, clock=now, macro=FakeMacro(error=KeyError("boom"))).session().execute("get_macro", {})
    assert out.is_error and out.content.startswith("查询出错：KeyError")
    out = await tools_for(settings, clock=now).session().execute("get_macro", {})  # 没有宏观数据源：不提供
    assert out.is_error and "没有可用的工具" in out.content


# ───────── get_intraday ─────────


async def test_get_intraday_split_idiosyncratic(settings, calendar):
    m = build_market(calendar, shocks={("MU", TARGET_DAY, 100): -0.03})
    now = at(m.target, 120)
    t = tools_for(settings, clock=now, prices=FakePriceSource(m), calendar=calendar)
    out = await t.session().execute("get_intraday", {"symbol": "美光", "minutes": "30"})
    assert not out.is_error, out.content
    d = json.loads(out.content)
    assert d["symbol"] == "MU" and d["status"] == "盘中" and d["date"] == "2026-10-09" and d["last_bar"] == "10:29 CDT"
    assert d["benchmark"] == "SOXX" and 0.9 < d["beta"] < 1.7
    w = d["last_30m"]
    assert w["move_pct"] == pytest.approx(w["sector_pct"] + w["idio_pct"], abs=0.02)
    assert w["sector_pct"] == pytest.approx(d["beta"] * w["benchmark_pct"], abs=0.02)
    assert w["idio_pct"] < -2.5 and w["judgment"] == "以个股因素为主" and d["note"].startswith("sector_pct")
    assert set(d["peers_last_30m"]) == {"SNDK", "SKHY", "WDC"} and all(abs(v) < 1 for v in d["peers_last_30m"].values())
    assert d["vs_prev_close"]["move_pct"] < -2.0 and "peers_vs_prev_close" in d


async def test_get_intraday_sector_move_and_after_close(settings, calendar):
    m = build_market(calendar, shocks={("SOXX", TARGET_DAY, 200): -0.05})
    s = m.target
    t = tools_for(settings, clock=s.close + timedelta(hours=1), prices=FakePriceSource(m), calendar=calendar)
    d = json.loads((await t.session().execute("get_intraday", {"symbol": "SNDK", "minutes": 400})).content)
    assert d["status"] == "今天已收盘" and "last_390m" in d  # 窗口截到 390 分钟
    assert d["vs_prev_close"]["judgment"] == "以板块联动为主"
    assert d["vs_prev_close"]["benchmark_pct"] < -3 and d["vs_prev_close"]["sector_pct"] < -4

    weekend = tools_for(settings, clock=utc(2026, 10, 10, 15), prices=FakePriceSource(m), calendar=calendar)
    d = json.loads((await weekend.session().execute("get_intraday", {"symbol": "SNDK"})).content)
    assert d["date"] == "2026-10-09" and d["status"].startswith("美股现在休市") and "最近一个交易日" in d["status"]
    assert d["note"].startswith("last_30m 是 10-09 收盘前最后 30 分钟，不是现在。")


async def test_get_intraday_bad_symbols(settings, calendar):
    m = build_market(calendar)
    t = tools_for(settings, clock=at(m.target, 120), prices=FakePriceSource(m), calendar=calendar)
    out = await t.session().execute("get_intraday", {"symbol": "000660.KS"})
    assert out.is_error and "不是美股代码" in out.content
    out = await t.session().execute("get_intraday", {})
    assert out.is_error and "需要 symbol" in out.content
    out = await t.session().execute("get_intraday", {"symbol": "AAPL"})  # 合成行情里没有
    assert out.is_error and "没有取到 AAPL" in out.content


# ───────── search_news ─────────


def news(title, ts, url, symbols=("MU",), kind="news", source="Reuters"):
    return NewsItem(title=title, url=url, published_at=ts, source=source, provider="fake", symbols=symbols, kind=kind)


async def test_search_news_symbol_freetext_and_links(settings):
    now = utc(2026, 10, 9, 15)
    items = [
        news("Micron raises guidance", now - timedelta(hours=2), "https://reuters.com/mu1"),
        news("MU 提交 8-K", now - timedelta(hours=1), "https://sec.gov/mu-8k", kind="filing", source=""),
        news("Old story", now - timedelta(hours=30), "https://reuters.com/old"),
    ]
    src = FakeNewsSource(items)
    t = tools_for(settings, clock=now, news=NewsHub([src], settings.universe))
    session = t.session()
    out = await session.execute("search_news", {"query": "美光", "hours": 24})
    assert not out.is_error
    q = src.queries[-1]
    assert q.symbols == ("MU",) and SECTOR not in q.queries  # 持仓：按预设检索词 + 公告，不带行业词
    lines = out.content.splitlines()
    assert "MU（美光；已含预设的多语言检索词与公告）" in lines[0] and "共 2 条" in lines[0]
    assert lines[1] == "[n1] 10-09 09:00｜SEC公告｜MU 提交 8-K" and lines[2].startswith("[n2] 10-09 08:00｜Reuters｜Micron")
    assert "Old story" not in out.content and "http" not in out.content  # 不把网址交给模型

    out = await session.execute("search_news", {"query": "HBM 价格", "hours": "48"})
    q = src.queries[-1]
    assert q.symbols == () and q.queries == {SECTOR: {"zh": ["HBM 价格"]}} and q.langs == ("zh",)
    assert "[n1]" in out.content and "[n3] " in out.content  # 同一次提问内：同一条沿用编号，新条目接着编

    answer = session.link_refs("上调指引 [n2]，另见【n1】与 [n2, n3]；[**n3**] 加粗也认；[n9] 不存在 [n8]。")
    assert answer.startswith("上调指引 [Reuters](https://reuters.com/mu1)，另见[fake](https://sec.gov/mu-8k)与 ")
    assert answer.count("(https://reuters.com/old)") == 2
    assert answer.endswith("加粗也认；不存在。")  # 本次提问里没出现过的编号直接去掉（模型会沿用上一轮的编号）
    assert session.link_refs("rose [n9] because") == "rose because"


async def test_search_news_failures(settings):
    now = utc(2026, 10, 9, 15)

    class Down(NewsSource):
        name = "down"

        async def fetch(self, q):
            raise SourceError("HTTP 503")

    t = tools_for(settings, clock=now, news=NewsHub([Down()], settings.universe))
    out = await t.session().execute("search_news", {"query": "MU"})
    assert out.is_error and "新闻源全部失败" in out.content and "503" in out.content

    t = tools_for(settings, clock=now, news=NewsHub([FakeNewsSource([]), Down()], settings.universe))
    out = await t.session().execute("search_news", {"query": "SNDK"})
    assert not out.is_error and "没有找到" in out.content and "部分来源失败：down" in out.content
    out = await t.session().execute("search_news", {})
    assert out.is_error and "需要 query" in out.content


# ───────── recent_alerts / upcoming_events ─────────


async def test_recent_alerts_with_attribution(settings):
    now = utc(2026, 10, 9, 18)
    storage = Storage(":memory:")
    a = Alert(symbol="MU", ts=now - timedelta(days=1), price=97.0, prev_close=100.0,
              triggers=[Trigger("level", -1, -3.0, level_pct=3)], window_minutes=None, move_pct=-3.0,
              benchmark_move_pct=-1.0, beta=1.3, abnormal_move_pct=-1.7, volume_ratio=1.5)
    storage.record_alert(a, "2026-10-08")
    storage.save_attribution(a.id, "略", {"summary": "同业指引下调带动板块走弱"})
    old = Alert(symbol="SNDK", ts=now - timedelta(days=9), price=50.0, prev_close=48.0,
                triggers=[Trigger("rapid", 1, 2.6, window_minutes=15, z=5.0)], window_minutes=15, move_pct=2.6,
                benchmark_move_pct=None, beta=1.5, abnormal_move_pct=None, volume_ratio=None)
    storage.record_alert(old, "2026-09-30")
    t = tools_for(settings, clock=now, storage=storage)
    out = await t.session().execute("recent_alerts", {})
    assert not out.is_error
    lines = out.content.splitlines()
    assert "共 1 条" in lines[0] and len(lines) == 2
    assert lines[1].startswith("- 10-08 13:00 MU 跌破较昨收 -3% 关口")
    assert "较昨收 -3.0%" in lines[1] and "板块 -1.3%（SOXX -1.0% × β1.30）·个股特异 -1.7%" in lines[1]
    assert lines[1].endswith("归因：同业指引下调带动板块走弱")

    out = await t.session().execute("recent_alerts", {"days": 30, "symbol": "闪迪"})
    assert "SNDK 15分钟内急涨 +2.6%" in out.content and "归因：无记录" in out.content and "MU" not in out.content
    out = await t.session().execute("recent_alerts", {"days": 2, "symbol": "SKHY"})
    assert "没有 SKHY 异动记录" in out.content


async def test_upcoming_events_window_symbol_and_hub(settings):
    now = utc(2026, 10, 21, 11)  # 06:00 CDT
    storage = Storage(":memory:")
    today = date(2026, 10, 21)
    sk_at = datetime(2026, 10, 28, 0, 0, tzinfo=now.tzinfo)  # 10-28 09:00 KST = 10-27 19:00 CDT
    events = [
        Event(kind="earnings", symbol="SKHY", day=date(2026, 10, 28), title="SK海力士 2026 年三季度业绩",
              key=earnings_key("SKHY", date(2026, 10, 28)), at=sk_at, confirmed=True, source="sec_6k"),
        Event(kind="earnings", symbol="WDC", day=today + timedelta(days=9), title="西部数据 财报",
              key=earnings_key("WDC", today + timedelta(days=9)), time_hint="盘后", source="yfinance"),
        Event(kind="earnings", symbol="MU", day=today + timedelta(days=40), title="美光 财报",
              key=earnings_key("MU", today + timedelta(days=40)), source="yfinance"),
    ]
    for e in events:
        storage.upsert_event(e, now)
    t = tools_for(settings, clock=now, storage=storage)
    out = await t.session().execute("upcoming_events", {})
    lines = out.content.splitlines()
    assert "未来 14 天（10-21 起）事件，共 2 条" in lines[0]
    assert lines[1] == "- SKHY（持仓） 10-27 周二 19:00 CDT SK海力士 2026 年三季度业绩"  # 带代码与持仓标记
    assert lines[2] == "- WDC 10-30 周五 盘后 西部数据 财报（预计日期）"

    out = await t.session().execute("upcoming_events", {"days": 60, "symbol": "SK海力士(首尔)"})  # 主上市代码并入 ADR
    assert "SKHY 的事件" in out.content and "美光" not in out.content

    hub = EventHub(settings, [], storage)
    t = tools_for(settings, clock=now, storage=storage, events=hub)
    out = await t.session().execute("upcoming_events", {"days": 5})  # 至少看 14 天：“下周”问成 7 天也不漏
    assert out.content.startswith("未来 14 天") and "西部数据 财报" in out.content and "还没有刷新过" in out.content
    empty = tools_for(settings, clock=now, storage=Storage(":memory:"), events=EventHub(settings, [], Storage(":memory:")))
    out = await empty.session().execute("upcoming_events", {"days": 5})
    assert "没有已知的事件" in out.content


def test_resolve_does_not_hijack_real_tickers(settings):
    """英文只认整词部分匹配：ON（安森美）不能变成 Micron，DIS / AD 不能变成 SanDisk / SK hynix ADR。"""
    t = tools_for(settings, clock=utc(2026, 10, 9, 15))
    for q in ["ON", "DIS", "SAN", "AN", "AD", "MI"]:
        assert t.resolve(q) == q and t.resolve(q, allow_unknown=False) is None
    assert t.resolve("Micron") == "MU" and t.resolve("sandisk") == "SNDK" and t.resolve("海力士") == "SKHY"


async def test_search_news_reserves_slots_for_filings(settings):
    now = utc(2026, 10, 9, 15)
    items = [news(f"Micron story {i}", now - timedelta(minutes=10 * i), f"https://reuters.com/{i}") for i in range(1, 30)]
    items.append(news("MU 提交 4（内部人交易）", now - timedelta(hours=20), "https://sec.gov/mu-4", kind="filing", source=""))
    t = tools_for(settings, clock=now, news=NewsHub([FakeNewsSource(items)], settings.universe))
    out = await t.session().execute("search_news", {"query": "MU"})
    lines = out.content.splitlines()
    assert "其中 SEC 公告 1 条" in lines[0] and len(lines) == 1 + chat_tools.MAX_NEWS + 1
    assert any("SEC公告｜MU 提交 4" in line for line in lines)  # 新闻再多，公告也不会被挤掉


def test_link_refs_strips_model_urls_and_separates_links(settings):
    t = tools_for(settings, clock=utc(2026, 10, 9, 15))
    session = t.session()
    session._ref(news("A", utc(2026, 10, 9, 14), "https://reuters.com/a", source="Reuters"))
    session._ref(news("B", utc(2026, 10, 9, 14), "https://bloomberg.com/b", source="Bloomberg"))
    out = session.link_refs("原文见 [SEC 8-K](https://www.sec.gov/fake/0001.htm)，另见 https://made.up/x [n1][n2] 和 [n7](https://fake.example)")
    assert "sec.gov/fake" not in out and "made.up" not in out and "fake.example" not in out
    assert "[SEC 8-K]" not in out and "SEC 8-K" in out  # 模型写的链接只留文字
    assert "[Reuters](https://reuters.com/a)、[Bloomberg](https://bloomberg.com/b)" in out


async def test_upcoming_events_capped_at_collection_horizon(settings):
    now = utc(2026, 10, 21, 11)
    t = tools_for(settings, clock=now, storage=Storage(":memory:"))
    horizon = settings.events.lookahead_days
    out = await t.session().execute("upcoming_events", {"days": 120, "symbol": "MU"})
    assert out.content.startswith(f"未来 {horizon} 天没有已知的MU 的事件")
    assert "日历只收集到" in out.content and "不要推测日期" in out.content


def test_with_official_close_replaces_last_bar():
    import pandas as pd

    view = pd.DataFrame({"close": [10.0, 10.5]}, index=pd.DatetimeIndex(["2026-10-09 19:58", "2026-10-09 19:59"], tz="UTC"))
    daily = pd.DataFrame({"close": [9.0, 10.52]}, index=pd.DatetimeIndex(["2026-10-08", "2026-10-09"]))
    out = chat_tools._with_official_close(view, daily, date(2026, 10, 9))
    assert out["close"].tolist() == [10.0, 10.52] and view["close"].iloc[-1] == 10.5  # 不改原数据
    assert chat_tools._with_official_close(view, daily, date(2026, 10, 12)) is view  # 日线还没有这一天：保持不变


async def test_upcoming_events_symbol_reaches_horizon_and_hints_when_short(settings):
    """只问一家公司时直接查到收集范围（MU 下次财报在 60 天以外也能查到）；不填代码、天数不够时提示可以调大。"""
    now = utc(2026, 10, 21, 11)
    storage = Storage(":memory:")
    far = date(2026, 12, 23)
    storage.upsert_event(Event(kind="earnings", symbol="MU", day=far, title="美光 财报",
                               key=earnings_key("MU", far), source="yfinance"), now)
    t = tools_for(settings, clock=now, storage=storage)
    out = await t.session().execute("upcoming_events", {"symbol": "MU", "days": 60})
    assert "美光 财报" in out.content and "12-23" in out.content
    out = await t.session().execute("upcoming_events", {"days": 20})
    assert out.content.startswith("未来 20 天没有已知的事件") and "可把 days 调到" in out.content
    spec = next(s for s in t.specs() if s.name == "upcoming_events")
    assert f"14–{settings.events.lookahead_days}" in spec.parameters["properties"]["days"]["description"]


async def test_upcoming_events_long_horizon_untracked_symbol_and_truncation(settings):
    now = utc(2026, 10, 21, 11)
    settings.events.lookahead_days = 200
    storage = Storage(":memory:")
    t = tools_for(settings, clock=now, storage=storage)
    out = await t.session().execute("upcoming_events", {"days": 200})
    assert out.content.startswith("未来 200 天没有已知的事件") and "日历只收集到" in out.content  # 提示的天数确实调得到
    out = await t.session().execute("upcoming_events", {"symbol": "INTC"})
    assert out.content.startswith("INTC 不在事件日历的收集范围") and "不能据此判断它没有财报" in out.content
    out = await t.session().execute("upcoming_events", {"symbol": "Micron Tech"})
    assert out.content.startswith("没认出“Micron Tech”对应的代码")
    for i in range(chat_tools.MAX_EVENTS + 5):
        d = date(2026, 10, 22) + timedelta(days=i)
        storage.upsert_event(Event(kind="earnings", symbol="WDC", day=d, title=f"事件 {i}",
                                   key=f"manual:x{i}", source="manual", confirmed=True), now)
    out = await t.session().execute("upcoming_events", {"days": 60})
    assert f"共 {chat_tools.MAX_EVENTS + 5} 条，只列最早的 {chat_tools.MAX_EVENTS} 条" in out.content


async def test_recent_alerts_by_symbol_and_reference(settings):
    """问持仓时把它的夜间信号、夜间篮子一起带上；问首尔 000660.KS 就查它自己；盘后的异动写“较今日收盘”。"""
    now = utc(2026, 10, 9, 18)
    storage = Storage(":memory:")

    def rec(sym, hours, segment="regular"):
        a = Alert(symbol=sym, ts=now - timedelta(hours=hours), price=97.0, prev_close=100.0,
                  triggers=[Trigger("day", -1, -3.0, level_pct=3)], window_minutes=None, move_pct=-3.0,
                  benchmark_move_pct=None, beta=None, abnormal_move_pct=None, volume_ratio=None, segment=segment)
        storage.record_alert(a, "2026-10-09")

    rec("SKHY", 2, "post"), rec("000660.KS", 10), rec("night:memory:XKRX", 11), rec("MU", 3)
    t = tools_for(settings, clock=now, storage=storage)
    out = (await t.session().execute("recent_alerts", {"symbol": "000660.KS"})).content
    assert "000660.KS" in out and "SKHY" not in out.split("\n", 1)[1]
    out = (await t.session().execute("recent_alerts", {"symbol": "SKHY"})).content
    assert "共 3 条" in out and "MU" not in out.split("\n", 1)[1] and "较今日收盘 -3.0%" in out
