"""异动监控：每 interval_seconds 检测一次（全天候），只有出现异动才推送。

资产按“钟”分组（detect/clock.py）：美股（盘前 / 盘中 / 盘后）、CME 期货、加密货币、各海外交易所。
每次检测（tick）对每一组：找出现在所在的段 → 段变了就重新估计基线（用前几个同类段，见 prepare_context）
→ 拉取这一段的分钟 K 线 → 对齐网格 → 规则检测（每个资产用自己的警报标准，config/assets.yaml）→ 冷却过滤
→ 逐条入库 → 各组都检测完后统一推送：同主题同方向的合成一条、merge_minutes 内跟着动的挂在第一条下面（jobs/merge.py），
附宏观快照 → 后台异步做归因（合成的一条归因一次），完成后以“回复”的形式追加推送。
不在交易时段的组直接跳过；一组出错不影响其他组（出错的组在本次检测结束后报错，供 App 计数告警）。

推送失败（断网、飞书接口出错）时，异动照常入库、冷却照常生效（同一波异动不会被当成新的反复检测），
但 message_id 留空表示“还没送达”；之后每次检测都重发，RESEND_WINDOW 内送达就补上（标题注明“补发”），超过就放弃。
基线（β、σ、成交量画像）在每段开始时估计一次，段内不再变化（避免异动本身污染基线）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import pandas as pd

from tickersentinel.agents.attribution import Attributor
from tickersentinel.agents.render import render_alert, render_alert_group, sector_judgment
from tickersentinel.config import Settings, is_pseudo, is_us_symbol
from tickersentinel.detect.basket import add_baskets
from tickersentinel.detect.baseline import (
    SessionContext,
    build_session_context,
    day_extremes,
    prev_close_from_daily,
    session_frame,
)
from tickersentinel.detect.clock import Clock, Segment, build_clock, clock_key, exchange_of
from tickersentinel.detect.cooldown import AlertGate
from tickersentinel.detect.price import PriceAnomalyDetector
from tickersentinel.market_calendar import MarketCalendar
from tickersentinel.jobs.merge import Batch, Merger
from tickersentinel.models import Alert, Message
from tickersentinel.notify.base import Notifier
from tickersentinel.sources.base import PriceSource
from tickersentinel.storage import Storage
from tickersentinel.timeutil import ET, fmt_local, utcnow

if TYPE_CHECKING:
    from tickersentinel.sources.macro import MacroSnapshot, MacroSource

log = logging.getLogger(__name__)

RESEND_WINDOW = timedelta(minutes=30)  # 推送失败的异动，30 分钟内持续重发；再晚就没有时效了


@dataclass
class MonitorGroup:
    """同一个钟的一组资产。基准 / 同业只用来做板块拆分和同业对比，不单独检测。

    每只股票用自己主题的基准（benchmarks，可能是同业篮子 basket:<主题>）和同业（peers）；
    benchmark 是全组共用的基准（benchmarks 里没有的股票用它，例如命令行临时指定的代码）。"""

    key: str
    clock: Clock
    symbols: list[str]
    benchmark: str | None = None
    others: list[str] = field(default_factory=list)
    prepost: bool = False  # 拉分钟线时要不要带盘前盘后（美股盘前 / 盘后段需要）
    hours: dict[str, str] = field(default_factory=dict)  # 美股：symbol → regular / extended
    benchmarks: dict[str, str | None] = field(default_factory=dict)  # 股票 → 自己主题的基准
    peers: dict[str, list[str]] = field(default_factory=dict)  # 股票 → 同期对比的代码（同一个钟的）
    baskets: dict[str, list[str]] = field(default_factory=dict)  # 篮子伪代码 → 成分
    basket_tz: Any = ET  # 篮子按哪个时区的日期划分交易日（海外交易所用当地时区）

    @property
    def fetch(self) -> list[str]:
        """要从数据源取的代码：篮子换成它的成分（篮子本身由成分算出来，见 detect/basket.py）。"""
        out: list[str] = []
        for s in [*self.symbols, self.benchmark, *self.benchmarks.values(), *self.others,
                  *(p for ps in self.peers.values() for p in ps)]:
            if not s:
                continue
            for x in self.baskets.get(s, [s]):
                if x not in out and x not in self.baskets:
                    out.append(x)
        for members in self.baskets.values():
            out += [m for m in members if m not in out]
        return out

    @property
    def derived(self) -> list[str]:
        """由成分算出来的篮子伪代码。"""
        return list(self.baskets)

    def active(self, seg: Segment) -> list[str]:
        """这一段要检测的资产：美股只设了 regular 的，盘前盘后不检测。"""
        if seg.kind in ("pre", "post"):
            return [s for s in self.symbols if self.hours.get(s) == "extended"]
        return list(self.symbols)


def build_groups(settings: Settings, calendar: MarketCalendar) -> list[MonitorGroup]:
    """按钟分组；每只股票的基准、同业来自它的主题（config/themes/）。"""
    u = settings.universe
    by_key: dict[str, list[str]] = {}
    for inst in u.monitored:
        by_key.setdefault(clock_key(inst.asset_class, inst.symbol), []).append(inst.symbol)
    groups = []
    for key, syms in by_key.items():
        if key == "us":
            hours = {s: settings.hours_for(s) for s in syms}
            extended = any(h == "extended" for h in hours.values())
            benchmarks = {s: u.benchmark_for(s) for s in syms}
            peers = {s: [p for p in u.peers_for(s) if is_us_symbol(p)] for s in syms}
            baskets = {b: u.basket_members(b) for b in benchmarks.values() if b and u.basket_members(b)}
            groups.append(MonitorGroup(key, build_clock(key, calendar, extended=extended), syms, prepost=extended,
                                       hours=hours, benchmarks=benchmarks, peers=peers, baskets=baskets))
        else:
            # 海外交易所：同一交易所的同业一起拉，异动时看同期涨跌（例如首尔的 SK海力士 对比 三星电子）；
            # 夜间信号篮子（night:<主题>:<交易所>）由成分算出来，成分相对篮子算“单独大动”
            clock = build_clock(key, calendar)
            exch = key not in ("cme", "utc")
            peers = {s: ([p for p in u.peers_for(s) if exchange_of(p) == key] if exch else []) for s in syms}
            benchmarks = {s: (u.benchmark_for(s) if exch else None) for s in syms}
            codes = {*(b for b in benchmarks.values() if b), *(s for s in syms if is_pseudo(s))}
            baskets = {c: u.basket_members(c) for c in codes if u.basket_members(c)}
            tz = getattr(getattr(clock, "calendar", None), "tz", ET)
            groups.append(MonitorGroup(key, clock, syms, peers=peers, benchmarks=benchmarks, baskets=baskets,
                                       basket_tz=tz))
    return groups


def asof_close(df: pd.DataFrame | None, t: datetime) -> float | None:
    if df is None or df.empty:
        return None
    c = df["close"].dropna()
    c = c[c.index <= pd.Timestamp(t)]
    return float(c.iloc[-1]) if len(c) else None


async def prepare_context(settings: Settings, group: MonitorGroup, seg: Segment, prices: PriceSource,
                          intraday: dict[str, pd.DataFrame] | None = None,
                          daily: dict[str, pd.DataFrame] | None = None) -> SessionContext:
    """估计一段的基线：前 sigma_lookback_sessions 个同类段的分钟数据算 σ 和成交量画像，日线算 β；再定参照价。
    intraday / daily 可以由调用方一次取好传进来（回放）。"""
    cfg = settings.monitor.price
    prior = group.clock.previous(seg, cfg.sigma_lookback_sessions)
    fetch = group.fetch
    if intraday is None:
        intraday = await prices.intraday_bars(fetch, start=prior[0].open, end=seg.open, prepost=group.prepost) if prior else {}
    stocks = [s for s in fetch if group.key not in ("cme", "utc")]
    if daily is None:
        daily = {}
        if stocks:
            try:
                daily = await prices.daily_bars(stocks, lookback_days=cfg.beta_lookback_days + 10)
            except Exception as exc:  # 没有日线：β 用默认值，参照价退回分钟线
                log.warning("取日线失败（%s 组）：%s", group.key, exc)
    if group.baskets:  # 同业 / 夜间篮子：由成分的分钟线 / 日线算出来，日线按这一段的交易日对齐到“昨收 = 100”
        intraday, daily = add_baskets(group.baskets, intraday=intraday, daily=daily, anchor=seg.day, tz=group.basket_tz)
    targets = [*fetch, *group.derived]
    refs: dict[str, float] | None = None
    if seg.kind == "post":
        # 盘后：参照当天收盘（日线有当天就用日线的官方收盘，否则用分钟线收盘那一刻）
        refs = {}
        for s in targets:
            d = daily.get(s)
            day_close = None
            if d is not None and not d.empty and "close" in d:
                c = d["close"].dropna()
                c = c[c.index.date == seg.day]
                day_close = float(c.iloc[-1]) if len(c) else None
            if day_close:
                refs[s] = day_close
        missing = [s for s in fetch if s not in refs]
        if missing and seg.ref_at is not None:  # 日线还没有当天：用收盘前最后几分钟的分钟线
            try:
                last = await prices.intraday_bars(missing, start=seg.ref_at - timedelta(minutes=30), end=seg.ref_at)
            except Exception as exc:
                log.warning("取当天收盘价失败：%s", exc)
                last = {}
            refs.update({s: v for s in missing if (v := asof_close(last.get(s), seg.ref_at))})
            for code, members in group.baskets.items():  # 篮子：成分收盘较昨收的平均（与篮子分钟线同一口径）
                if code not in refs:
                    rel = [refs[m] / pc for m in members if m in refs
                           and (pc := prev_close_from_daily(daily.get(m), seg.day))]
                    if rel:
                        refs[code] = 100.0 * sum(rel) / len(rel)
    elif seg.kind in ("cme", "utc") and seg.ref_at is not None:
        # 期货 / 加密货币：上一段结束那一刻的分钟价（日线在期货换月时是另一个合约，不能用）
        refs = {s: v for s in fetch if (v := asof_close(intraday.get(s), seg.ref_at))}
    watch = group.active(seg)
    ctx = build_session_context(
        session=seg, prior_sessions=prior, intraday=intraday, daily=daily, watchlist=watch,
        benchmark=group.benchmark, others=[s for s in targets if s not in group.symbols],
        cfg=cfg, prev_close=refs, benchmarks={s: group.benchmarks.get(s, group.benchmark) for s in watch},
        peers={s: group.peers[s] for s in watch if s in group.peers},
    )
    if group.baskets:  # 检测时把当段的分钟线算成篮子要用成分的日线（取每个交易日的昨收）
        ctx.basket_daily = {m: daily[m] for ms in group.baskets.values() for m in ms if m in daily}
    if seg.kind == "regular" and hasattr(group.clock, "segments_on"):  # 美股盘中：带上当天盘前的最高 / 最低点
        pre = next((g for g in group.clock.segments_on(seg.day) if g.kind == "pre" and g.key == seg.key), None)
        if pre is not None:
            ctx.carry = day_extremes(intraday, targets, pre)
    return ctx


class IntradayMonitor:
    def __init__(self, settings: Settings, calendar: MarketCalendar, prices: PriceSource, storage: Storage,
                 notifier: Notifier, attributor: Attributor, macro: "MacroSource | None" = None):
        self.settings = settings
        self.calendar = calendar
        self.prices = prices
        self.storage = storage
        self.notifier = notifier
        self.attributor = attributor
        self.macro = macro
        self.cfg = settings.monitor.price
        self.groups = build_groups(settings, calendar)
        rules = {i.symbol: settings.rules_for(i.symbol) for i in settings.universe.monitored}
        self.detector = PriceAnomalyDetector(self.cfg, rules)
        self.gate = AlertGate(self.cfg)
        self.merger = Merger(settings.monitor.merge_minutes)  # 同主题同方向一起异动时合并推送
        self.ctx: dict[str, SessionContext] = {}  # 组 → 当前段的上下文
        self._gen = 0  # 分组的代数：reload 一次加一，正在估计的旧上下文估完不写回
        self._restored: set[str] = set()  # 已从数据库恢复过冷却状态的段 key
        self._tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()

    def reload(self) -> None:
        """监控范围变了（tsen scope / 飞书 /scope / 改了配置文件）：重建分组和检测参数，下一次检测重新估计基线。
        冷却状态保留（同一波异动不会因为重载再推一次）。"""
        self.groups = build_groups(self.settings, self.calendar)
        rules = {i.symbol: self.settings.rules_for(i.symbol) for i in self.settings.universe.monitored}
        self.detector = PriceAnomalyDetector(self.cfg, rules)
        self.ctx = {}
        self._gen += 1

    def describe(self) -> list[str]:
        """启动消息里的一行行说明：每组盯什么、什么时段。"""
        names = {"us": "美股", "cme": "期货（CME，美东 18:00–次日 17:00）", "utc": "加密货币（全天）",
                 "XKRX": "首尔交易所（美东约 20:00–02:30）", "XTKS": "东京交易所（美东约 20:00–02:30）",
                 "XHKG": "香港交易所", "XTAI": "台湾交易所", "XSHG": "上海 / 深圳交易所（美东约 21:30–03:00）"}
        out = []
        for g in self.groups:
            if g.key == "us":
                ext = [s for s in g.symbols if g.hours.get(s) == "extended"]
                span = "盘前 + 盘中 + 盘后（美东 04:00–20:00）" if ext else "常规时段"
                out.append(f"{names['us']} {'、'.join(g.symbols)}：{span}")
            else:
                out.append(f"{names.get(g.key, g.key + ' 交易时段')} {'、'.join(g.symbols)}")
        return out

    async def prepare(self, group: MonitorGroup, seg: Segment) -> SessionContext:
        """段开始时估计基线，并从数据库恢复这一段的冷却状态（重启不重复推送）。"""
        gen = self._gen
        ctx = await prepare_context(self.settings, group, seg, self.prices)
        if seg.key not in self._restored:
            self.gate.restore(self.storage.alerts_for_day(seg.key), seg.key)
            self._restored.add(seg.key)
        if gen == self._gen:  # 估计期间监控范围热更新了（飞书 /scope 确认）：按旧分组估的不写回，下一次检测重估
            self.ctx[group.key] = ctx
        for b in ctx.baselines.values():
            log.info("基线（%s %s）%s", seg.key, seg.kind, b.describe())
        return ctx

    async def tick(self, now: datetime | None = None) -> list[Alert]:
        now = now or utcnow()
        detected: list[Alert] = []
        errors: list[BaseException] = []
        async with self._lock:  # 上一次检测没跑完时不并发
            for group in self.groups:
                seg = group.clock.segment_at(now)
                if seg is None or not group.active(seg):
                    continue
                try:
                    detected += await self._tick_group(group, seg, now)
                except Exception as exc:  # 一组出错不影响其他组
                    log.warning("%s 组检测失败：%s: %s", group.key, type(exc).__name__, exc)
                    errors.append(exc)
            if detected:  # 各组都检测完再推：首尔篮子和铠侠同时动时也能合成一条
                await self._push(detected)
        if errors:
            raise errors[0]
        return detected

    async def _tick_group(self, group: MonitorGroup, seg: Segment, now: datetime) -> list[Alert]:
        ctx = self.ctx.get(group.key)
        if ctx is None or ctx.session != seg:
            ctx = await self.prepare(group, seg)
        await self._resend_undelivered(seg.key, now)
        bars = await self.prices.intraday_bars(group.fetch, start=seg.open, end=None, prepost=group.prepost)
        if group.baskets:
            bars, _ = add_baskets(group.baskets, intraday=bars, daily=ctx.basket_daily, tz=group.basket_tz)
        frames = {s: session_frame(df, seg) for s, df in bars.items()}
        candidates = self.detector.evaluate(ctx, frames, as_of=now)
        return self._admit(candidates, seg.key)

    def _admit(self, candidates: list[Alert], day_key: str) -> list[Alert]:
        """过冷却、入库（每条异动都单独入库，合并只影响推送）。返回这次新出现的异动。"""
        out = []
        for cand in candidates:
            alert = self.gate.filter(cand, day_key)
            if alert is None:
                continue
            self.gate.commit(alert, day_key)
            self.storage.record_alert(alert, day_key)
            out.append(alert)
        return out

    async def _push(self, alerts: list[Alert]) -> None:
        """宏观快照只取一次；同主题同方向的合成一条或挂在前一条下面（jobs/merge.py）。"""
        macro = await self._macro_snapshot()
        for a in alerts:
            a.macro = macro
            if macro is not None and a.id is not None:
                self.storage.set_alert_macro(a.id, macro.to_json())
        for batch in self.merger.plan(alerts, self.settings):
            try:
                await self._deliver_batch(batch)
            except Exception:  # 排版等意外：别的批照样推，没送达的下次检测补发
                log.exception("推送异动失败（%s）", "、".join(a.symbol for a in batch.alerts))

    async def _deliver_batch(self, batch: Batch) -> None:
        if batch.thread is not None:
            await self._deliver_follow(batch)
        elif len(batch.alerts) == 1:
            self.merger.opened(batch, await self._deliver(batch.alerts[0]))
        else:
            await self._deliver_group(batch)

    async def _send(self, msg: Message, alerts: list[Alert]) -> str | None:
        """推一条消息，记下每条异动的 message_id（非空即“已送达”）。失败返回 None，等下一次检测逐条补发。"""
        try:
            mid = await self.notifier.send(msg)
        except Exception as exc:
            log.warning("推送异动失败（%s），下次检测时重发：%s", "、".join(a.symbol for a in alerts), exc)
            return None
        for a in alerts:
            if a.id is not None:
                self.storage.set_alert_message_id(a.id, mid or "")
        return mid or ""

    async def _deliver_group(self, batch: Batch) -> None:
        alerts = batch.alerts
        msg = render_alert_group(alerts, self.settings, theme=batch.theme or "", macro=alerts[0].macro,
                                 attribution_pending=self.settings.attribution.enabled)
        mid = await self._send(msg, alerts)
        if mid is None:
            return
        self.merger.opened(batch, mid)
        if self.settings.attribution.enabled:
            self._spawn(self._attribute_group(alerts, batch.theme or "", mid))

    async def _deliver_follow(self, batch: Batch) -> None:
        """挂在同一波的第一条下面：个股因素为主的照样单独归因，板块联动的不再重复归因。"""
        t = batch.thread
        own = [a for a in batch.alerts if sector_judgment(a) == "以个股因素为主"]
        msg = render_alert_group(batch.alerts, self.settings, theme=batch.theme or "", follow_of=t.started,
                                 attribution_pending=bool(own) and self.settings.attribution.enabled)
        msg.reply_to = t.message_id
        mid = await self._send(msg, batch.alerts)
        if mid is None:
            return
        self.merger.followed(batch)
        when = fmt_local(t.started, self.settings.tz, "%H:%M")
        if not self.settings.attribution.enabled:
            return
        lead = t.alert_ids[0] if t.alert_ids else None
        for a in batch.alerts:
            if a in own:
                self._spawn(self._attribute(a, mid))
            elif a.id is not None:
                note = f"同一波行情（{when} 那一条的跟帖），原因见那一条的归因"
                self.storage.save_attribution(a.id, note, {"summary": note, "same_wave_as": lead} if lead else None)

    async def _macro_snapshot(self) -> "MacroSnapshot | None":
        """异动推送附带的宏观快照。最多等 macro.timeout_seconds：取不到就不附，不能耽误异动推送。"""
        cfg = self.settings.macro
        if self.macro is None or not (cfg.enabled and cfg.attach_to_alerts):
            return None
        try:
            return await asyncio.wait_for(self.macro.snapshot(), cfg.timeout_seconds)
        except Exception as exc:  # 含超时
            log.warning("取宏观快照失败，本次异动不附宏观数据：%s: %s", type(exc).__name__, exc)
            return None

    async def _deliver(self, alert: Alert, *, resend: bool = False) -> str | None:
        """推送一条异动快讯；成功后启动归因，返回 message_id。失败返回 None，等下一次检测重发。"""
        msg = render_alert(alert, self.settings, attribution_pending=self.settings.attribution.enabled,
                           macro=alert.macro)
        if resend:
            msg.title += f"（补发：{fmt_local(alert.ts, self.settings.tz, '%H:%M')} 的异动）"
        mid = await self._send(msg, [alert])
        if mid is not None and self.settings.attribution.enabled:
            self._spawn(self._attribute(alert, mid))
        return mid

    async def _resend_undelivered(self, day_key: str, now: datetime) -> None:
        for alert in self.storage.undelivered_alerts(day_key, since=now - RESEND_WINDOW):
            if await self._deliver(alert, resend=True) is not None:
                log.info("已补发 %s 的异动（%s）", alert.symbol, fmt_local(alert.ts, self.settings.tz, "%H:%M"))
            else:
                break  # 还是发不出去：这一轮不再逐条尝试

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _attribute(self, alert: Alert, reply_to: str | None) -> None:
        try:
            res = await self.attributor.explain(alert, macro=alert.macro)
            if alert.id is not None:
                self.storage.save_attribution(alert.id, res.text, res.data)
            title = (f"🧭 {self.settings.universe.display(alert.symbol)} 异动归因"
                     f"（{fmt_local(alert.ts, self.settings.tz, '%H:%M')} {alert.headline.short(alert.ref_label)}）")
            await self.notifier.send(Message(title=title, body=res.text, kind="attribution", reply_to=reply_to))
        except Exception:
            log.exception("归因失败（alert id=%s）", alert.id)

    async def _attribute_group(self, alerts: list[Alert], theme: str, reply_to: str | None) -> None:
        """合成一条的几只一起归因（同一个原因的可能性大）：以动得最多的那只为主，其他几只作为“同时异动”写进提示词。"""
        lead = max(alerts, key=lambda a: abs(a.headline.move_pct))
        others = [a for a in alerts if a is not lead]
        try:
            res = await self.attributor.explain(lead, macro=lead.macro, others=others)
            for a in alerts:
                if a.id is not None:
                    self.storage.save_attribution(a.id, res.text, res.data)
            th = self.settings.universe.themes.get(theme)
            title = (f"🧭 {th.name if th else theme} {len(alerts)} 只一起{'涨' if lead.direction > 0 else '跌'}的归因"
                     f"（{fmt_local(lead.ts, self.settings.tz, '%H:%M')}）")
            await self.notifier.send(Message(title=title, body=res.text, kind="attribution", reply_to=reply_to))
        except Exception:
            log.exception("归因失败（alert id=%s）", [a.id for a in alerts])

    async def drain(self) -> None:
        """等待后台归因任务结束（命令行单次运行和测试用）。"""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


__all__ = ["IntradayMonitor", "MonitorGroup", "build_groups", "prepare_context", "is_us_symbol"]
