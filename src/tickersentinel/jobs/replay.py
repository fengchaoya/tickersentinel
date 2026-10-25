"""回放：用最近 N 个交易日的真实 1 分钟数据逐分钟重跑检测与冷却逻辑，
统计“按当前阈值会推送多少条、在什么时候、哪个时段”，用来校准 config/assets.yaml 里各资产的警报标准。

和实盘一样按段回放（美股盘前 / 盘中 / 盘后、海外交易所、期货……，见 detect/clock.py），
每段都只用它之前的数据估计基线（没有前视偏差），盘前和盘中共用一个冷却 key（同一个昨收）。
注意：yfinance 的 1 分钟数据只覆盖最近约 30 天。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from tickersentinel.agents.render import pct
from tickersentinel.config import Settings
from tickersentinel.detect.baseline import Baseline, session_frame
from tickersentinel.detect.basket import add_baskets
from tickersentinel.detect.clock import Segment, build_clock, clock_key
from tickersentinel.detect.cooldown import AlertGate
from tickersentinel.detect.price import PriceAnomalyDetector
from tickersentinel.jobs.merge import count_messages
from tickersentinel.jobs.monitor import MonitorGroup, build_groups, prepare_context
from tickersentinel.market_calendar import MarketCalendar, Session
from tickersentinel.models import Alert
from tickersentinel.sources.base import PriceSource
from tickersentinel.timeutil import fmt_local, tz_abbr, utcnow


@dataclass
class ReplayResult:
    sessions: list[Session]  # 回放覆盖的美股交易日（报告按天汇总）
    alerts: list[tuple[Segment, Alert]] = field(default_factory=list)
    baselines: dict[str, Baseline] = field(default_factory=dict)  # 每个资产最后一段的基线
    segments: list[Segment] = field(default_factory=list)

    def report(self, settings: Settings) -> str:
        tz = settings.tz
        if not self.segments:
            return "没有可回放的交易时段。"
        days = sorted({g.day for g in self.segments})
        kinds = Counter(g.kind for g in self.segments)
        names = {"pre": "盘前", "regular": "盘中", "post": "盘后", "cme": "期货", "utc": "加密货币", "exchange": "海外交易所"}
        lines = [
            f"回放 {days[0]:%m-%d} ~ {days[-1]:%m-%d}（{len(days)} 个交易日，"
            + "、".join(f"{names.get(k, k)} {n} 段" for k, n in kinds.items())
            + f"），按当前阈值共会推送 {len(self.alerts)} 条异动（不含归因消息）。",
        ]
        msgs, follows = count_messages([a for _, a in self.alerts], settings)
        if msgs != len(self.alerts):
            lines[0] += (f"同一主题同方向一起动的合成一条后，实际推送 {msgs} 条"
                         + (f"（其中 {follows} 条挂在前一条下面作回复）" if follows else "") + "。")
        per_day = Counter((g.day, settings.universe.display(a.symbol)) for g, a in self.alerts)
        for d in days:
            items = [f"{sym} {n}条" for (dd, sym), n in sorted(per_day.items()) if dd == d]
            lines.append(f"  {d:%m-%d}：{('、'.join(items)) if items else '无'}")
        by_seg = Counter(a.segment for _, a in self.alerts)
        if by_seg:
            lines.append("  按时段：" + "、".join(f"{names.get(k, k)} {n} 条" for k, n in by_seg.items()))
        if self.alerts:
            lines.append("\n明细：")
            for _, a in self.alerts:
                split = ""
                if a.abnormal_move_pct is not None and a.benchmark_move_pct is not None and a.beta is not None:
                    split = f"  拆分：板块 {pct(a.beta * a.benchmark_move_pct)} / 个股 {pct(a.abnormal_move_pct)}"
                esc = "（扩大）" if a.is_escalation else ""
                tag = f"[{a.segment_label}]" if a.segment_label else ""
                lines.append(
                    f"  {fmt_local(a.ts, tz, '%m-%d %H:%M')} {tz_abbr(a.ts, tz)}  {settings.universe.display(a.symbol):<5}{tag} "
                    + "；".join(t.describe(a.ref_label) for t in a.triggers) + esc + split
                )
        if self.baselines:
            lines.append("\n基线（各资产最后一段）：")
            lines += [f"  {b.describe()}" for b in self.baselines.values()]
        return "\n".join(lines)


def replay_groups(settings: Settings, calendar: MarketCalendar, symbols: list[str] | None) -> list[MonitorGroup]:
    groups = build_groups(settings, calendar)
    if not symbols:
        return groups
    u = settings.universe
    out: list[MonitorGroup] = []
    for g in groups:
        keep = [s for s in g.symbols if s in symbols]
        if keep:
            g.symbols = keep
            g.benchmarks = {s: b for s, b in g.benchmarks.items() if s in keep}
            g.peers = {s: ps for s, ps in g.peers.items() if s in keep}
            used = {*g.benchmarks.values(), *keep}  # 留下来的标的用到的篮子，以及要回放的篮子本身（夜间篮子）
            g.baskets = {b: ms for b, ms in g.baskets.items() if b in used}
            out.append(g)
    known = {s for g in groups for s in g.symbols}
    extra = [s for s in symbols if s not in known]  # 命令行指定、但不在资产清单里的：按美股常规时段回放，用兜底基准
    if extra:
        out.append(MonitorGroup("us", build_clock("us", calendar, extended=False), extra, benchmark=u.benchmark,
                                hours={s: "regular" for s in extra}))
    return out


async def run_replay(settings: Settings, calendar: MarketCalendar, prices: PriceSource, days: int,
                     symbols: list[str] | None = None, now: datetime | None = None) -> ReplayResult:
    cfg = settings.monitor.price
    now = now or utcnow()
    rules = {s: settings.rules_for(s) for s in settings.universe.symbols} | {
        i.symbol: settings.rules_for(i.symbol) for i in settings.universe.monitored}
    detector = PriceAnomalyDetector(cfg, rules)
    result = ReplayResult(sessions=calendar.last_completed_sessions(now, days))
    for group in replay_groups(settings, calendar, symbols):
        targets = [g for g in group.clock.completed(now, days) if group.active(g)]
        if not targets:
            continue
        first_prior = group.clock.previous(targets[0], cfg.sigma_lookback_sessions)
        start = (first_prior[0] if first_prior else targets[0]).open
        intraday = await prices.intraday_bars(group.fetch, start=start, end=targets[-1].close, prepost=group.prepost)
        daily: dict = {}
        if group.key not in ("cme", "utc"):
            daily = await prices.daily_bars(group.fetch, lookback_days=cfg.beta_lookback_days + 2 * days + 15)
        if group.baskets:  # 篮子的分钟线整段只算一次（口径与每段的 anchor 无关）
            intraday, _ = add_baskets(group.baskets, intraday=intraday, daily=daily, tz=group.basket_tz)
        gate = AlertGate(cfg)
        for seg in targets:
            ctx = await prepare_context(settings, group, seg, prices, intraday=intraday, daily=daily)
            frames = {s: session_frame(df, seg) for s, df in intraday.items()}
            for k in range(1, seg.minutes + 1):
                for cand in detector.evaluate(ctx, frames, as_of=seg.open + timedelta(minutes=k)):
                    alert = gate.filter(cand, seg.key)
                    if alert is not None:
                        gate.commit(alert, seg.key)
                        result.alerts.append((seg, alert))
            result.baselines.update(ctx.baselines)
            result.segments.append(seg)
    result.alerts.sort(key=lambda x: x[1].ts)
    result.segments.sort(key=lambda g: g.open)
    return result


__all__ = ["ReplayResult", "run_replay", "clock_key", "date"]
