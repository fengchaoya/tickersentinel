"""事件汇总：定时从各来源收集事件 → 归并到监控标的 → 同一事件多个来源时取最可靠的 → 入库并记录变化。

可靠程度：手工录入 > 公司 / 交易所公告（SEC 6-K、Form 144、JPX 官方日程）> Finnhub > yfinance 估计。
已确认的事件不会被估计日期覆盖（见 Storage.upsert_event）。
海外主上市代码的事件并入对应的监控标的（000660.KS 的财报 = SKHY 的财报），避免同一件事出现两次。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from tickersentinel.config import Settings
from tickersentinel.models import EVENT_SOURCE_RANK, Event, earnings_key
from tickersentinel.sources.base import EventSource
from tickersentinel.storage import Storage
from tickersentinel.timeutil import utcnow

log = logging.getLogger(__name__)

LAST_REFRESH_KEY = "events:last_refresh"


@dataclass
class RefreshResult:
    events: list[Event]
    changes: list[tuple[Event, str]]
    per_source: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


def _rank(e: Event) -> tuple[int, int, int, int]:
    """手工录入永远优先（用来更正其他来源）；其次已确认 > 估计，有精确时间 > 只有日期，再按来源可靠程度。"""
    return (0 if e.source == "config" else 1, 0 if e.confirmed else 1, 0 if e.at else 1, EVENT_SOURCE_RANK.get(e.source, 9))


def merge(events: list[Event]) -> list[Event]:
    """同一 key 的多条记录只保留最可靠的一条。"""
    best: dict[str, Event] = {}
    for e in events:
        cur = best.get(e.key)
        if cur is None or _rank(e) < _rank(cur):
            best[e.key] = e
    return sorted(best.values(), key=lambda e: (e.day, e.at.timestamp() if e.at else 0.0, e.key))


def scope_symbols(settings: Settings) -> set[str]:
    """收集事件的公司：持仓、海外主上市代码、生效主题的同业和催化剂公司，再加 events.related。"""
    u = settings.universe
    return {*u.event_symbols, *settings.events.related, *u.underlying_map.values()}


def event_in_scope(settings: Settings, e: Event) -> bool:
    """事件是否在现在的监控范围里：宏观日程、手工录入的总在；公司事件要求这家公司还在收集范围
    （卖掉一个主题的最后一只持仓后，它的同业和相关公司的旧事件不再出现在汇总、简报、对话和飞书日历里；数据仍留在库里）。"""
    if e.kind == "macro" or e.source == "config":
        return True
    return e.symbol in scope_symbols(settings)


class EventHub:
    def __init__(self, settings: Settings, sources: list[EventSource], storage: Storage):
        self.settings = settings
        self.sources = sources
        self.storage = storage

    @property
    def symbols(self) -> list[str]:
        """收集哪些公司的事件：持仓（股票）、海外主上市代码、生效主题的同业和催化剂公司，再加 events.related。
        期货、加密货币没有财报和公告。"""
        u, ev = self.settings.universe, self.settings.events
        return list(dict.fromkeys([*u.event_symbols, *ev.related]))

    def in_scope(self, e: Event) -> bool:
        """事件是否在现在的监控范围里（见 event_in_scope）。"""
        return event_in_scope(self.settings, e)

    def update_scope(self) -> None:
        """监控范围变了：各来源的公司名表（事件标题用）和 Form 144 的范围（只看持仓）跟着更新。"""
        u = self.settings.universe
        names = u.event_names(self.settings.events.related)
        for src in self.sources:
            if isinstance(getattr(src, "names", None), dict):
                src.names.update(names)
            if getattr(src, "name", "") == "sec_144":
                src.scope = set(u.equity_symbols)

    def canonical(self, e: Event) -> Event:
        target = self.settings.universe.underlying_map.get(e.symbol)
        if target is None:
            return e
        key = earnings_key(target, e.day) if e.kind == "earnings" else e.key.replace(e.symbol, target)
        return replace(e, symbol=target, key=key)

    def scoped(self, source: EventSource, symbols: list[str]) -> list[str]:
        """来源可以用 scope 限定只看哪些代码（例如 Form 144 只看持仓，不看相关公司）。"""
        scope = getattr(source, "scope", None)
        return [s for s in symbols if s in scope] if scope is not None else symbols

    def manual_events(self) -> list[Event]:
        out = []
        for m in self.settings.events.manual:
            if m.id:
                key = f"{m.kind}:{m.symbol}:{m.id}"
            elif m.kind == "earnings":
                key = earnings_key(m.symbol, m.day)
            else:
                key = f"{m.kind}:{m.symbol}:{m.day.isoformat()}"
            out.append(Event(kind=m.kind, symbol=m.symbol, day=m.day, title=m.title, key=key, time_hint=m.time_hint,
                             confirmed=True, source="config", url=m.url, details={"note": m.note} if m.note else {}))
        return out

    async def refresh(self, now: datetime | None = None) -> RefreshResult:
        now = now or utcnow()
        today = now.astimezone(self.settings.tz).date()
        start, end = today - timedelta(days=7), today + timedelta(days=self.settings.events.lookahead_days)
        syms = self.symbols
        results = await asyncio.gather(*(s.collect(self.scoped(s, syms), start, end) for s in self.sources),
                                       return_exceptions=True)
        collected: list[Event] = []
        per_source: dict[str, int] = {}
        errors: dict[str, str] = {}
        for src, res in zip(self.sources, results):
            if isinstance(res, BaseException):
                errors[src.name] = f"{type(res).__name__}: {res}"
                log.warning("事件来源 %s 失败：%s", src.name, res)
                continue
            per_source[src.name] = len(res)
            collected.extend(res)
        manual = self.manual_events()
        events = merge([self.canonical(e) for e in [*collected, *manual]])
        changes = [(e, c) for e in events if (c := self.storage.upsert_event(e, now))]
        removed = self.storage.sync_manual_events({e.key for e in manual})
        for e in events:
            if e.kind == "earnings" and e.confirmed:
                removed += self.storage.drop_superseded_estimates(e)
        # 只清理本次成功返回的来源；失败的来源保留上次的结果
        removed += self.storage.prune_stale_estimates(list(per_source), now, today)
        if removed:
            log.info("事件日历：清掉 %d 条过时的记录", removed)
        self.storage.kv_set(LAST_REFRESH_KEY, now.isoformat())
        log.info("事件日历刷新：%d 条（%s），变化 %d 条", len(events),
                 "，".join(f"{k} {v}" for k, v in per_source.items()) or "无来源", len(changes))
        return RefreshResult(events, changes, per_source, errors)

    def last_refresh(self) -> datetime | None:
        raw = self.storage.kv_get(LAST_REFRESH_KEY)
        return datetime.fromisoformat(raw) if raw else None

    def upcoming(self, start, end) -> list[Event]:
        return [e for e in self.storage.events_between(start, end) if event_in_scope(self.settings, e)]

    def changed_since(self, ts: datetime) -> list[tuple[Event, str, object]]:
        """Storage.events_changed_since，去掉已经不在监控范围里的公司事件。"""
        return [(e, c, prev) for e, c, prev in self.storage.events_changed_since(ts) if self.in_scope(e)]

    async def aclose(self) -> None:
        """SEC 客户端与新闻共用，由 NewsHub 关闭；SEC 事件来源自己的 aclose 不会关它。"""
        for s in self.sources:
            try:
                await s.aclose()
            except Exception:  # pragma: no cover
                log.debug("关闭事件来源出错", exc_info=True)
