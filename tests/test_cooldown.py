from datetime import date, timedelta

from tickersentinel.config import PriceRules
from tickersentinel.detect.cooldown import AlertGate
from tickersentinel.models import Alert, Trigger
from tickersentinel.storage import Storage

from conftest import utc

DAY = date(2026, 10, 9)
T0 = utc(2026, 10, 9, 15, 0)


def alert(*triggers, ts=T0, symbol="MU"):
    return Alert(symbol=symbol, ts=ts, price=100.0, prev_close=103.0, triggers=list(triggers), window_minutes=5,
                 move_pct=triggers[0].move_pct, benchmark_move_pct=-0.2, beta=1.3, abnormal_move_pct=-2.5, volume_ratio=2.0)


def rapid(move, w=5, z=-6.0):
    return Trigger("rapid", 1 if move > 0 else -1, move, window_minutes=w, z=z)


def level(move, lv):
    return Trigger("level", 1 if move > 0 else -1, move, level_pct=lv)


def pass_through(gate, a, day=DAY):
    out = gate.filter(a, day)
    if out:
        gate.commit(out, day)
    return out


def test_same_direction_suppressed_within_cooldown():
    gate = AlertGate(PriceRules(cooldown_minutes=15))
    assert pass_through(gate, alert(rapid(-2.0))) is not None
    assert pass_through(gate, alert(rapid(-2.1), ts=T0 + timedelta(minutes=5))) is None
    assert pass_through(gate, alert(rapid(-2.0), ts=T0 + timedelta(minutes=16))) is not None


def test_effective_cooldown_is_at_least_the_window():
    gate = AlertGate(PriceRules(cooldown_minutes=15))
    assert pass_through(gate, alert(rapid(-3.6, w=30))) is not None
    # 30 分钟窗口在第 20 分钟仍“看得到”同一波下跌，不应重复推送
    assert pass_through(gate, alert(rapid(-3.7, w=30), ts=T0 + timedelta(minutes=20))) is None
    assert pass_through(gate, alert(rapid(-3.7, w=30), ts=T0 + timedelta(minutes=31))) is not None


def test_escalation_passes_within_cooldown():
    gate = AlertGate(PriceRules(cooldown_minutes=15, escalation_factor=1.5))
    pass_through(gate, alert(rapid(-2.0)))
    out = pass_through(gate, alert(rapid(-3.1, w=15), ts=T0 + timedelta(minutes=3)))
    assert out is not None and out.is_escalation
    # 再次扩大需要相对上一次（-3.1%）再放大 1.5 倍
    assert pass_through(gate, alert(rapid(-4.0, w=15), ts=T0 + timedelta(minutes=6))) is None


def test_opposite_direction_not_suppressed():
    gate = AlertGate(PriceRules())
    pass_through(gate, alert(rapid(-2.0)))
    assert pass_through(gate, alert(rapid(+2.0), ts=T0 + timedelta(minutes=2))) is not None


def test_levels_fire_once_per_day_and_only_upwards():
    gate = AlertGate(PriceRules())
    assert pass_through(gate, alert(level(-3.2, 3))) is not None
    assert pass_through(gate, alert(level(-3.9, 3), ts=T0 + timedelta(hours=1))) is None
    assert pass_through(gate, alert(level(-5.4, 5), ts=T0 + timedelta(hours=2))) is not None
    # 第二天重新计算
    assert pass_through(gate, alert(level(-3.1, 3)), day=DAY + timedelta(days=3)) is not None


def test_mixed_triggers_keep_only_new_parts():
    gate = AlertGate(PriceRules())
    pass_through(gate, alert(level(-3.2, 3)))
    out = pass_through(gate, alert(rapid(-2.0), level(-3.5, 3), ts=T0 + timedelta(minutes=1)))
    assert out is not None and [t.rule for t in out.triggers] == ["rapid"]


def test_restore_from_storage_prevents_duplicates_after_restart():
    st = Storage(":memory:")
    a = alert(rapid(-2.0), level(-3.2, 3))
    st.record_alert(a, DAY.isoformat())
    gate = AlertGate(PriceRules())
    gate.restore(st.alerts_for_day(DAY.isoformat()), DAY)
    assert pass_through(gate, alert(rapid(-2.1), ts=T0 + timedelta(minutes=4))) is None
    assert pass_through(gate, alert(level(-3.4, 3), ts=T0 + timedelta(minutes=4))) is None
    restored = st.alerts_for_day(DAY.isoformat())[0]
    assert restored.triggers[0].rule == "rapid" and restored.ts == T0 and restored.id == a.id


def day(move, lv, anchor=None, anchor_pct=0.0):
    return Trigger("day", 1 if move > 0 else -1, move, level_pct=lv, anchor_ts=anchor, anchor_pct=anchor_pct,
                   minutes=None if anchor is None else 30)


def test_day_steps_fire_once_per_anchor_and_reset_on_a_new_high():
    gate = AlertGate(PriceRules())
    h1, h2 = T0 - timedelta(hours=2), T0 + timedelta(hours=1)
    assert pass_through(gate, alert(day(-5.2, 5, h1, 1.0))) is not None  # 从 h1 的高点回落 5 个点
    assert pass_through(gate, alert(day(-6.0, 5, h1, 1.0), ts=T0 + timedelta(minutes=30))) is None  # 同一起点、同一档
    assert pass_through(gate, alert(day(-8.1, 8, h1, 1.0), ts=T0 + timedelta(minutes=40))) is not None  # 下一档
    # 涨回去、创出新高 h2 之后又跌 5 个点：新的一波，档位重新计
    assert pass_through(gate, alert(day(-5.1, 5, h2, 2.0), ts=T0 + timedelta(hours=2))) is not None
    # 上涨方向自己算；旧记录的“关口”当作起点为昨收的全天累计
    assert pass_through(gate, alert(day(5.5, 5), ts=T0 + timedelta(hours=3))) is not None
    assert pass_through(gate, alert(level(5.9, 5), ts=T0 + timedelta(hours=4))) is None


def test_day_trigger_text_and_storage_roundtrip(tmp_path):
    t = day(-3.1, 3, T0 - timedelta(minutes=40), 2.0)
    assert t.describe() == "全天：从日内高点回落超 3 个点（较昨收 +2.0% → -1.1%，用时 30 分钟）"
    assert t.short() == "从日内高点回落 3.1 个点" and day(-5.4, 5).short("较今日收盘") == "较今日收盘跌超 5%"
    r = Trigger("rapid", -1, -3.0, window_minutes=60, z=-4.5, anchor_ts=T0, anchor_pct=2.0, minutes=40)
    assert r.describe() == "40分钟内从高点回落 3.0 个点（较昨收 +2.0% → -1.0%，z=-4.5）" and r.short() == "40分钟急跌 3.0 个点"
    assert Trigger("rapid", -1, -2.0, window_minutes=5, z=-6.0).short() == "5分钟急跌 -2.0%"  # 旧记录照旧显示
    s = Storage(str(tmp_path / "db.sqlite3"))
    s.record_alert(alert(r, t), DAY.isoformat())
    back = s.alerts_for_day(DAY.isoformat())[0]
    assert back.triggers[0] == r and back.triggers[1] == t  # 起点时间存进库、读回来一样（冷却能从库里恢复）


def rapid_at(move, anchor, w=60):
    return Trigger("rapid", 1 if move > 0 else -1, move, window_minutes=w, z=6.0 if move > 0 else -6.0,
                   anchor_ts=anchor, anchor_pct=1.0, minutes=10)


def test_day_step_absorbed_only_by_the_same_wave():
    """全天的档位：同一波（急涨急跌的起点不早于它）已经推过幅度相当的急跌时不再单独推——急跌在冷却中、仍出现在候选里也一样；
    创出新高之后的新一波不被之前的急跌吞掉；判断只依赖入库的急涨急跌，重启前后一样。"""
    gate = AlertGate(PriceRules())
    h = T0 - timedelta(minutes=30)
    assert pass_through(gate, alert(rapid_at(-4.4, h))) is not None
    assert pass_through(gate, alert(rapid_at(-5.0, h), day(-5.1, 5, h, 1.0), ts=T0 + timedelta(minutes=9))) is None
    h2 = T0 + timedelta(minutes=20)  # 新高
    out = pass_through(gate, alert(day(-5.2, 5, h2, 2.0), ts=T0 + timedelta(minutes=50)))
    assert out is not None and [t.rule for t in out.triggers] == ["day"]
    restarted = AlertGate(PriceRules())
    restarted.restore([alert(rapid_at(-4.4, h))], DAY)
    assert pass_through(restarted, alert(day(-5.1, 5, h, 1.0), ts=T0 + timedelta(hours=3))) is None
    # 同一次检测里一起触发的：合在同一条推送里，全天这一档照样带上
    fresh = AlertGate(PriceRules())
    out = pass_through(fresh, alert(rapid_at(-5.0, h), day(-5.1, 5, h, 1.0)))
    assert [t.rule for t in out.triggers] == ["rapid", "day"]


def test_cooldown_does_not_carry_over_into_the_post_market():
    """盘中最后几分钟推过的急跌，不压盘后（参照价换成今日收盘，另一个段 key）开头的财报急跌。"""
    gate = AlertGate(PriceRules())
    assert pass_through(gate, alert(rapid_at(-3.2, T0)), day="2026-10-09") is not None
    assert pass_through(gate, alert(rapid_at(-3.5, T0), ts=T0 + timedelta(minutes=5)), day="2026-10-09") is None
    assert pass_through(gate, alert(rapid_at(-4.5, T0), ts=T0 + timedelta(minutes=11)), day="2026-10-09:post") is not None
