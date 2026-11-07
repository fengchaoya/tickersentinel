"""冷却与去重：决定候选异动里哪些触发条件需要真正推送。

- 急涨急跌 / 个股特异：同一段（段 key：盘前和盘中共用一个，盘后、各海外交易日各自一个）、同标的、同规则、同方向，
  在有效冷却期内不重复推送；有效冷却期 = max(cooldown_minutes, 上次窗口, 本次窗口)，避免 60 分钟窗口在冷却结束后
  “重新看到”同一波下跌。冷却期内若幅度放大到上次的 escalation_factor 倍以上，仍推送并标记为“扩大”。
  按段区分：盘中最后几分钟推过的急跌，不会压掉盘后（参照价换成今日收盘）开头的财报急跌。
- 全天累计：同一个起点（昨收，或某个日内高 / 低点）每一档只推一次；一次跨过多档只推最高的那档。
  创出新的高点（下跌方向）/ 新的低点（上涨方向）就是新的起点，档位重新计——那是新的一波。
  同一段里已经推过同方向的急涨急跌、它的起点不早于这一档的起点（同一波）、幅度又已有这一档的 ABSORB 倍时，
  这一档不再单独推（和急涨急跌在同一次检测里一起推出去的除外：合在同一条推送里）。这个判断只依赖已入库的
  急涨急跌，所以重启前后结果一样。
  旧记录里的“关口”（level）当作起点为昨收的全天累计。
- 状态可以从当段已入库的异动恢复（程序重启后不会重复推送）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from tickersentinel.config import PriceRules
from tickersentinel.models import Alert, Trigger

DAY_RULES = ("day", "level")
ABSORB = 0.75  # 同一波推过的急涨急跌幅度 ≥ 这一档 × 0.75：全天的这一档不再单独推



def anchor_id(tr: Trigger) -> str:
    """全天累计的起点标识：昨收为 "ref"，日内高 / 低点为它的时间。"""
    return "ref" if tr.anchor_ts is None else tr.anchor_ts.isoformat()


class AlertGate:
    def __init__(self, cfg: PriceRules):
        self.cfg = cfg
        # (段 key, symbol, rule, direction) → (时间, |幅度|, 窗口)
        self._last: dict[tuple, tuple[datetime, float, int]] = {}
        # (段 key, symbol, direction) → (起点, 这个起点已推送的最高一档)
        self._day: dict[tuple, tuple[str, float]] = {}
        # (段 key, symbol, direction) → 推过的急涨急跌 [(起点时间, |幅度|)]，判断全天的档位是不是同一波
        self._rapid: dict[tuple, list[tuple[datetime | None, float]]] = {}

    def _fired(self, key: tuple, tr: Trigger) -> float:
        anchor, fired = self._day.get(key, ("", 0.0))
        return fired if anchor == anchor_id(tr) else 0.0

    def _absorbed(self, key: tuple, tr: Trigger) -> bool:
        """这一档是不是已经由同一波的急涨急跌报过了。起点是昨收的（anchor_ts 为 None）：同一段里任何一条都算同一波；
        起点是日内高 / 低点的：急涨急跌的起点不早于它才算（创出新高之后的回落是新的一波，不能被之前的急跌吞掉）。"""
        need = ABSORB * (tr.level_pct or 0.0)
        for a_ts, mag in self._rapid.get(key, []):
            same_wave = tr.anchor_ts is None or (a_ts is not None and a_ts >= tr.anchor_ts)
            if same_wave and mag >= need:
                return True
        return False

    def filter(self, alert: Alert, day: date | str) -> Alert | None:
        kept: list[Trigger] = []
        escalation = False
        for tr in alert.triggers:  # 按 RULE_PRIORITY 排序：急涨急跌先处理，全天累计能看到它有没有留下
            if tr.rule in DAY_RULES:
                key = (day, alert.symbol, tr.direction)
                if tr.level_pct is None or tr.level_pct <= self._fired(key, tr):
                    continue
                together = any(t.rule == "rapid" and t.direction == tr.direction for t in kept)
                if not together and self._absorbed(key, tr):
                    continue
                kept.append(tr)
                continue
            last = self._last.get((day, alert.symbol, tr.rule, tr.direction))
            if last is not None:
                last_ts, last_mag, last_w = last
                cool = timedelta(minutes=max(self.cfg.cooldown_minutes, last_w, tr.window_minutes or 0))
                if alert.ts - last_ts < cool:
                    if abs(tr.move_pct) >= self.cfg.escalation_factor * last_mag:
                        escalation = True
                        kept.append(tr)
                    continue
            kept.append(tr)
        if not kept:
            return None
        # 标题可能变了（如急跌被冷却、只剩全天累计）：拆分上下文随之换成新标题的口径
        return alert.with_triggers(kept, is_escalation=escalation)

    def commit(self, alert: Alert, day: date | str) -> None:
        for tr in alert.triggers:
            if tr.rule in DAY_RULES:
                key = (day, alert.symbol, tr.direction)
                self._day[key] = (anchor_id(tr), max(self._fired(key, tr), tr.level_pct or 0.0))
                continue
            self._last[(day, alert.symbol, tr.rule, tr.direction)] = (alert.ts, abs(tr.move_pct), tr.window_minutes or 0)
            if tr.rule == "rapid":
                self._rapid.setdefault((day, alert.symbol, tr.direction), []).append((tr.anchor_ts, abs(tr.move_pct)))

    def restore(self, alerts: list[Alert], day: date | str) -> None:
        for a in sorted(alerts, key=lambda x: x.ts):
            self.commit(a, day)
