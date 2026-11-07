"""异动归因：收集异动前后的新闻/公告 → 交给模型判断最可能原因（可联网补充检索）→ 排版。

没有大模型 key 或调用失败时，降级为“按时间接近程度列出相关新闻 + 板块拆分判断”。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from tickersentinel.agents.render import (
    alert_benchmark,
    fmt_price,
    macro_prompt_lines,
    pct,
    alert_start,
    rel_to_alert,
    render_attribution,
    render_attribution_fallback,
    sector_judgment,
    split_line,
)
from tickersentinel.config import NIGHT_PREFIX, Settings
from tickersentinel.llm.client import LLMClient, extract_json
from tickersentinel.llm.prompts import ATTRIBUTION_SCHEMA, attribution_system
from tickersentinel.models import Alert, Event, NewsItem, is_sector
from tickersentinel.news.hub import NewsHub
from tickersentinel.textutil import truncate
from tickersentinel.timeutil import ET, fmt_local, tz_abbr, utcnow

if TYPE_CHECKING:
    from tickersentinel.events.hub import EventHub
    from tickersentinel.sources.macro import MacroSnapshot

log = logging.getLogger(__name__)


@dataclass
class AttributionResult:
    text: str
    data: dict | None
    candidates: list[NewsItem] = field(default_factory=list)
    searches: int = 0


EVENTS_BEFORE = timedelta(hours=24)  # 归因时列出异动前 24 小时内的日程（数据发布、议息、同业财报……）
EVENTS_AHEAD = timedelta(hours=24)  # 以及接下来 24 小时的重大日程（事前布局 / 避险）
MAX_EVENTS = 12


class Attributor:
    def __init__(self, settings: Settings, news: NewsHub, llm: LLMClient, events: "EventHub | None" = None):
        self.settings = settings
        self.news = news
        self.llm = llm
        self.events = events

    def nearby_events(self, alert: Alert) -> tuple[list[Event], list[Event]]:
        """事件日历里异动前后的日程：(异动前 24 小时内已经发生的, 接下来 24 小时内的)。
        只有日期没有时间的（多数财报、期权到期）：日期在前一天到当天之间的算“已发生”（盘后财报常是隔夜跳空的原因）。"""
        if self.events is None:
            return [], []
        ts = alert.ts
        lo, hi = ts - EVENTS_BEFORE, ts + EVENTS_AHEAD
        try:
            evs = self.events.upcoming(lo.astimezone(ET).date() - timedelta(days=1), hi.astimezone(ET).date() + timedelta(days=1))
        except Exception as exc:
            log.info("归因时读取事件日历失败：%s", exc)
            return [], []
        today = ts.astimezone(ET).date()
        before, ahead = [], []
        for e in evs:
            if e.at is not None:
                if lo <= e.at <= ts + timedelta(minutes=5):
                    before.append(e)
                elif ts < e.at <= hi:
                    ahead.append(e)
            elif today - timedelta(days=1) <= e.day <= today:
                before.append(e)
            elif e.day == today + timedelta(days=1):
                ahead.append(e)
        return before[-MAX_EVENTS:], ahead[:MAX_EVENTS // 2]

    def window(self, alert: Alert, now: datetime | None = None) -> tuple[datetime, datetime]:
        cfg = self.settings.attribution
        if alert.window_minutes is None:  # 从昨收算的全天累计（含开盘跳空）：回看隔夜
            start = alert.ts - timedelta(hours=cfg.overnight_hours_for_gaps)
        else:
            start = alert.ts - timedelta(minutes=cfg.news_minutes_before + alert.window_minutes)
        end = min(alert.ts + timedelta(minutes=cfg.news_minutes_after), now or utcnow())
        return start, max(end, alert.ts + timedelta(minutes=1))

    def select_candidates(self, alert: Alert, items: list[NewsItem]) -> list[NewsItem]:
        """优先保留时间上最接近异动的条目，再按时间顺序编号。"""
        n = self.settings.attribution.max_candidates
        closest = sorted(items, key=lambda x: abs((x.published_at - alert.ts).total_seconds()))[:n]
        return sorted(closest, key=lambda x: x.published_at)

    def _news_symbols(self, alert: Alert) -> list[str]:
        """只盯盘的资产（夜间信号）按它对应的持仓检索新闻。"""
        inst = self.settings.universe.find(alert.symbol)
        return list(inst.related_to) if inst is not None and not inst.holding and inst.related_to else [alert.symbol]

    async def explain(self, alert: Alert, macro: "MacroSnapshot | None" = None,
                      others: list[Alert] | None = None) -> AttributionResult:
        """others：同一主题、同时同方向异动、合并在同一条推送里的其他几条（一起归因，见 jobs/merge.py）。"""
        others = list(others or [])
        spans = [self.window(a) for a in [alert, *others]]  # 合成一条的几只：新闻窗口取并集（有的要回看隔夜）
        start, end = min(s for s, _ in spans), max(e for _, e in spans)
        try:
            news_syms = list(dict.fromkeys(s for a in [alert, *others] for s in self._news_symbols(a)))
            # 板块新闻按这只股票所属的主题
            items = await self.news.collect(news_syms, start, end, include_sector=True, sector_of=[alert.symbol])
        except Exception as exc:  # 新闻源全部失败也要给出结果
            log.warning("归因时获取新闻失败：%s", exc)
            items = []
        candidates = self.select_candidates(alert, items)

        if not self.llm.available("attribution"):
            return AttributionResult(render_attribution_fallback(alert, candidates, self.settings, "未配置大模型"), None, candidates)

        cfg = self.settings.attribution
        try:
            res = await self.llm.complete(
                "attribution",
                system=attribution_system(self.settings, alert.symbol),
                prompt=self.build_prompt(alert, candidates, macro, others=others),
                json_schema=ATTRIBUTION_SCHEMA,
                web_search=cfg.use_web_search,
                max_searches=cfg.max_web_searches,
            )
        except Exception as exc:
            log.exception("归因调用大模型失败")
            text = render_attribution_fallback(alert, candidates, self.settings, f"大模型调用失败：{type(exc).__name__}")
            return AttributionResult(text, None, candidates)

        data = extract_json(res.text, required_key="causes")
        if data is None:
            log.warning("归因结果不是预期的 JSON，直接转发原文")
            return AttributionResult(res.text or "（模型未返回内容）", None, candidates, res.searches)
        text = render_attribution(alert, data, candidates, res.citations, self.settings)
        return AttributionResult(text, data, candidates, res.searches)

    def build_prompt(self, alert: Alert, candidates: list[NewsItem], macro: "MacroSnapshot | None" = None,
                     others: list[Alert] | None = None) -> str:
        s = self.settings
        u, tz = s.universe, s.tz
        inst = u.get(alert.symbol)
        lines = [
            f"【异动】{u.display(alert.symbol)}（{inst.name}） {fmt_local(alert.ts, tz, '%Y-%m-%d %H:%M')} {tz_abbr(alert.ts, tz)}"
            f"（{alert.ts:%H:%M} UTC）",
        ]
        ref = alert.ref_label
        theme = u.theme_of(alert.symbol)
        if theme.key != "default":
            lines.append(f"- 板块：{theme.name}" + (f"（{theme.focus}）" if theme.focus and theme.focus != theme.name else ""))
        if alert.segment_label:
            lines.append(f"- 时段：{alert.segment_label}")
        if not inst.holding and inst.related_to:
            who = "、".join(inst.related_to)
            lines.append(f"- 这是持仓 {who} 的关联标的（{u.label(alert.symbol)}，夜间信号），按 {who} 找原因")
        lines += [f"- 触发：{tr.describe(ref)}" for tr in alert.triggers]
        if (start := alert_start(alert)) is not None:
            lines.append(f"- 这波涨跌的起点：{fmt_local(start, tz, '%H:%M')}（日内{'低点' if alert.direction > 0 else '高点'}，"
                         f"{ref} {alert.headline.anchor_pct:+.1f}%）；检测到是 {fmt_local(alert.ts, tz, '%H:%M')}")
        if alert.symbol.startswith(NIGHT_PREFIX):
            members = "、".join(f"{u.label(m)}（{m}）" for m in u.basket_members(alert.symbol))
            lines.append(f"- 这是 {members} 的等权篮子，{ref} {pct(alert.day_change_pct)}；各成分的同期涨跌见下面的“同期”")
        else:
            lines.append(f"- 现价 {fmt_price(alert.symbol, alert.price)}，{ref} {pct(alert.day_change_pct)}")
        wlabel = f"{alert.window_minutes}分钟" if alert.window_minutes else ref
        bench = alert_benchmark(alert, s)
        if alert.benchmark_move_pct is not None and alert.beta is not None:
            lines.append(
                f"- 拆分（{wlabel}）：原始 {pct(alert.move_pct)}；板块 {u.label(bench or '')} {pct(alert.benchmark_move_pct)} × β{alert.beta:.2f}"
                f" = {pct(alert.beta * alert.benchmark_move_pct)}；个股特异 {pct(alert.abnormal_move_pct)}"
                + (f"（{sector_judgment(alert)}）" if sector_judgment(alert) else "")
            )
        if alert.peers_move_pct:
            lines.append(f"- 同期（{wlabel}）：" + "，".join(f"{u.label(k)} {pct(v)}" for k, v in alert.peers_move_pct.items()))
        if alert.volume_ratio is not None:
            lines.append(f"- 成交量为历史同时段的 {alert.volume_ratio:.1f} 倍")
        if others:
            lines += ["", "【同一主题同时异动】（和上面这条合在同一条推送里，多半是同一个原因：先看是不是板块共同的因素，"
                      "再看哪一只有自己的消息；结论里点明是几只一起动）"]
            for o in others:
                sl = split_line(o, s)
                lines.append(f"- {u.display(o.symbol)}：" + "；".join(tr.describe(o.ref_label) for tr in o.triggers)
                             + (f"；{sl}" if sl else ""))
        if macro is not None and macro.quotes:
            lines += ["", "【宏观背景】（异动时；括号内为较昨收或 24 小时、以及近 1 小时的变化；利率与期货约延迟 10–15 分钟）",
                      *macro_prompt_lines(macro, s, themes=u.theme_keys_of(alert.symbol))]
        before, ahead = self.nearby_events(alert)
        if before:
            lines += ["", f"【异动前 24 小时内的日程】（事件日历；时间为 {tz_abbr(alert.ts, tz)}）",
                      *[self._event_line(e, alert) for e in before]]
        if ahead:
            lines += ["", "【接下来 24 小时的日程】（可能引起事前布局或避险）", *[self._event_line(e, alert) for e in ahead]]

        lines += ["", f"【候选新闻/公告】（按发布时间排序；时间为 {tz_abbr(alert.ts, tz)}）"]
        for i, it in enumerate(candidates, 1):
            tag = "行业" if any(is_sector(x) for x in it.symbols) else ",".join(it.symbols)
            kind = "公告" if it.kind == "filing" else (it.source or it.provider)
            summary = f" — {truncate(it.summary, 200)}" if it.summary else ""
            lines.append(
                f"#{i} [{fmt_local(it.published_at, tz, '%m-%d %H:%M')}，{rel_to_alert(alert, it.published_at)}]"
                f" [{kind}] [{tag}] {it.title}{summary}"
            )
        if not candidates:
            lines.append("（时间窗口内没有找到候选条目）")

        qinst = u.find(inst.related_to[0]) if not inst.holding and inst.related_to else inst
        sector_terms = [q for qs in u.sector_news([alert.symbol]).values() for q in qs.get("en", [])]
        terms = [t for t in (qinst or inst).queries.get("en", [])] + sector_terms
        lines += ["", "【可补充检索的方向】" + "；".join(terms[:6])]
        return "\n".join(lines)

    def _event_line(self, e: Event, alert: Alert) -> str:
        tz, u = self.settings.tz, self.settings.universe
        who = "" if e.kind == "macro" else f"[{e.symbol}{'，持仓' if e.symbol in u.symbols else ''}] "
        est = "" if e.confirmed else "（预计）"
        if e.at is not None:
            return f"- {fmt_local(e.at, tz, '%m-%d %H:%M')}（{rel_to_alert(alert, e.at)}）{who}{e.title}{est}"
        return f"- {e.day:%m-%d}{(' ' + e.time_hint) if e.time_hint else ''}（具体时间未知）{who}{e.title}{est}"
