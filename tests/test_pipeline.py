"""端到端测试：盘中检测 → 推送 → 归因（降级 / 假模型），简报，回放，日程规划。全部离线。"""

from datetime import date, datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from tickersentinel.agents.attribution import Attributor
from tickersentinel.agents.brief import BriefBuilder
from tickersentinel.agents.render import fmt_price
from tickersentinel.jobs.monitor import IntradayMonitor
from tickersentinel.jobs.replay import run_replay
from tickersentinel.jobs.scheduler import Planner
from tickersentinel.config import LLMTasks
from tickersentinel.llm.base import ChatProvider, LLMResult
from tickersentinel.llm.client import LLMClient
from tickersentinel.models import SECTOR, NewsItem
from tickersentinel.news.hub import NewsHub
from tickersentinel.notify.console import ConsoleNotifier
from tickersentinel.storage import Storage
from tickersentinel.timeutil import UTC

from conftest import TARGET_DAY, FakeNewsSource, FakePriceSource, at, build_market

CRASH = {("MU", TARGET_DAY, 120 + i): -0.006 for i in range(5)}


class FakeProvider(ChatProvider):
    kind = "fake"

    def __init__(self, text):
        super().__init__("fake", "fake-model")
        self.text = text
        self.calls = []

    async def complete(self, **kw):
        self.calls.append(kw)
        return LLMResult(text=self.text, model=self.model)


def fake_llm(settings, text):
    cfg = settings.llm.model_copy(update={"tasks": LLMTasks(brief=["fake"], attribution=["fake"], events=["fake"])})
    return LLMClient(cfg, providers={"fake": FakeProvider(text)})


def no_llm(settings):
    return LLMClient(settings.llm, providers={})


def news_for(market):
    return [
        NewsItem("Micron cut to Sell at Example Securities", "https://news.example/mu-cut", at(market.target, 117),
                 source="Benzinga", provider="fake", symbols=("MU",)),
        NewsItem("Unrelated afternoon story", "https://news.example/late", at(market.target, 300),
                 source="Yahoo", provider="fake", symbols=("MU",)),
    ]


def make_monitor(settings, calendar, market, llm):
    storage, notifier = Storage(":memory:"), ConsoleNotifier(echo=False)
    hub = NewsHub([FakeNewsSource(news_for(market))], settings.universe)
    mon = IntradayMonitor(settings, calendar, FakePriceSource(market), storage, notifier, Attributor(settings, hub, llm))
    return mon, storage, notifier


async def test_tick_alert_then_fallback_attribution(calendar, settings):
    settings.monitor.price.day_pct = [8.0, 12.0]  # 只看急跌规则，避免随机游走碰到全天的档位
    m = build_market(calendar, shocks=CRASH)
    mon, storage, notifier = make_monitor(settings, calendar, m, no_llm(settings))

    alerts = await mon.tick(now=at(m.target, 125))
    await mon.drain()
    assert [a.symbol for a in alerts] == ["MU"]
    assert [msg.kind for _, msg in notifier.sent] == ["alert", "attribution"]
    alert_id, alert_msg = notifier.sent[0]
    attr = notifier.sent[1][1]
    assert attr.reply_to == alert_id  # 归因挂在异动消息下面
    assert "Micron cut to Sell" in attr.body and "异动前" in attr.body and "Unrelated" not in attr.body
    row = storage.alerts_for_day(TARGET_DAY.isoformat())[0]
    assert row.symbol == "MU" and row.headline.rule == "rapid"

    # 冷却：下一分钟不重复推送
    assert await mon.tick(now=at(m.target, 126)) == []
    # 非交易时段什么也不做
    assert await mon.tick(now=m.target.close + timedelta(minutes=5)) == []


async def test_attribution_with_model_output(calendar, settings):
    settings.monitor.price.day_pct = [8.0, 12.0]
    m = build_market(calendar, shocks=CRASH)
    reply = ('{"summary": "券商下调评级引发抛售", "driver": "个股", "causes": ['
             '{"cause": "Example Securities 下调至卖出", "confidence": "高", "evidence": ["#1"], "timing": "异动前7分钟"}], '
             '"watch": "关注其他券商是否跟进"}')
    llm = fake_llm(settings, reply)
    mon, storage, notifier = make_monitor(settings, calendar, m, llm)
    await mon.tick(now=at(m.target, 125))
    await mon.drain()
    body = notifier.sent[1][1].body
    assert "券商下调评级引发抛售" in body and "[Benzinga](https://news.example/mu-cut)" in body and "🟢[高]" in body
    call = llm.providers["fake"].calls[0]
    assert "#1 [" in call["prompt"] and "拆分" in call["prompt"]
    # 时间差按这波涨跌的起点（日内高点）和检测时刻两个都标；提示词里写明起点
    assert "起点前" in call["prompt"] and "检测前" in call["prompt"] and "这波涨跌的起点：" in call["prompt"]
    assert call["web_search"] is True and call["json_schema"]["required"] == ["summary", "driver", "causes", "watch"]
    saved = storage.attribution_summary(storage.alerts_for_day(TARGET_DAY.isoformat())[0].id)
    assert saved == "券商下调评级引发抛售"


def brief_items(m):
    return [
        NewsItem("Micron expands HBM capacity", "https://n.example/1", at(m.target, 200), source="Reuters", symbols=("MU",)),
        NewsItem("DRAM contract prices rise again", "https://n.example/2", at(m.target, 100), source="TrendForce", symbols=(SECTOR,)),
    ]


async def test_close_brief_fallback_and_seen_filter(calendar, settings):
    m = build_market(calendar)
    storage = Storage(":memory:")
    hub = NewsHub([FakeNewsSource(brief_items(m))], settings.universe)
    builder = BriefBuilder(settings, calendar, FakePriceSource(m), hub, None, storage, no_llm(settings))
    now = m.target.close + timedelta(minutes=15)
    res = await builder.build("close", now=now)
    body = res.message.body
    assert res.message.title.startswith("🌙 收盘简报 · 10-09 周五")
    assert "MU：收 $" in body and "板块贡献" in body and "个股特异" in body
    assert "Micron expands HBM capacity" in body and "行业：" in body
    storage.mark_seen(res.items)
    again = await builder.build("close", now=now)
    assert "本时段没有新的新闻" in again.message.body


async def test_premarket_snapshot_without_minute_bars_shows_latest_close(calendar, settings):
    # 取不到盘前分钟数据时 Quote 退化为“最近收盘 vs 前一收盘”（session="closed"）：
    # “昨收”应是最近一个收盘价，而不是再前一天的
    m = build_market(calendar)
    builder = BriefBuilder(settings, calendar, FakePriceSource(m), NewsHub([], settings.universe), None,
                           Storage(":memory:"), no_llm(settings))
    last, before = (float(x) for x in m.daily["MU"]["close"].iloc[[-1, -2]])
    lines = await builder._premarket_snapshot(m.target.open + timedelta(days=3))  # 下一个交易日（周一）盘前
    assert lines[0].startswith(f"- MU：昨收 {fmt_price('MU', last)}") and fmt_price("MU", before) not in lines[0]
    assert f"板块 SOXX：昨收 {fmt_price('SOXX', float(m.daily['SOXX']['close'].iloc[-1]))}" in "\n".join(lines)
    # 日线最后一行就是当天（盘中/收盘后手动运行）：那一行不是“昨收”
    same_day = await builder._premarket_snapshot(m.target.close + timedelta(minutes=30))
    assert same_day[0].startswith(f"- MU：昨收 {fmt_price('MU', before)}")


async def test_brief_with_model_links_references(calendar, settings):
    m = build_market(calendar)
    llm = fake_llm(settings, "存储涨价延续。\n**MU**\n- HBM 扩产 [1]\n**行业**\n- DRAM 合约价上涨 [2][9]")
    hub = NewsHub([FakeNewsSource(brief_items(m))], settings.universe)
    builder = BriefBuilder(settings, calendar, FakePriceSource(m), hub, None, Storage(":memory:"), llm)
    res = await builder.build("close", now=m.target.close + timedelta(minutes=15))
    assert res.used_llm
    assert "[Reuters](https://n.example/1)" in res.message.body
    assert "[TrendForce](https://n.example/2)[9]" in res.message.body  # 不存在的编号保持原样
    call = llm.providers["fake"].calls[0]
    assert "【行情】" in call["prompt"] and "【新闻与公告条目】" in call["prompt"] and "1. [" in call["prompt"]
    assert "SK海力士 ADR（SKHY，10 份 ADR = 1 股 000660.KS；2026-07-10 在纳斯达克上市" in call["system"]
    assert "两者冲突时以这里为准" in call["system"]  # 本地模型知识过时：持仓信息优先


async def test_replay(calendar, settings):
    m = build_market(calendar, n_prior=6, shocks=CRASH, sigma_m=0.0002, sigma_e=0.0002)
    res = await run_replay(settings, calendar, FakePriceSource(m), days=2, now=m.target.close + timedelta(hours=1))
    assert [s.day for s in res.sessions] == [date(2026, 10, 8), TARGET_DAY]
    assert res.alerts and all(s.day == TARGET_DAY and a.symbol == "MU" for s, a in res.alerts)
    report = res.report(settings)
    assert "共会推送" in report and "MU" in report and "基线" in report


class FakeApp:
    def __init__(self, settings, calendar):
        self.settings, self.calendar = settings, calendar
        self.caught_up = []

    async def catch_up(self, now=None):
        self.caught_up.append(now)

    async def run_brief(self, kind): ...

    async def prepare_monitor(self, day): ...

    async def monitor_tick(self): ...


async def test_planner_handles_early_close_and_holidays(calendar, settings):
    tz = settings.tz
    sched = AsyncIOScheduler(timezone=tz)
    day = date(2026, 11, 27)  # 感恩节次日，13:00 ET 提前收盘
    planned = Planner(FakeApp(settings, calendar), sched).plan(day, now=datetime(2026, 11, 27, 6, 0, tzinfo=tz))
    jobs = {j.id: j for j in sched.get_jobs()}
    assert jobs[f"brief:premarket:{day}"].trigger.run_date == datetime(2026, 11, 27, 7, 30, tzinfo=tz)
    assert jobs[f"brief:close:{day}"].trigger.run_date == datetime(2026, 11, 27, 18, 15, tzinfo=UTC)  # 12:15 CST
    assert not any(j.startswith("monitor") for j in jobs)  # 监控是全天候的常驻任务，不按天规划
    assert any("提前收盘" in p for p in planned)

    sched2 = AsyncIOScheduler(timezone=tz)
    assert Planner(FakeApp(settings, calendar), sched2).plan(date(2026, 11, 26), now=datetime(2026, 11, 26, 6, 0, tzinfo=tz)) == []
    assert sched2.get_jobs() == []

    # 盘中重启：已经过去的任务不再安排，监控从“现在”开始
    sched3 = AsyncIOScheduler(timezone=tz)
    Planner(FakeApp(settings, calendar), sched3).plan(TARGET_DAY, now=datetime(2026, 10, 9, 11, 0, tzinfo=tz))
    ids = {j.id for j in sched3.get_jobs()}
    assert f"brief:premarket:{TARGET_DAY}" not in ids and f"brief:close:{TARGET_DAY}" in ids


class FlakyNotifier(ConsoleNotifier):
    """前 fail_times 次推送抛错（模拟断网），之后恢复。"""

    def __init__(self, fail_times):
        super().__init__(echo=False)
        self.fail_times = fail_times

    async def send(self, msg):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("network down")
        return await super().send(msg)


async def test_undelivered_alert_is_resent_on_next_tick(calendar, settings):
    settings.monitor.price.day_pct = [8.0, 12.0]
    settings.attribution.enabled = False
    m = build_market(calendar, shocks=CRASH)
    storage, notifier = Storage(":memory:"), FlakyNotifier(fail_times=1)
    mon = IntradayMonitor(settings, calendar, FakePriceSource(m), storage, notifier,
                          Attributor(settings, NewsHub([], settings.universe), no_llm(settings)))
    detected = await mon.tick(now=at(m.target, 125))
    assert [a.symbol for a in detected] == ["MU"] and notifier.sent == []  # 检测到了，但没送达
    [rec] = storage.alerts_for_day(TARGET_DAY.isoformat())
    assert storage.undelivered_alerts(TARGET_DAY.isoformat(), since=at(m.target, 0)) != []

    assert await mon.tick(now=at(m.target, 126)) == []  # 冷却照常：不会当成新的异动
    [(_, msg)] = notifier.sent  # 但上一条补发出去了
    assert msg.title.startswith("🔻 MU") and "（补发：" in msg.title
    assert storage.undelivered_alerts(TARGET_DAY.isoformat(), since=at(m.target, 0)) == []
    await mon.tick(now=at(m.target, 127))
    assert len(notifier.sent) == 1  # 送达后不再重发


async def test_undelivered_alert_given_up_after_window(calendar, settings):
    settings.monitor.price.day_pct = [8.0, 12.0]
    settings.attribution.enabled = False
    m = build_market(calendar, shocks=CRASH)
    storage, notifier = Storage(":memory:"), FlakyNotifier(fail_times=1)
    mon = IntradayMonitor(settings, calendar, FakePriceSource(m), storage, notifier,
                          Attributor(settings, NewsHub([], settings.universe), no_llm(settings)))
    await mon.tick(now=at(m.target, 125))
    await mon.tick(now=at(m.target, 125 + 40))  # 网络恢复时已经过去 40 分钟：不再补发过时的快讯
    assert all("补发" not in msg.title for _, msg in notifier.sent)


async def test_monitor_is_one_round_the_clock_job_and_watchdog_catches_up(calendar, settings):
    """异动监控是一个全天候的常驻任务（不按天规划）：Mac 休眠醒来后接着跑，错过的合并成一次。"""
    from tickersentinel.jobs.scheduler import Planner

    settings.events.enabled = False
    app = FakeApp(settings, calendar)
    sched = AsyncIOScheduler(timezone=settings.tz)
    planner = Planner(app, sched)
    planned = planner.install()
    jobs = {j.id: j for j in sched.get_jobs()}
    tick = jobs["monitor:tick"]
    assert tick.misfire_grace_time is None and tick.coalesce and tick.max_instances == 1
    assert tick.trigger.interval == timedelta(seconds=settings.monitor.interval_seconds) and tick.trigger.end_date is None
    assert any(p.startswith("异动监控") for p in planned) and not any(j.startswith("monitor:tick:") for j in jobs)
    assert jobs["catch_up"].misfire_grace_time is None
    now = datetime(2026, 10, 9, 9, 0, tzinfo=settings.tz)
    await planner.watchdog(now)
    assert app.caught_up == [now]
