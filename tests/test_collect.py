"""分钟线采集（jobs/collect.py）：要采哪些代码、增量起点、分批、休市不算失败、补历史、夜盘、热更新、收益率曲线。"""

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from conftest import isolate
from tickersentinel.barstore import BarStore
from tickersentinel.config import AlertRules
from tickersentinel.jobs.collect import OVERLAP, BarCollector, collect_targets
from tickersentinel.sources.base import NoDataError, PriceSource, SourceError
from tickersentinel.sources.macro import TreasuryCurve
from tickersentinel.timeutil import UTC

NOW = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)  # 周三 11:00 美东


def frame(start: datetime, end: datetime, base: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range(start, end, freq="1min", tz=UTC, inclusive="left")
    close = base + np.arange(len(idx)) * 0.01
    return pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "volume": 1.0}, index=idx)


class FakeDay(PriceSource):
    name = "fake-day"

    def __init__(self, *, empty: bool = False, fail: set[str] | None = None):
        self.calls: list[tuple[list[str], datetime, datetime, bool]] = []
        self.empty = empty
        self.fail = fail or set()

    async def intraday_bars(self, symbols, start, end=None, *, prepost=False):
        self.calls.append((list(symbols), start, end, prepost))
        if self.fail & set(symbols):
            raise SourceError("boom")
        if self.empty:
            raise NoDataError("休市")
        return {s: frame(start, end) for s in symbols}

    async def daily_bars(self, symbols, lookback_days):  # pragma: no cover
        return {}

    async def latest_quotes(self, symbols):  # pragma: no cover
        return {}


class FakeNight(FakeDay):
    def __init__(self):
        super().__init__()
        self.night_calls: list[tuple[list[str], datetime, datetime]] = []

    async def overnight_bars(self, symbols, start, end=None):
        self.night_calls.append((list(symbols), start, end))
        return {s: frame(start, start + timedelta(minutes=5), base=50.0) for s in symbols}


class FakeMacro:
    def __init__(self):
        self.years: list[int] = []

    async def treasury_year(self, year):
        self.years.append(year)
        return [TreasuryCurve(date(year, 10, 1), {"2Y": 3.5, "10Y": 4.0})]


def make(settings, calendar, **kw) -> tuple[BarCollector, BarStore, FakeDay]:
    store = BarStore(":memory:")
    day = kw.pop("day", None) or FakeDay()
    col = BarCollector(settings, calendar, store, day=day, clock=lambda: NOW, **kw)
    return col, store, day


def test_targets_cover_macro_and_monitor_groups(settings, calendar):
    t = collect_targets(settings, calendar)
    for sym in ("^TNX", "CL=F", "^GSPC", "ES=F", "MU", "SNDK", "SKHY", "SOXX", "000660.KS", "KRW=X"):
        assert sym in t, sym
    assert not any(s.startswith(("basket:", "night:")) for s in t)
    assert t["MU"].prepost and not t["MU"].night
    settings.macro.enabled = False
    assert "^TNX" not in collect_targets(settings, calendar)


def test_targets_mark_24x5_holdings_for_night(settings, calendar):
    isolate(settings, ("MU",))
    settings.universe.classes["us_equity"] = AlertRules(hours="24x5")
    t = collect_targets(settings, calendar)
    assert t["MU"].night and not t["^TNX"].night


async def test_new_symbols_are_backfilled_in_background(settings, calendar):
    isolate(settings, ("MU",))
    settings.macro.enabled = False
    col, store, day = make(settings, calendar)
    assert await col.collect() == 0  # 这一轮只把新代码交给后台
    await col.wait_backfill()
    assert day.calls and all(c[1] == NOW - timedelta(days=settings.bars.backfill_days) for c in day.calls)
    assert all(len(c[0]) == 1 for c in day.calls)  # 补历史逐个代码下载
    cov = store.coverage("MU")
    assert cov.first == NOW - timedelta(days=29) and cov.last == NOW
    # 下一轮是增量：从最后一根往前 OVERLAP，同类代码一次批量下载
    day.calls.clear()
    later = NOW + timedelta(minutes=2)
    col.clock = lambda: later
    await col.collect(later)
    assert len(day.calls) == 1
    syms, start, end, prepost = day.calls[0]
    assert set(syms) == set(col.targets()) and start == NOW - OVERLAP and prepost
    assert col.status.failures == 0 and col.status.last_ok == later


async def test_market_closed_is_not_a_failure_and_moves_checked_mark(settings, calendar):
    isolate(settings, ("MU",))
    settings.macro.enabled = False
    col, store, day = make(settings, calendar)
    store.upsert("MU", frame(NOW - timedelta(days=3), NOW - timedelta(days=3) + timedelta(minutes=10)))
    for s in col.targets():
        store.upsert(s, frame(NOW - timedelta(days=3), NOW - timedelta(days=3) + timedelta(minutes=10)))
    day.empty = True
    await col.collect()
    assert col.status.failures == 0
    first_start = day.calls[0][1]
    assert first_start < NOW - timedelta(days=2)  # 第一轮从库里最后一根补起（空档）
    day.calls.clear()
    later = NOW + timedelta(minutes=2)
    await col.collect(later)
    assert day.calls[0][1] == NOW - OVERLAP  # 之后不再每轮从三天前重查


async def test_source_error_counts_failures(settings, calendar):
    isolate(settings, ("MU",))
    settings.macro.enabled = False
    col, store, day = make(settings, calendar, day=FakeDay(fail={"MU"}))
    for s in col.targets():
        store.upsert(s, frame(NOW - timedelta(minutes=30), NOW - timedelta(minutes=20)))
    await col.collect()
    await col.collect(NOW + timedelta(minutes=2))
    assert col.status.failures == 2 and "boom" in col.status.last_error and col.status.last_ok is None
    js = col.status_json()
    assert js["failures"] == 2 and js["symbols"] == len(col.targets())


async def test_gap_and_recent_symbols_fetched_separately(settings, calendar):
    isolate(settings, ("MU",))
    settings.macro.enabled = False
    col, store, day = make(settings, calendar)
    syms = list(col.targets())
    assert len(syms) >= 2
    store.upsert(syms[0], frame(NOW - timedelta(minutes=30), NOW - timedelta(minutes=10)))
    for s in syms[1:]:
        store.upsert(s, frame(NOW - timedelta(days=2), NOW - timedelta(days=2) + timedelta(minutes=5)))
    await col.collect()
    starts = sorted(c[1] for c in day.calls)
    assert len(day.calls) == 2
    assert starts[1] == NOW - timedelta(minutes=11) - OVERLAP  # 最近一批不被空档拖到两天前
    assert starts[0] < NOW - timedelta(days=1)


async def test_night_bars_stored_with_own_source(settings, calendar):
    isolate(settings, ("MU",))
    settings.macro.enabled = False
    settings.universe.classes["us_equity"] = AlertRules(hours="24x5")
    night = FakeNight()
    col, store, day = make(settings, calendar, night=night)
    for s in col.targets():
        store.upsert(s, frame(NOW - timedelta(minutes=30), NOW - timedelta(minutes=10)))
    await col.collect()
    assert night.night_calls and night.night_calls[0][0] == ["MU"]
    src = store.conn.execute("SELECT DISTINCT source FROM bars WHERE symbol = 'MU'").fetchall()
    assert {r[0] for r in src} >= {"hyperliquid", "yfinance"}


async def test_reload_picks_up_new_scope(settings, calendar):
    col, store, day = make(settings, calendar)
    before = set(col.targets())
    assert "SNDK" in before
    isolate(settings, ("MU",))
    assert set(col.targets()) == before  # 缓存着
    col.reload()
    assert "SNDK" not in col.targets() and "MU" in col.targets()


async def test_curve_and_compaction_housekeeping(settings, calendar):
    isolate(settings, ("MU",))
    settings.bars.treasury_years = 2
    macro = FakeMacro()
    col, store, day = make(settings, calendar, macro=macro)
    old = NOW - timedelta(days=settings.bars.keep_minute_days + 1)
    store.upsert("MU", frame(old, old + timedelta(minutes=10)))
    await col.collect()
    await col.wait_backfill()
    assert macro.years == [2025, 2026]
    assert store.coverage("MU").rows_5m == 2
    assert not store.curve(date(2025, 1, 1)).empty
    # 12 小时内不再取；之后只取今年（去年库里已经有了）
    await col.collect(NOW + timedelta(hours=1))
    assert macro.years == [2025, 2026]
    await col.collect(NOW + timedelta(hours=13))
    assert macro.years == [2025, 2026, 2026]


@pytest.mark.parametrize("dry_run", [True, False])
async def test_app_wires_store_and_collector(settings, dry_run):
    from tickersentinel.app import App

    async with App(settings, dry_run=dry_run) as app:
        assert app.bars is not None
        assert (app.collector is None) == dry_run
        ctx = app.web_context()
        assert ctx.bars is app.bars
        if not dry_run:
            app.write_heartbeat()
            import json
            hb = json.loads(app.storage.kv_get("runtime:heartbeat"))
            assert hb["bars"]["symbols"] >= 0


def test_planner_installs_round_the_clock_collect_job(settings, calendar):
    from types import SimpleNamespace

    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from tickersentinel.jobs.scheduler import Planner

    col, store, day = make(settings, calendar)

    async def collect_bars():  # pragma: no cover
        return 0

    app = SimpleNamespace(settings=settings, collector=col, collect_bars=collect_bars)
    sched = AsyncIOScheduler(timezone=settings.tz)
    lines = Planner(app, sched)._install_bars()
    job = {j.id: j for j in sched.get_jobs()}["bars:collect"]
    assert job.misfire_grace_time is None and job.coalesce and job.max_instances == 1
    assert job.trigger.interval == timedelta(seconds=settings.bars.collect_seconds)
    assert lines and lines[0].startswith("分钟线库：每 2 分钟")
    app.collector = None
    assert Planner(app, AsyncIOScheduler(timezone=settings.tz))._install_bars() == []


class PartlyLimited(FakeDay):
    """一部分代码取数失败（限流），其余正常——像 YFinancePriceSource 那样在结果的 failed 里列出。"""

    def __init__(self, bad: set[str]):
        super().__init__()
        self.bad = set(bad)

    async def intraday_bars(self, symbols, start, end=None, *, prepost=False):
        from tickersentinel.sources.prices_yfinance import Bars

        self.calls.append((list(symbols), start, end, prepost))
        return Bars({s: frame(start, end) for s in symbols if s not in self.bad},
                    failed={s: "YFRateLimitError('Too Many Requests')" for s in symbols if s in self.bad})


async def test_rate_limited_symbols_are_counted_and_refetched_from_the_gap(settings, calendar):
    """限流的代码不推进“查到哪一刻”、计入失败；恢复后从库里最后一根往前重取，断掉的那段补上（以前当成休市，永久缺）。"""
    isolate(settings, ("MU",))
    settings.macro.enabled = False
    col, store, day = make(settings, calendar, day=PartlyLimited({"MU"}))
    for s in col.targets():
        store.upsert(s, frame(NOW - timedelta(minutes=30), NOW - timedelta(minutes=20)))
    await col.collect()
    assert col.status.failures == 1 and "MU" in col.status.last_error and "Too Many" in col.status.last_error
    assert "MU" not in col._checked and all(col._checked[s] == NOW for s in col.targets() if s != "MU")
    assert store.last_ts("MU") == NOW - timedelta(minutes=21)  # MU 没写进新数据
    day.bad.clear()
    day.calls.clear()
    later = NOW + timedelta(minutes=2)
    await col.collect(later)
    assert col.status.failures == 0 and col._checked["MU"] == later
    assert min(c[1] for c in day.calls if "MU" in c[0]) <= NOW - timedelta(minutes=21) - OVERLAP
    assert store.last_ts("MU") >= NOW  # 断掉的那段补上了


async def test_failed_backfill_is_retried_even_when_the_store_has_data(settings, calendar):
    """补历史只补到一部分（库里已经有这个代码的数据，不再算“新代码”）：过 RETRY_FAILED 走 retry 整段重补，把中间的空档填上。"""
    isolate(settings, ("MU",))
    settings.macro.enabled = False
    col, store, day = make(settings, calendar, day=PartlyLimited({"MU"}))
    for s in col.targets():
        store.upsert(s, frame(NOW - timedelta(days=2), NOW - timedelta(days=2) + timedelta(minutes=10)))
    out = await col.backfill(["MU"])
    assert out["MU"] == -1 and "MU" in col._failed and store.last_ts("MU") is not None
    day.bad.clear()
    await col.collect(NOW + timedelta(minutes=10))  # 还没到 RETRY_FAILED：不重补
    await col.wait_backfill()
    assert "MU" in col._failed and store.coverage("MU").first > NOW - timedelta(days=28)
    later = NOW + timedelta(minutes=31)
    col.clock = lambda: later
    await col.collect(later)
    await col.wait_backfill()
    assert "MU" not in col._failed and store.coverage("MU").first <= NOW - timedelta(days=28)


async def test_app_alerts_once_per_failure_streak_and_times_out_hung_rounds(settings, monkeypatch):
    import asyncio

    from tickersentinel.app import App

    settings.bars.alert_after_failures = 3
    async with App(settings, dry_run=False) as app:
        sent: list[str] = []

        async def notify(text):
            sent.append(text)

        app._notify_error = notify

        async def boom():
            raise SourceError("[yfinance] 分钟线取数失败：Too Many Requests")

        clock = iter(NOW + timedelta(minutes=2 * i) for i in range(1000))

        async def ok():  # 真正的 collect 成功一轮：清零、记下成功的时刻
            app.collector.status.failures = 0
            app.collector.status.last_ok = next(clock)
            return 7

        app.collector.collect = boom
        for _ in range(5):
            assert await app.collect_bars() == 0
        assert app.collector.status.failures == 5 and len(sent) == 1
        assert "连续失败 3 轮" in sent[0] and "Too Many Requests" in sent[0]
        app.collector.collect = ok
        assert await app.collect_bars() == 7
        app.collector.collect = boom
        for _ in range(3):
            await app.collect_bars()
        assert len(sent) == 2  # 新的一段连续失败，再提醒一次
        await ok()  # 网页上点“现在补抓”成功了一轮（直接调 collect，不经过 collect_bars）
        app.collector.collect = boom
        for _ in range(3):
            await app.collect_bars()
        assert len(sent) == 3  # 照样算新的一段

        # 某个请求挂住：放弃这一轮（记为失败），不会让 max_instances=1 的任务从此停摆
        monkeypatch.setattr(app, "job_timeout", lambda job: 0.05)

        async def hang(*a, **kw):
            await asyncio.sleep(30)

        app.collector.collect = hang
        app.collector.status.failures = 0
        assert await app.collect_bars() == 0
        assert app.collector.status.failures == 1 and "放弃了这一轮" in app.collector.status.last_error
        app.monitor.tick = hang
        await app.monitor_tick()
        assert app._tick_failures == 1 and app.last_tick_ok is False
