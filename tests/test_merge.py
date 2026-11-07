"""同一主题同方向一起异动时合并推送（jobs/merge.py + IntradayMonitor）。"""

from datetime import datetime, timedelta

from tickersentinel.config import Instrument
from tickersentinel.jobs.merge import Merger, count_messages, merge_theme
from tickersentinel.models import Alert, Trigger
from tickersentinel.timeutil import UTC

from conftest import BENCH, TARGET_DAY, at, build_market
from test_pipeline import make_monitor, no_llm

DAY = TARGET_DAY.isoformat()


def crash(symbols, start, minutes=5, step=-0.006):
    return {(s, TARGET_DAY, start + i): step for s in symbols for i in range(minutes)}


def made(sym, ts, *, direction=-1, abnormal=0.2, store=None):
    """手工构造一条异动：板块（β1.4 × 基准 2%）占了大头，个股特异只有 abnormal 个点（和异动同方向）。"""
    move = 3.0 * direction
    tr = Trigger("rapid", direction, move, window_minutes=15, z=6.0 * direction,
                 anchor_ts=ts - timedelta(minutes=10), anchor_pct=0.5, minutes=10)
    a = Alert(symbol=sym, ts=ts, price=100.0, prev_close=103.0, triggers=[tr], window_minutes=10, move_pct=move,
              benchmark_move_pct=2.0 * direction, beta=1.4, abnormal_move_pct=abnormal * direction, volume_ratio=None,
              benchmark=BENCH)
    if store is not None:
        store.record_alert(a, DAY)
    return a


async def test_same_tick_alerts_of_one_theme_are_merged(calendar, settings):
    settings.monitor.price.day_pct = [8.0, 12.0]
    m = build_market(calendar, shocks=crash(("MU", "SNDK", "SKHY"), 120))
    mon, storage, notifier = make_monitor(settings, calendar, m, no_llm(settings))
    alerts = await mon.tick(now=at(m.target, 125))
    await mon.drain()
    assert sorted(a.symbol for a in alerts) == ["MU", "SKHY", "SNDK"]  # 异动照样一条条入库
    assert [msg.kind for _, msg in notifier.sent] == ["alert", "attribution"]  # 但只推一条，归因也只做一次
    mid, msg = notifier.sent[0]
    assert msg.title.startswith("🔻 存储芯片 3 只一起跌：") and all(s in msg.title for s in ("MU", "SNDK", "SKHY"))
    assert msg.body.count("分钟内从高点回落") == 3 and "拆分" in msg.body and "正在检索可能原因" in msg.body
    attr = notifier.sent[1][1]
    assert attr.reply_to == mid and attr.title.startswith("🧭 存储芯片 3 只一起跌的归因")
    rows = storage.alerts_for_day(DAY)
    assert len(rows) == 3 and not storage.undelivered_alerts(DAY, since=at(m.target, 0))  # 三条都算送达
    texts = {storage.conn.execute("SELECT attribution_text FROM alerts WHERE id = ?", (r.id,)).fetchone()[0] for r in rows}
    assert len(texts) == 1 and None not in texts  # 三条记录存的是同一份归因


async def test_later_alert_of_the_same_wave_replies_under_the_first(calendar, settings):
    settings.monitor.price.day_pct = [8.0, 12.0]
    m = build_market(calendar, shocks=crash(("MU",), 120) | crash(("SNDK",), 127))
    mon, storage, notifier = make_monitor(settings, calendar, m, no_llm(settings))
    assert [a.symbol for a in await mon.tick(now=at(m.target, 125))] == ["MU"]
    await mon.drain()
    first_id = notifier.sent[0][0]
    assert [a.symbol for a in await mon.tick(now=at(m.target, 132))] == ["SNDK"]
    await mon.drain()
    follow_id, follow = notifier.sent[2]
    assert follow.reply_to == first_id and follow.title.startswith("🔻 SNDK 也跌了：")
    assert "宏观快照见那一条" in follow.body
    # SNDK 是自己跌的（板块没动）：个股因素为主，照样单独归因，挂在跟帖下面
    assert notifier.sent[3][1].kind == "attribution" and notifier.sent[3][1].reply_to == follow_id
    assert mon.merger.threads[("memory", -1)].symbols == ["MU", "SNDK"]


async def test_sector_driven_follow_up_is_not_attributed_again(calendar, settings):
    mon, storage, notifier = make_monitor(settings, calendar, build_market(calendar), no_llm(settings))
    t0 = at(calendar.session(TARGET_DAY), 125)
    await mon._push([made("MU", t0, store=storage)])
    await mon.drain()
    n = len(notifier.sent)
    skhy = made("SKHY", t0 + timedelta(minutes=4), store=storage)  # 板块联动为主的跟帖：挂在下面，不再重复归因
    await mon._push([skhy])
    await mon.drain()
    assert [msg.kind for _, msg in notifier.sent[n:]] == ["alert"] and notifier.sent[n][1].reply_to == notifier.sent[0][0]
    note = storage.conn.execute("SELECT attribution_text FROM alerts WHERE id = ?", (skhy.id,)).fetchone()[0]
    assert note.startswith("同一波行情") and "那一条的归因" in note
    await mon._push([made("SNDK", t0 + timedelta(minutes=12), store=storage)])  # 超过 10 分钟：新的一条
    await mon.drain()
    assert notifier.sent[-2][1].kind == "alert" and notifier.sent[-2][1].reply_to is None
    await mon._push([made("MU", t0 + timedelta(minutes=13), direction=1, store=storage)])  # 反方向：新的一条
    await mon.drain()
    assert notifier.sent[-2][1].reply_to is None


async def test_merging_can_be_turned_off(calendar, settings):
    settings.monitor.price.day_pct = [8.0, 12.0]
    settings.monitor.merge_minutes = 0
    m = build_market(calendar, shocks=crash(("MU", "SNDK", "SKHY"), 120))
    mon, _, notifier = make_monitor(settings, calendar, m, no_llm(settings))
    await mon.tick(now=at(m.target, 125))
    await mon.drain()
    assert [msg.kind for _, msg in notifier.sent].count("alert") == 3


def test_merge_plan_and_replay_count(settings):
    settings.universe.watchlist.append(Instrument(symbol="AAPL", name="苹果"))  # 没写 theme：默认主题，不和别的合并
    assert merge_theme(settings, "MU") == "memory" and merge_theme(settings, "AAPL") is None
    assert merge_theme(settings, "night:memory:XKRX") == "memory" and merge_theme(settings, "285A.T") == "memory"
    t0 = datetime(2026, 10, 9, 14, 0, tzinfo=UTC)

    def a(sym, minute, direction=-1):
        return made(sym, t0 + timedelta(minutes=minute), direction=direction)

    plan = Merger(10).plan([a("MU", 0), a("SNDK", 0), a("AAPL", 0), a("SKHY", 0, 1)], settings)
    assert [[x.symbol for x in b.alerts] for b in plan] == [["MU", "SNDK"], ["AAPL"], ["SKHY"]]
    # 回放：同一分钟的 MU + SNDK 一条；5 分钟后 SKHY 同方向挂在下面；15 分钟后 MU 又是新的一条；AAPL 单独
    alerts = [a("MU", 0), a("SNDK", 0), a("SKHY", 5), a("MU", 15), a("AAPL", 15)]
    assert count_messages(alerts, settings) == (4, 1)
    settings.monitor.merge_minutes = 0
    assert count_messages(alerts, settings) == (5, 0)


async def test_failed_merged_push_is_resent_one_by_one(calendar, settings):
    """合成的那一条没发出去：三条都算未送达、不建串；下一次检测逐条补发，各自归因。"""
    settings.monitor.price.day_pct = [8.0, 12.0]
    m = build_market(calendar, shocks=crash(("MU", "SNDK", "SKHY"), 120))
    mon, storage, notifier = make_monitor(settings, calendar, m, no_llm(settings))
    real_send, calls = notifier.send, []

    async def flaky(msg):
        calls.append(msg.kind)
        if len(calls) == 1:
            raise RuntimeError("网络断了")
        return await real_send(msg)

    notifier.send = flaky
    await mon.tick(now=at(m.target, 125))
    await mon.drain()
    assert len(storage.undelivered_alerts(DAY, since=at(m.target, 0))) == 3 and not mon.merger.threads
    await mon.tick(now=at(m.target, 126))
    await mon.drain()
    assert not storage.undelivered_alerts(DAY, since=at(m.target, 0))
    sent = [msg for _, msg in notifier.sent]
    assert [x.kind for x in sent].count("alert") == 3 and all("补发" in x.title for x in sent if x.kind == "alert")
    assert [x.kind for x in sent].count("attribution") == 3


async def test_follow_up_without_attribution_writes_no_note(calendar, settings):
    settings.attribution.enabled = False
    mon, storage, _ = make_monitor(settings, calendar, build_market(calendar), no_llm(settings))
    t0 = at(calendar.session(TARGET_DAY), 125)
    await mon._push([made("MU", t0, store=storage)])
    skhy = made("SKHY", t0 + timedelta(minutes=4), store=storage)
    await mon._push([skhy])
    assert storage.conn.execute("SELECT attribution_text FROM alerts WHERE id = ?", (skhy.id,)).fetchone()[0] is None


def test_overnight_signal_keeps_its_own_theme_even_if_a_spare_theme_lists_it(settings):
    from tickersentinel.config import Theme

    u = settings.universe
    u.themes = {"dram": Theme(key="dram", name="DRAM", peers={"000660.KS": "海力士"}), **u.themes}  # 备用主题排在前面
    assert merge_theme(settings, "000660.KS") == "memory" and u.theme_of("000660.KS").key == "memory"


def test_follow_up_summary_points_to_the_first_message(settings):
    from tickersentinel.storage import Storage

    st = Storage(":memory:")
    t0 = datetime(2026, 10, 9, 14, 0, tzinfo=UTC)
    first, follow, plain = made("MU", t0, store=st), made("SNDK", t0 + timedelta(minutes=3), store=st), made("SKHY", t0, store=st)
    st.save_attribution(first.id, "长文", {"summary": "同业下调指引带动板块"})
    st.save_attribution(follow.id, "同一波行情", {"summary": "同一波行情", "same_wave_as": first.id})
    st.save_attribution(plain.id, "**按时间接近程度**列出的新闻（没有大模型）\n- …", None)  # 降级归因：取第一行
    assert st.attribution_summary(follow.id) == "同一波行情：同业下调指引带动板块"
    assert st.attribution_summary(plain.id) == "按时间接近程度列出的新闻（没有大模型）"


async def test_context_estimated_before_a_hot_reload_is_not_written_back(calendar, settings):
    """估计基线的时候监控范围热更新了（飞书 /scope 确认）：按旧分组估出来的上下文不写回，下一次检测重估。"""
    mon, _, _ = make_monitor(settings, calendar, build_market(calendar), no_llm(settings))
    group = next(g for g in mon.groups if g.key == "us")
    seg = group.clock.segment_at(at(calendar.session(TARGET_DAY), 60))
    import tickersentinel.jobs.monitor as monitor_mod

    real = monitor_mod.prepare_context

    async def slow(*a, **kw):
        mon.reload()  # 估计到一半，热更新发生
        return await real(*a, **kw)

    monitor_mod.prepare_context = slow
    try:
        await mon.prepare(group, seg)
    finally:
        monitor_mod.prepare_context = real
    assert "us" not in mon.ctx
