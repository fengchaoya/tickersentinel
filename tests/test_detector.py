from datetime import timedelta

import pytest

from tickersentinel.agents.render import render_alert, sector_judgment
from tickersentinel.config import PriceRules
from tickersentinel.detect.baseline import build_session_context, five_min_returns, robust_std, session_frame
from tickersentinel.detect.cooldown import AlertGate
from tickersentinel.detect.price import PriceAnomalyDetector

from conftest import BENCH, TARGET_DAY, at, build_market


def make_ctx(market, cfg, watch=("MU", "SNDK", "SKHY")):
    target = market.target
    return build_session_context(
        session=target, prior_sessions=market.sessions[:-1], intraday=market.intraday, daily=market.daily,
        watchlist=list(watch), benchmark=BENCH, others=["WDC"], cfg=cfg,
    )


def frames_for(market):
    return {s: session_frame(df, market.target) for s, df in market.intraday.items()}


def run_day(market, cfg, ctx=None):
    """逐分钟回放当天，返回经过冷却后的异动列表。"""
    ctx = ctx or make_ctx(market, cfg)
    det, gate, frames = PriceAnomalyDetector(cfg), AlertGate(cfg), frames_for(market)
    fired = []
    for k in range(1, market.target.minutes + 1):
        for cand in det.evaluate(ctx, frames, as_of=at(market.target, k)):
            a = gate.filter(cand, TARGET_DAY)
            if a:
                gate.commit(a, TARGET_DAY)
                fired.append(a)
    return fired


def test_baseline_estimates(calendar):
    m = build_market(calendar)
    cfg = PriceRules()
    ctx = make_ctx(m, cfg)
    mu = ctx.baselines["MU"]
    assert not mu.beta_fallback and 0.8 < mu.beta < 1.8  # 真值 1.3（日线估计有抽样误差）
    # 分钟真实波动 ≈ sqrt(1.3²·0.0004² + 0.0004²) ≈ 0.00066
    assert mu.sigma_1m == pytest.approx(0.00066, rel=0.25)
    assert mu.volume_profile is not None and len(mu.volume_profile) == 390
    assert ctx.prev_close["MU"] == pytest.approx(float(m.daily["MU"]["close"].iloc[-2]))


def test_robust_std_and_five_min_returns(calendar):
    m = build_market(calendar)
    f = session_frame(m.intraday["MU"], m.sessions[0])
    r = five_min_returns(f)
    assert len(r) == 77  # 390 分钟 → 78 个采样点 → 77 个收益
    assert robust_std([0.0] * 10) is None  # 样本太少


def test_calm_day_produces_no_alerts(calendar):
    m = build_market(calendar, sigma_m=0.0002, sigma_e=0.0002)  # 日内波动约 0.6%，到不了任何档位
    assert run_day(m, PriceRules()) == []


def test_day_alerts_measure_from_the_right_anchor(calendar):
    """全天累计：起点是昨收与日内高（低）点里更远的那个；起点较昨收 + 走过的点数 = 现价较昨收。"""
    m = build_market(calendar)  # 合成市场一天只走 1% 上下：档位放低，看口径对不对
    fired = run_day(m, PriceRules(day_pct=[0.8, 1.6]))
    days = [(a, t) for a in fired for t in a.triggers if t.rule == "day"]
    assert days
    for a, t in days:
        assert t.move_pct * t.direction >= t.level_pct
        assert t.anchor_pct + t.move_pct == pytest.approx(a.day_change_pct)
        if t.anchor_ts is None:
            assert t.anchor_pct == 0 and t.context_window is None  # 从昨收算：拆分用“较昨收”
        else:  # 从日内高点（下跌）/ 低点（上涨）算：起点在昨收的另一侧
            assert t.anchor_pct * t.direction < 0
            assert t.minutes is not None and t.context_window == t.minutes


def test_idiosyncratic_crash_triggers_rapid_alert(calendar):
    shocks = {("MU", TARGET_DAY, 120 + i): -0.006 for i in range(5)}  # 第 120–124 分钟，共跌约 3%
    m = build_market(calendar, shocks=shocks)
    cfg = PriceRules()
    ctx = make_ctx(m, cfg)
    # as_of=第125分钟 → 最新K线是第124分钟，5分钟窗口 (119, 124] 恰好包含全部冲击
    alerts = PriceAnomalyDetector(cfg).evaluate(ctx, frames_for(m), as_of=at(m.target, 125))
    mu = [a for a in alerts if a.symbol == "MU"]
    assert len(mu) == 1
    a = mu[0]
    assert a.headline.rule == "rapid" and a.direction == -1 and a.headline.window_minutes == 5
    assert a.move_pct == pytest.approx(-3.0, abs=0.4)
    assert a.headline.z < -cfg.z_threshold
    assert abs(a.abnormal_move_pct - a.move_pct) < 0.5  # 板块没动 → 几乎全是个股特异
    assert sector_judgment(a) == "以个股因素为主"
    assert {"SNDK", "SKHY", "WDC", "SOXX"} <= set(a.peers_move_pct)
    assert not [x for x in alerts if x.symbol != "MU"]


def test_crash_alert_then_escalation_then_level(calendar):
    shocks = {("MU", TARGET_DAY, 120 + i): -0.006 for i in range(5)}
    fired = run_day(build_market(calendar, shocks=shocks), PriceRules())
    mu = [a for a in fired if a.symbol == "MU"]
    rules = [a.headline.rule for a in mu]
    assert rules[0] == "rapid" and not mu[0].is_escalation
    assert len(mu) <= 4  # 冷却生效：一波下跌不会每分钟推一次


def test_split_context_follows_headline_after_cooldown(calendar):
    """急跌推送后、冷却期内第二波首次越过全天的 3% 档：冷却去掉急跌触发、只剩全天累计，拆分要改用它的口径。"""
    shocks = {("MU", TARGET_DAY, 120 + i): -0.006 for i in range(5)}  # 第一波：5 分钟跌约 3%
    shocks[("MU", TARGET_DAY, 130)] = -0.012  # 第二波：再跌约 1.2%，从全天的起点算首次超过 3%
    m = build_market(calendar, shocks=shocks)
    cfg = PriceRules(day_pct=[3])
    ctx = make_ctx(m, cfg)
    det, gate, frames = PriceAnomalyDetector(cfg), AlertGate(cfg), frames_for(m)
    fired = []  # (候选, 过滤后)
    for k in range(1, 150):
        for cand in det.evaluate(ctx, frames, as_of=at(m.target, k)):
            if cand.symbol != "MU":
                continue
            a = gate.filter(cand, TARGET_DAY)
            if a:
                gate.commit(a, TARGET_DAY)
                fired.append((cand, a))

    first = fired[0][1]
    assert first.headline.rule == "rapid" and first.headline.minutes <= 6 and first.window_minutes == first.headline.minutes
    cand, a = next((c, x) for c, x in fired if x.headline.rule == "day")
    assert a.ts - first.ts < timedelta(minutes=cfg.cooldown_minutes)  # 确实在急跌的冷却期内
    # 候选的标题是急跌（从几分钟前的高点算），冷却把急跌去掉后只剩全天累计
    assert cand.headline.rule == "rapid" and cand.window_minutes == cand.headline.context_window
    assert [t.rule for t in a.triggers] == ["day"] and a.headline.level_pct == 3 and not a.is_escalation
    # 拆分上下文与新标题一致：全天累计的起点（昨收，或日内高点）
    w = a.headline.context_window
    assert a.window_minutes == w and w != cand.window_minutes
    i = int((a.ts - m.target.open).total_seconds() // 60)
    bclose = frames[BENCH]["close"]
    bprev = ctx.prev_close[BENCH]  # 拆分和触发同一口径：按参照价的百分点
    start = bprev if w is None else bclose.iloc[i - w]
    assert a.benchmark_move_pct == pytest.approx((bclose.iloc[i] - start) / bprev * 100)
    assert a.abnormal_move_pct == pytest.approx(a.move_pct - a.beta * a.benchmark_move_pct)
    assert a.peers_move_pct[BENCH] == pytest.approx(a.benchmark_move_pct)  # 同业同期也是同一口径


def test_sector_move_is_attributed_to_sector(calendar):
    shocks = {(BENCH, TARGET_DAY, 200 + i): -0.005 for i in range(5)}  # SOXX 5 分钟跌约 2.5%
    m = build_market(calendar, shocks=shocks)
    cfg = PriceRules(idiosyncratic={"enabled": True})
    alerts = PriceAnomalyDetector(cfg).evaluate(make_ctx(m, cfg), frames_for(m), as_of=at(m.target, 205))
    mu = next(a for a in alerts if a.symbol == "MU")
    assert mu.headline.rule == "rapid" and mu.direction == -1
    assert "idio" not in [t.rule for t in mu.triggers]  # 剔除板块后不异常
    assert sector_judgment(mu) == "以板块联动为主"
    assert mu.benchmark_move_pct == pytest.approx(-2.5, abs=0.4)


def test_idiosyncratic_rule_when_enabled(calendar):
    shocks = {("SKHY", TARGET_DAY, 150 + i): 0.0018 for i in range(15)}  # 15 分钟涨约 2.7%，板块不动
    m = build_market(calendar, shocks=shocks)
    cfg = PriceRules(idiosyncratic={"enabled": True, "z_threshold": 4.0, "min_move_pct": 2.0})
    alerts = PriceAnomalyDetector(cfg).evaluate(make_ctx(m, cfg), frames_for(m), as_of=at(m.target, 166))
    sk = next(a for a in alerts if a.symbol == "SKHY")
    assert "idio" in [t.rule for t in sk.triggers] and sk.direction == 1


def test_opening_gap_triggers_day_not_rapid(calendar):
    shocks = {("SNDK", TARGET_DAY, 0): -0.065}  # 开盘跳空约 -6.3%
    m = build_market(calendar, shocks=shocks)
    cfg = PriceRules()
    alerts = PriceAnomalyDetector(cfg).evaluate(make_ctx(m, cfg), frames_for(m), as_of=at(m.target, 2))
    sn = next(a for a in alerts if a.symbol == "SNDK")
    assert [t.rule for t in sn.triggers] == ["day"]
    assert sn.headline.level_pct == 5 and sn.headline.anchor_ts is None and sn.window_minutes is None  # 起点是昨收
    # 预热期内急涨急跌规则不运行
    assert all(t.rule != "rapid" for a in alerts for t in a.triggers)


def test_render_alert_message(calendar, settings):
    shocks = {("MU", TARGET_DAY, 120 + i): -0.006 for i in range(5)}
    m = build_market(calendar, shocks=shocks)
    cfg = settings.monitor.price
    alert = next(a for a in PriceAnomalyDetector(cfg).evaluate(make_ctx(m, cfg), frames_for(m), as_of=at(m.target, 125)) if a.symbol == "MU")
    msg = render_alert(alert, settings)
    assert msg.title.startswith("🔻 MU 5分钟急跌")
    assert "拆分" in msg.body and "SOXX" in msg.body and "个股特异" in msg.body
    assert "CDT" in msg.body  # 本地时区（夏令时）
    assert "正在检索可能原因" in msg.body


def test_as_of_truncation_prevents_lookahead(calendar):
    shocks = {("MU", TARGET_DAY, 300 + i): -0.006 for i in range(5)}
    m = build_market(calendar, shocks=shocks)
    cfg = PriceRules()
    ctx = make_ctx(m, cfg)
    det = PriceAnomalyDetector(cfg)
    # 冲击发生在第 300 分钟，之前的时点不应看到它
    assert not [a for a in det.evaluate(ctx, frames_for(m), as_of=at(m.target, 299)) if a.symbol == "MU"]
    assert [a for a in det.evaluate(ctx, frames_for(m), as_of=at(m.target, 306)) if a.symbol == "MU"]


def test_partial_day_frame_has_nan_after_last_bar(calendar):
    m = build_market(calendar)
    partial = m.intraday["MU"][m.intraday["MU"].index < at(m.target, 60)]
    f = session_frame(partial, m.target)
    assert f.attrs["n_obs"] == 60
    assert f["close"].iloc[:60].notna().all() and f["close"].iloc[60:].isna().all()
    assert len(f) == 390 and f.index[0] == m.target.open and f.index[-1] == m.target.close - timedelta(minutes=1)


def test_reversal_from_intraday_high_is_caught(calendar):
    """读者举的例子：先涨 2% 多，再在 40 分钟里跌到水下，从高点算回落 3 个点以上。
    原来的规则看 30 分钟窗口的起点到现价（要 3.5%），40 分钟里分摊开的回落一条都推不出来。"""
    shocks = {("MU", TARGET_DAY, 60 + i): 0.0008 for i in range(30)}  # 10:30–11:00 涨约 2.4%
    shocks.update({("MU", TARGET_DAY, 90 + i): -0.00095 for i in range(40)})  # 11:00–11:40 跌约 3.8%
    fired = [a for a in run_day(build_market(calendar, shocks=shocks), PriceRules()) if a.symbol == "MU"]
    swing = next(a for a in fired if a.headline.rule == "rapid" and a.direction < 0)
    t = swing.headline
    assert t.window_minutes in (30, 60) and t.move_pct <= -3.0 and abs(t.z) >= 4
    assert t.anchor_pct > 1.0 and 20 <= t.minutes <= 45  # 起点是上午冲高的那个高点，用了 20 多分钟
    i_anchor = int((t.anchor_ts - TARGET_DAY_OPEN(calendar)).total_seconds() // 60)
    assert 80 <= i_anchor <= 95
    assert "分钟内从高点回落" in t.describe() and "较昨收 +" in t.describe()


def test_slow_all_day_decline_is_pushed_by_day_steps(calendar):
    """一路阴跌：每个 60 分钟都不到 3 个点，但一天累计跌到 -8% 以上，按全天的 5%、8% 两档各推一次。"""
    shocks = {("MU", TARGET_DAY, 30 + i): -0.0003 for i in range(340)}  # 约 -10%（对数），每小时约 -1.8%
    fired = [a for a in run_day(build_market(calendar, shocks=shocks), PriceRules()) if a.symbol == "MU"]
    rules = [t.rule for a in fired for t in a.triggers]
    assert "rapid" not in rules
    assert [t.level_pct for a in fired for t in a.triggers if t.rule == "day"] == [5, 8]
    assert all(a.direction < 0 for a in fired)


def TARGET_DAY_OPEN(calendar):
    return calendar.session(TARGET_DAY).open


def test_split_uses_the_benchmark_price_as_of_the_alert_minute(calendar):
    """基准停止报价（盘前盘后常见）之后标的自己急跌：拆分按基准截至此刻的最新价算，板块部分是 0、全算个股。"""
    shocks = {(BENCH, TARGET_DAY, 100 + i): -0.002 for i in range(5)} | {("MU", TARGET_DAY, 150 + i): -0.006 for i in range(5)}
    m = build_market(calendar, shocks=shocks)
    m.intraday[BENCH] = m.intraday[BENCH][m.intraday[BENCH].index < at(m.target, 110)]  # 第 110 分钟之后没有成交
    cfg = PriceRules()
    alerts = PriceAnomalyDetector(cfg).evaluate(make_ctx(m, cfg), frames_for(m), as_of=at(m.target, 155))
    mu = next(a for a in alerts if a.symbol == "MU")
    assert mu.benchmark_move_pct == pytest.approx(0.0, abs=1e-9) and mu.abnormal_move_pct == pytest.approx(mu.move_pct)
