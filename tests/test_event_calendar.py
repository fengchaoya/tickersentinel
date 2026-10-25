"""事件日历：入库与变化识别、多来源合并、每晚汇总、写进简报、定时任务。全部离线。"""

from datetime import date, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from tickersentinel.agents.brief import BriefBuilder
from tickersentinel.config import LLMTasks, ManualEvent
from tickersentinel.events.digest import EventDigest, event_line, local_day
from tickersentinel.events.hub import EventHub, merge
from tickersentinel.jobs.scheduler import Planner
from tickersentinel.llm.base import ChatProvider, LLMResult
from tickersentinel.llm.client import LLMClient
from tickersentinel.models import Event, earnings_key
from tickersentinel.news.hub import NewsHub
from tickersentinel.sources.base import EventSource, SourceError
from tickersentinel.storage import Storage

from conftest import FakePriceSource, build_market, utc

NOW = utc(2026, 10, 26, 23, 0)  # 10-26 周一 18:00 CDT


def ev(symbol, day, *, source="yfinance", confirmed=False, at=None, kind="earnings", title=None, time_hint=""):
    key = earnings_key(symbol, day) if kind == "earnings" else f"{kind}:{symbol}:{day}"
    return Event(kind=kind, symbol=symbol, day=day, title=title or f"{symbol} 财报", key=key, at=at,
                 confirmed=confirmed, source=source, time_hint=time_hint)


class FakeEvents(EventSource):
    def __init__(self, name, events=None, error=None):
        self.name = name
        self.events = events or []
        self.error = error
        self.calls = []

    async def collect(self, symbols, start, end):
        self.calls.append((tuple(symbols), start, end))
        if self.error:
            raise self.error
        return [e for e in self.events if start <= e.day <= end]


class FakeProvider(ChatProvider):
    def __init__(self, text):
        super().__init__("fake", "fake-model")
        self.text = text
        self.calls = []

    async def complete(self, **kw):
        self.calls.append(kw)
        return LLMResult(text=self.text, model=self.model)


def test_storage_tracks_new_rescheduled_confirmed():
    st = Storage(":memory:")
    t0 = utc(2026, 10, 20, 12, 0)
    assert st.upsert_event(ev("WDC", date(2026, 10, 23)), t0) == "new"
    assert st.upsert_event(ev("WDC", date(2026, 10, 23)), t0 + timedelta(hours=1)) is None
    assert st.upsert_event(ev("WDC", date(2026, 10, 22)), t0 + timedelta(hours=2)) == "rescheduled"
    assert st.upsert_event(ev("WDC", date(2026, 10, 22), source="config", confirmed=True), t0 + timedelta(hours=3)) == "confirmed"
    # 已确认的事件不会被之后的估计日期覆盖
    assert st.upsert_event(ev("WDC", date(2026, 10, 29)), t0 + timedelta(hours=4)) is None
    [stored] = st.events_between(date(2026, 10, 1), date(2026, 10, 31))
    assert stored.day == date(2026, 10, 22) and stored.confirmed and stored.source == "config"
    changes = st.events_changed_since(t0 + timedelta(minutes=90))
    assert [(c, prev) for _, c, prev in changes] == [("confirmed", date(2026, 10, 23))]


async def test_hub_merges_sources_and_maps_underlying(settings):
    at = utc(2026, 10, 28, 0, 0)  # 10-28 09:00 KST
    yf = FakeEvents("yfinance", [ev("000660.KS", date(2026, 10, 27)), ev("SKHY", date(2026, 10, 27)), ev("WDC", date(2026, 10, 22))])
    sec = FakeEvents("sec_6k", [ev("SKHY", date(2026, 10, 27), source="sec_6k", confirmed=True, at=at, title="SK海力士 三季度业绩")])
    broken = FakeEvents("jpx", error=SourceError("下载失败"))
    settings.events.manual = [ManualEvent(symbol="SKHY", day=date(2027, 1, 5), kind="lockup_expiry", title="锁定期到期")]
    st = Storage(":memory:")
    hub = EventHub(settings, [yf, sec, broken], st)
    res = await hub.refresh(NOW)
    assert res.per_source == {"yfinance": 3, "sec_6k": 1} and "jpx" in res.errors
    skhy = [e for e in res.events if e.symbol == "SKHY" and e.kind == "earnings"]
    assert len(skhy) == 1 and skhy[0].confirmed and skhy[0].at == at and skhy[0].title == "SK海力士 三季度业绩"
    assert not any(e.symbol == "000660.KS" for e in res.events)  # 海外主上市代码并入 SKHY
    assert {e.kind for e in res.events} == {"earnings", "lockup_expiry"}
    assert "000660.KS" in yf.calls[0][0] and "NVDA" in yf.calls[0][0]  # 收集范围含主上市代码与相关公司
    assert hub.last_refresh() == NOW
    assert {c for _, c in res.changes} == {"new"}
    again = await hub.refresh(NOW + timedelta(hours=1))
    assert again.changes == []


def test_merge_prefers_confirmed_then_exact_time():
    d = date(2026, 10, 29)
    a, b = ev("SNDK", d), ev("SNDK", d, source="finnhub", time_hint="盘后")
    c = ev("SNDK", d, source="sec_6k", confirmed=True)
    assert merge([a, b]) == [b] and merge([a, b, c]) == [c]


async def test_digest_first_run_then_only_when_something_new(settings):
    st = Storage(":memory:")
    at = utc(2026, 10, 28, 0, 0)  # 本地 10-27 周二 19:00 CDT：从 10-26 看就是“明天”
    src = FakeEvents("x", [ev("SKHY", date(2026, 10, 27), source="sec_6k", confirmed=True, at=at, title="SK海力士 三季度业绩"),
                           ev("SNDK", date(2026, 10, 29), time_hint="盘后", title="闪迪 财报"),
                           ev("MU", date(2026, 10, 23), kind="insider_sale", source="sec_144", confirmed=True,
                              title="美光 内部人拟减持：张三（高管）约 3 万股")])
    hub = EventHub(settings, [src], st)
    digest = EventDigest(settings, hub, st, LLMClient(settings.llm, providers={}))
    await hub.refresh(NOW)
    msg = await digest.build(NOW)
    assert msg.kind == "events" and msg.title == "📅 事件日历 · 10-26 周一"
    assert "**明天**\n- 10-27 周二 19:00 CDT｜SK海力士 三季度业绩" in msg.body
    assert "- 10-29 周四 盘后｜闪迪 财报（预计）" in msg.body and "首次汇总" in msg.body
    assert "- 新增：10-23 周五｜美光 内部人拟减持：张三（高管）约 3 万股" in msg.body  # 首次汇总也列出近一周的拟减持
    digest.mark_sent(NOW)

    later = NOW + timedelta(days=1)  # 10-27：明天（10-28）没有事件，也没有变化 → 不推送
    await hub.refresh(later)
    assert await digest.build(later) is None

    src.events[1] = ev("SNDK", date(2026, 10, 30), time_hint="盘后", title="闪迪 财报")  # 改期
    await hub.refresh(later + timedelta(hours=1))
    msg = await digest.build(later + timedelta(hours=2))
    assert "- 改期（原 10-29）：10-30 周五 盘后｜闪迪 财报（预计）" in msg.body and "**明天**" not in msg.body
    digest.mark_sent(later + timedelta(hours=2))

    # 远期（45 天以外）的估计日期：入库，但新增 / 改期都不推送；已确认的照常推送
    src.events.append(ev("MU", date(2026, 12, 23), title="美光 财报"))
    await hub.refresh(later + timedelta(hours=3))
    assert st.events_between(date(2026, 12, 22), date(2026, 12, 24)) and await digest.build(later + timedelta(hours=4)) is None
    src.events[-1] = ev("MU", date(2026, 12, 23), source="manual", confirmed=True, title="美光 财报")
    await hub.refresh(later + timedelta(hours=5))
    msg = await digest.build(later + timedelta(hours=6))
    assert msg is not None and "美光 财报" in msg.body


async def test_digest_adds_model_overview_when_available(settings):
    st = Storage(":memory:")
    hub = EventHub(settings, [FakeEvents("x", [ev("WDC", date(2026, 10, 29), title="西部数据 财报")])], st)
    await hub.refresh(NOW)
    cfg = settings.llm.model_copy(update={"tasks": LLMTasks(brief=[], attribution=[], events=["fake"])})
    provider = FakeProvider("西部数据财报是 NAND 需求的风向标。")
    msg = await EventDigest(settings, hub, st, LLMClient(cfg, providers={"fake": provider})).build(NOW)
    assert msg.body.startswith("西部数据财报是 NAND 需求的风向标。")
    assert "西部数据 财报" in provider.calls[0]["prompt"]


async def test_brief_lists_upcoming_events(calendar, settings):
    m = build_market(calendar)
    st = Storage(":memory:")
    hub = EventHub(settings, [FakeEvents("x", [ev("WDC", m.target.day + timedelta(days=2), time_hint="盘后", title="西部数据 财报")])], st)
    await hub.refresh(m.target.close)
    builder = BriefBuilder(settings, calendar, FakePriceSource(m), NewsHub([], settings.universe), hub, st,
                           LLMClient(settings.llm, providers={}))
    res = await builder.build("close", now=m.target.close + timedelta(minutes=15))
    assert "**近期日程**\n- 10-11 周日 盘后｜西部数据 财报（预计）" in res.message.body


class FakeApp:
    def __init__(self, settings, calendar, last):
        self.settings, self.calendar = settings, calendar
        self.events = type("H", (), {"last_refresh": lambda _self: last})()

    async def run_brief(self, kind): ...
    async def prepare_monitor(self, day): ...
    async def monitor_tick(self): ...
    async def refresh_events(self): ...
    async def send_event_digest(self): ...
    async def catch_up(self): ...


def test_planner_installs_event_jobs(calendar, settings):
    sched = AsyncIOScheduler(timezone=settings.tz)
    planned = Planner(FakeApp(settings, calendar, None), sched).install()
    ids = {j.id for j in sched.get_jobs()}
    assert {"events:refresh:06:40", "events:refresh:18:30", "events:digest", "catch_up"} <= ids
    assert any("事件汇总" in p for p in planned) and any("补刷新" in p for p in planned)
    assert {j.id: j for j in sched.get_jobs()}["planner"].misfire_grace_time is None  # 休眠跨过 00:05 多久都会补做规划
    sched2 = AsyncIOScheduler(timezone=settings.tz)
    planned2 = Planner(FakeApp(settings, calendar, datetime.now(settings.tz)), sched2).install()
    assert not any("补刷新" in p for p in planned2)


def test_event_line_local_time_and_links(settings):
    e = ev("SKHY", date(2026, 10, 27), confirmed=True, at=utc(2026, 10, 28, 0, 0), title="SK海力士 三季度业绩")
    assert local_day(e, settings.tz) == date(2026, 10, 27)
    e2 = Event(kind="insider_sale", symbol="SNDK", day=date(2026, 11, 2), title="闪迪 内部人拟减持", key="insider_sale:SNDK:1",
               confirmed=True, url="https://www.sec.gov/x")
    assert event_line(e2, settings.tz) == "- 11-02 周一｜[闪迪 内部人拟减持](https://www.sec.gov/x)"


async def test_stale_estimates_pruned_only_after_grace(settings):
    settings.events.lookahead_days = 90
    st = Storage(":memory:")
    yf = FakeEvents("yfinance", [ev("MU", date(2026, 12, 30)), ev("WDC", date(2026, 11, 5))])
    other = FakeEvents("finnhub", [ev("STX", date(2026, 11, 3), source="finnhub")])
    hub = EventHub(settings, [yf, other], st)
    await hub.refresh(NOW)
    # 12 小时后：MU 临时取不到（单个代码限流），finnhub 整体失败 —— 都不能删，恢复后也不能当成“新增”
    yf.events = [ev("WDC", date(2026, 11, 5))]
    other.error = SourceError("限流")
    await hub.refresh(NOW + timedelta(hours=12))
    assert {e.symbol for e in st.events_between(date(2026, 10, 1), date(2027, 3, 1))} == {"MU", "WDC", "STX"}
    yf.events = [ev("MU", date(2026, 12, 30)), ev("WDC", date(2026, 11, 5))]
    assert (await hub.refresh(NOW + timedelta(hours=24))).changes == []
    # 估计日期跨季度换了 key：旧的那条超过宽限期没再出现才删
    yf.events = [ev("MU", date(2027, 1, 2)), ev("WDC", date(2026, 11, 5))]
    other.error = None
    await hub.refresh(NOW + timedelta(days=5))
    days = {(e.symbol, e.day) for e in st.events_between(date(2026, 10, 1), date(2027, 3, 1))}
    assert days == {("MU", date(2027, 1, 2)), ("WDC", date(2026, 11, 5)), ("STX", date(2026, 11, 3))}


def test_upsert_is_quiet_for_past_events_and_less_reliable_sources():
    st = Storage(":memory:")
    t0 = utc(2026, 10, 20, 12, 0)
    # 已经过去的财报：入库但不提醒；Form 144 即使日期已过也提醒
    assert st.upsert_event(ev("005930.KS", date(2026, 10, 8)), t0) is None
    assert st.upsert_event(ev("MU", date(2026, 10, 14), kind="insider_sale", source="sec_144", confirmed=True), t0) == "new"
    assert [e.symbol for e, _, _ in st.events_changed_since(t0 - timedelta(days=1))] == ["MU"]
    # finnhub 的估计不会被 yfinance 的另一个日期临时顶掉；finnhub 三天没再报告后才让位
    assert st.upsert_event(ev("STX", date(2026, 11, 3), source="finnhub"), t0) == "new"
    assert st.upsert_event(ev("STX", date(2026, 11, 4)), t0 + timedelta(hours=12)) is None
    assert st.events_between(date(2026, 11, 1), date(2026, 11, 30))[0].day == date(2026, 11, 3)
    assert st.upsert_event(ev("STX", date(2026, 11, 4)), t0 + timedelta(days=4)) == "rescheduled"
    # 同一天内盘前 / 盘后互换，或精确时间变动超过 1 小时：记为时间变更
    assert st.upsert_event(ev("WDC", date(2026, 10, 29), time_hint="盘后"), t0) == "new"
    assert st.upsert_event(ev("WDC", date(2026, 10, 29), time_hint="盘前"), t0 + timedelta(hours=1)) == "retimed"


async def test_manual_events_override_and_follow_config(settings):
    st = Storage(":memory:")
    at = utc(2026, 10, 28, 0, 0)
    sec = FakeEvents("sec_6k", [ev("SKHY", date(2026, 10, 27), source="sec_6k", confirmed=True, at=at, title="解析出的业绩")])
    settings.events.manual = [ManualEvent(symbol="SKHY", day=date(2026, 10, 28), kind="earnings", title="手工更正的业绩"),
                              ManualEvent(symbol="SKHY", day=date(2026, 11, 5), kind="lockup_expiry", title="锁定期到期", id="ipo")]
    hub = EventHub(settings, [sec], st)
    await hub.refresh(NOW)
    stored = {e.key: e for e in st.events_between(date(2026, 10, 1), date(2026, 12, 31))}
    assert stored["earnings:SKHY:2026Q4"].title == "手工更正的业绩"  # 手工录入优先于 6-K
    settings.events.manual = [ManualEvent(symbol="SKHY", day=date(2026, 11, 9), kind="lockup_expiry", title="锁定期到期", id="ipo")]
    res = await hub.refresh(NOW + timedelta(hours=1))
    # 手工条目带 id：改日期按“改期”提醒；手工更正的财报日撤掉后回到 6-K 的日期，也会提醒
    assert sorted((e.key, c) for e, c in res.changes) == [("earnings:SKHY:2026Q4", "rescheduled"),
                                                          ("lockup_expiry:SKHY:ipo", "rescheduled")]
    stored = {e.key: e for e in st.events_between(date(2026, 10, 1), date(2026, 12, 31))}
    assert stored["earnings:SKHY:2026Q4"].source == "sec_6k"  # 手工条目删掉后回到 6-K
    settings.events.manual = []
    await hub.refresh(NOW + timedelta(hours=2))
    assert "lockup_expiry:SKHY:ipo" not in {e.key for e in st.events_between(date(2026, 10, 1), date(2026, 12, 31))}


def test_brief_news_window_reaches_back_to_last_brief(calendar, settings):
    from tickersentinel.agents.brief import MAX_GAP_HOURS

    st = Storage(":memory:")
    builder = BriefBuilder(settings, calendar, None, NewsHub([], settings.universe), None, st, LLMClient(settings.llm, providers={}))
    monday_pre = utc(2026, 10, 26, 12, 30)  # 10-26 周一 07:30 CDT
    assert builder.window_start("premarket", monday_pre) == monday_pre - timedelta(hours=18)  # 没有记录：按固定回看
    st.record_brief("close", "收盘", "…", "m1", at=utc(2026, 10, 23, 20, 15))  # 上周五 15:15 的收盘简报
    st.record_brief("weekly", "周报", "…", "m2", at=utc(2026, 10, 24, 14, 0))  # 周报不算（它有自己的 168 小时窗口）
    assert builder.window_start("premarket", monday_pre) == utc(2026, 10, 23, 20, 15)  # 周末不留空档
    assert builder.window_start("weekly", monday_pre) == monday_pre - timedelta(hours=168)
    st2 = Storage(":memory:")
    st2.record_brief("close", "收盘", "…", "m1", at=utc(2026, 10, 1, 20, 15))  # 很久没运行：最多回看 96 小时
    builder2 = BriefBuilder(settings, calendar, None, NewsHub([], settings.universe), None, st2, LLMClient(settings.llm, providers={}))
    assert builder2.window_start("close", monday_pre) == monday_pre - timedelta(hours=MAX_GAP_HOURS)
