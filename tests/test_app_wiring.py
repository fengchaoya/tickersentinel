"""整体装配的冒烟测试：App 组装 → 刷新事件 → 汇总 → 简报 → 关闭。不联网（数据源全部关掉，事件来源换成假的）。"""

from datetime import date, timedelta

from tickersentinel.app import App
from tickersentinel.config import LLMTasks
from tickersentinel.models import Event, earnings_key
from tickersentinel.sources.base import EventSource

from conftest import utc


class FakeEvents(EventSource):
    name = "fake"

    async def collect(self, symbols, start, end):
        d = date(2026, 10, 22)
        return [Event(kind="earnings", symbol="WDC", day=d, title="西部数据 财报", key=earnings_key("WDC", d),
                      time_hint="盘后", source="yfinance")]


async def test_app_wiring_refresh_digest_and_close(settings, tmp_path):
    s = settings
    s.sources.news.google_news_rss.enabled = False
    s.sources.news.finnhub.enabled = False
    s.sources.filings.sec_edgar.enabled = False
    s.events.sources.yfinance = s.events.sources.jpx = False
    s.llm.tasks = LLMTasks(brief=[], attribution=[], events=[])
    app = App(s, dry_run=True)
    assert app.events.sources == [] and app.news.sources == []
    assert app.notifier.log_path is None  # 预览不写 outbox.log
    app.events.sources = [FakeEvents()]
    evening = utc(2026, 10, 22, 1, 30)  # 10-21 20:30 CDT：汇总时间（20:00）已过、今天还没发 → 看门狗补发
    await app.catch_up_events(evening)
    assert app.digest.sent_today(evening)
    [(_, msg)] = app.notifier.sent
    assert msg.title == "📅 事件日历 · 10-21 周三" and "**明天**\n- 10-22 周四 盘后｜西部数据 财报（预计）" in msg.body
    await app.catch_up_events(utc(2026, 10, 22, 1, 45))  # 已经发过：不重复
    assert len(app.notifier.sent) == 1
    await app.aclose()


def offline_app(settings):
    s = settings
    s.sources.news.google_news_rss.enabled = False
    s.sources.news.finnhub.enabled = False
    s.sources.filings.sec_edgar.enabled = False
    s.events.enabled = False
    s.events.digest.enabled = False
    s.llm.tasks = LLMTasks(brief=[], attribution=[], events=[], chat=[])
    app = App(s, dry_run=True)
    built = []

    async def fake_build(kind, now=None):
        from tickersentinel.agents.brief import BriefResult
        from tickersentinel.models import Message

        built.append((kind, now))
        if getattr(app, "fail_build", False):
            raise RuntimeError("行情接口超时")
        return BriefResult(Message(title=f"{kind} 简报", body="正文", kind="brief"), [], False)

    app.briefs.build = fake_build
    return app, built


async def test_missed_premarket_brief_is_sent_until_the_open(settings):
    app, built = offline_app(settings)
    # 2026-10-21 周三：盘前简报定在 07:30 CDT，开盘 08:30 CDT
    await app.catch_up_briefs(utc(2026, 10, 21, 12, 35))  # 07:35：还在宽限期内，交给正常的定时任务
    assert built == []
    await app.catch_up_briefs(utc(2026, 10, 21, 12, 45))  # 07:45 才启动：补发
    assert [k for k, _ in built] == ["premarket"]
    await app.catch_up_briefs(utc(2026, 10, 21, 13, 0))  # 已经发过：不重复
    await app.run_brief("premarket", now=utc(2026, 10, 21, 13, 5))  # 定时任务姗姗来迟也不重复
    assert [k for k, _ in built] == ["premarket"]
    await app.run_brief("premarket", force=True, now=utc(2026, 10, 21, 13, 6))  # 手动 --send 照发
    assert [k for k, _ in built] == ["premarket", "premarket"]
    await app.aclose()


async def test_no_premarket_catch_up_after_open_and_close_brief_catch_up(settings):
    app, built = offline_app(settings)
    await app.catch_up_briefs(utc(2026, 10, 21, 13, 45))  # 08:45 已开盘：盘前简报没有意义了，不补
    assert built == []
    await app.catch_up_briefs(utc(2026, 10, 21, 21, 0))  # 16:00：收盘简报（15:15）错过了，补发
    assert [k for k, _ in built] == ["close"]
    await app.aclose()


async def test_failed_brief_retried_at_most_three_times(settings):
    app, built = offline_app(settings)
    app.fail_build = True
    for minutes in range(0, 75, 15):  # 看门狗每 15 分钟一次
        await app.catch_up_briefs(utc(2026, 10, 21, 20, 30) + timedelta(minutes=minutes))
    assert [k for k, _ in built] == ["close"] * 3
    await app.aclose()


def test_in_memory_copy_reads_real_db_without_writing(tmp_path):
    """tsen ask 预览：从正式库复制一份进内存，读得到历史数据，写入不落盘。"""
    from tickersentinel.storage import Storage

    path = tmp_path / "real.sqlite3"
    real = Storage(path)
    real.kv_set("k", "v")
    real.close()
    copy = Storage.in_memory_copy(path)
    assert copy.kv_get("k") == "v"
    copy.kv_set("k", "changed")
    copy.close()
    again = Storage(path)
    assert again.kv_get("k") == "v"
    again.close()
    assert Storage.in_memory_copy(tmp_path / "missing.sqlite3").kv_get("k") is None


async def test_missing_feishu_credentials_fall_back_to_console_and_startup_says_so(settings):
    """配置了飞书推送和同步、但没有密钥：退回终端打印，不崩溃；启动消息写明实际渠道和同步没生效。"""
    from tickersentinel.config import Secrets

    s = settings
    s.sources.news.google_news_rss.enabled = False
    s.sources.news.finnhub.enabled = False
    s.sources.filings.sec_edgar.enabled = False
    s.llm.tasks = LLMTasks(brief=[], attribution=[], events=[], chat=[])
    s.secrets = Secrets()
    s.notify.provider = "feishu"
    s.notify.feishu_sync.calendar = True
    app = App(s)
    try:
        assert app.notifier.name == "console" and app.feishu is None
        text = app._startup_text([])
        assert "推送渠道：console（配置的 feishu 不可用" in text
        assert "飞书同步：已打开（日历），但没有生效" in text
        s.notify.feishu_sync.calendar = False
        assert app._feishu_sync_status() == "飞书同步：关闭"
    finally:
        await app.aclose()
