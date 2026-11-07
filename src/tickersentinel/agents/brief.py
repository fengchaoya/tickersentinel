"""定时简报：盘前 / 收盘 / 周报。

流程：行情快照 → 新闻与公告（去重，盘前/收盘只取没推送过的）→ 近期财报日程 → 盘中异动回顾
     → 大模型撰写（未配置 key 时降级为标题列表）→ 推送 → 标记新闻已读。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

import pandas as pd

from tickersentinel.agents.render import fmt_price, link, link_refs, pct, short_source
from tickersentinel.config import BASKET_PREFIX, Settings, Theme, is_us_symbol
from tickersentinel.detect.baseline import estimate_beta
from tickersentinel.detect.basket import add_baskets
from tickersentinel.events.digest import event_line, events_prompt_lines, local_day
from tickersentinel.events.hub import EventHub
from tickersentinel.llm.client import LLMClient
from tickersentinel.llm.prompts import BRIEF_FOCUS, brief_system
from tickersentinel.market_calendar import MarketCalendar
from tickersentinel.models import SECTOR, Event, Message, NewsItem, Quote, is_sector, sector_theme
from tickersentinel.news.hub import NewsHub
from tickersentinel.sources.base import PriceSource
from tickersentinel.storage import Storage
from tickersentinel.textutil import truncate
from tickersentinel.timeutil import fmt_local, tz_abbr, utcnow, weekday_cn

log = logging.getLogger(__name__)


def _last_close(q: Quote | None, today: date) -> float | None:
    """今天（交易所日期）之前最近一个交易日的收盘价。有分钟数据时是 prev_close；
    只有日线时（session="closed"，如取不到盘前成交）price 就是日线最后一行的收盘：
    那一行早于今天时它才是“昨收”，否则（盘中/收盘后手动运行）仍取 prev_close。"""
    if q is None:
        return None
    if q.session == "closed" and (q.day is None or q.day < today):
        return q.price
    return q.prev_close


# 盘前 / 收盘简报回看“上一份简报之后”的新闻，最多回看这么久（三天长周末 + 余量）
MAX_GAP_HOURS = 96

BriefKind = Literal["premarket", "close", "weekly"]
TITLES = {"premarket": "☀️ 盘前简报", "close": "🌙 收盘简报", "weekly": "📅 周报"}


@dataclass
class BriefResult:
    message: Message
    items: list[NewsItem]
    used_llm: bool


class BriefBuilder:
    def __init__(self, settings: Settings, calendar: MarketCalendar, prices: PriceSource, news: NewsHub,
                 events: EventHub | None, storage: Storage, llm: LLMClient):
        self.settings = settings
        self.calendar = calendar
        self.prices = prices
        self.news = news
        self.events = events
        self.storage = storage
        self.llm = llm

    # ───────── 主流程 ─────────

    async def build(self, kind: BriefKind, now: datetime | None = None) -> BriefResult:
        now = now or utcnow()
        s = self.settings
        start = self.window_start(kind, now)

        snapshot = await self._snapshot(kind, now)
        try:
            items = await self.news.collect(s.universe.symbols, start, now)
        except Exception as exc:
            log.warning("简报获取新闻失败：%s", exc)
            items = []
        if kind != "weekly":
            items = self.storage.unseen(items)
        items = self._select(items)
        events = self._events(kind, now)
        recap = self._alerts_recap(kind, now)

        body, used_llm = None, False
        if self.llm.available("brief"):
            try:
                res = await self.llm.complete(
                    "brief",
                    system=brief_system(s),
                    prompt=self._prompt(kind, now, snapshot, items, events, recap),
                )
                body, used_llm = link_refs(res.text, items), True
            except Exception as exc:
                log.exception("简报调用大模型失败，改用降级排版")
                snapshot = [*snapshot, f"（大模型调用失败：{type(exc).__name__}）"]
        if body is None:
            body = self._fallback(snapshot, items, events, recap)
        if kind == "weekly" and s.briefs.weekly.linkage_check and self.prices is not None:
            from tickersentinel.scope.health import linkage_report

            health = await linkage_report(s, self.prices, brief=True)  # 只有发现问题才有内容；失败返回空，不影响周报
            if health:
                body += "\n\n" + health
        if self.news.last_errors:
            body += "\n\n⚠️ 部分数据源失败：" + "；".join(f"{k}" for k in self.news.last_errors)
        title = f"{TITLES[kind]} · {fmt_local(now, s.tz, '%m-%d')} {weekday_cn(now, s.tz)}"
        return BriefResult(Message(title=title, body=body, kind="brief"), items, used_llm)

    def window_start(self, kind: BriefKind, now: datetime) -> datetime:
        """新闻窗口的起点：至少回看 lookback_hours；盘前 / 收盘简报再往前延到上一份盘前或收盘简报（最多 MAX_GAP_HOURS），
        否则周六周报之后到周一盘前之间、节假日前后的新闻和公告哪份简报都不收。已读过的条目由 storage.unseen 过滤，重叠无妨。"""
        start = now - timedelta(hours=getattr(self.settings.briefs, kind).lookback_hours)
        if kind in ("premarket", "close"):
            last = self.storage.last_brief_at(("premarket", "close"))
            if last is not None and last < start:
                start = max(last, now - timedelta(hours=MAX_GAP_HOURS))
        return start

    def _select(self, items: list[NewsItem]) -> list[NewsItem]:
        """每个标的（及行业）最多保留 max_news_per_symbol 条，公告全部保留；按时间倒序。"""
        cap = self.settings.briefs.max_news_per_symbol
        counts: dict[str, int] = {}
        out = []
        for it in sorted(items, key=lambda x: x.published_at, reverse=True):
            key = it.symbols[0] if it.symbols else SECTOR
            if it.kind == "filing" or counts.get(key, 0) < cap:
                out.append(it)
                if it.kind != "filing":
                    counts[key] = counts.get(key, 0) + 1
        return out[: self.settings.llm.brief_max_items]

    def _sector_name(self, tag: str) -> str:
        """SECTOR:memory → “行业·存储芯片”（只有一个主题时就写“行业”）。"""
        u = self.settings.universe
        th = u.themes.get(sector_theme(tag) or "")
        return f"行业·{th.name}" if th is not None and len(u.active_themes) > 1 else "行业"

    def _news_tag(self, it: NewsItem) -> str:
        sec = next((x for x in it.symbols if is_sector(x)), None)
        return self._sector_name(sec) if sec else ",".join(it.symbols)

    def _events(self, kind: BriefKind, now: datetime) -> list[Event]:
        """事件日历（由 EventHub 定时刷新入库）里今天起的事件；周报看得更远。"""
        if self.events is None:
            return []
        s = self.settings
        days = s.events.digest.days_ahead if kind == "weekly" else s.briefs.events_days_ahead
        today = now.astimezone(s.tz).date()
        try:
            evs = self.events.upcoming(today - timedelta(days=1), today + timedelta(days=days + 1))
        except Exception as exc:
            log.warning("读取事件日历失败：%s", exc)
            return []
        return [e for e in evs if today <= local_day(e, s.tz) <= today + timedelta(days=days)]

    def _alerts_recap(self, kind: BriefKind, now: datetime) -> list[str]:
        tz = self.settings.tz
        if kind == "premarket":
            return []
        start = now - timedelta(hours=14 if kind == "close" else 24 * 7)
        lines = []
        for a in self.storage.alerts_between(start, now):
            summary = self.storage.attribution_summary(a.id) if a.id else None
            when = fmt_local(a.ts, tz, "%H:%M" if kind == "close" else "%m-%d %H:%M")
            lines.append(f"- {when} {self.settings.universe.display(a.symbol)} {a.headline.short(a.ref_label)}" + (f" → {summary}" if summary else ""))
        return lines

    # ───────── 行情快照 ─────────

    async def _snapshot(self, kind: BriefKind, now: datetime) -> list[str]:
        try:
            if kind == "premarket":
                return await self._premarket_snapshot(now)
            if kind == "close":
                return await self._close_snapshot(now)
            return await self._weekly_snapshot()
        except Exception as exc:
            log.warning("行情快照失败：%s", exc)
            return [f"- 行情获取失败：{type(exc).__name__}: {truncate(str(exc), 120)}"]

    def _sector_rows(self) -> list[tuple[Theme, str | None, list[str]]]:
        """简报里每个生效主题一行：(主题, 基准, 同业)。只有一个主题时不写主题名，和原来一样。"""
        u = self.settings.universe
        rows = []
        for th in u.active_themes:
            holders = u.theme_holdings(th.key)
            bench = u.benchmark_for(holders[0]) if holders else None
            rows.append((th, bench, list(th.peers)))
        return rows

    def _sector_label(self, th: Theme, bench: str | None) -> str:
        u = self.settings.universe
        name = u.label(bench) if bench else "—"
        return name if len(u.active_themes) <= 1 else f"{th.name}（{name}）"

    def _fetch_list(self, extra: list[str]) -> list[str]:
        """取行情的代码：篮子换成成分。"""
        u = self.settings.universe
        out: list[str] = []
        for s in [*u.symbols, *(b for _, b, _ in self._sector_rows() if b), *extra]:
            for x in (u.basket_members(s) if s.startswith(BASKET_PREFIX) else [s]):
                if x not in out:
                    out.append(x)
        return out

    async def _premarket_snapshot(self, now: datetime) -> list[str]:
        u = self.settings.universe
        today = self.calendar.exchange_date(now)
        syms = self._fetch_list([*u.us_peers, *u.foreign_refs])
        q = await self.prices.latest_quotes(syms)
        lines = []
        for inst in u.holdings:
            sq = q.get(inst.symbol)
            last_close = _last_close(sq, today)
            line = f"- {inst.symbol}：昨收 {fmt_price(inst.symbol, last_close)}"
            if sq and sq.session == "pre" and sq.price is not None:
                line += f"，盘前 {fmt_price(inst.symbol, sq.price)}（{pct(sq.change_pct)}）"
            if inst.underlying and (uq := q.get(inst.underlying)) and uq.price is not None:
                day = f" {uq.day:%m-%d}" if uq.day else ""
                line += f"；{u.label(inst.underlying)}{day} 收 {fmt_price(inst.underlying, uq.price)}（{pct(uq.change_pct)}）"
                fx = q.get(inst.fx_symbol) if inst.fx_symbol else None
                if fx and fx.price and inst.adr_ratio and last_close:
                    # ADR 相对首尔股价长期有溢价（上市以来均值约 +36%），所以展示溢价水平而不是“隐含价较昨收”
                    implied = uq.price / inst.adr_ratio / fx.price
                    line += f" → 折合 ${implied:,.2f}，ADR 溢价 {pct((last_close / implied - 1) * 100)}"
            lines.append(line)
        multi = len(u.active_themes) > 1
        for th, bench, peer_list in self._sector_rows():
            label = self._sector_label(th, bench)
            if bench and bench.startswith(BASKET_PREFIX):
                chg = [q[m].change_pct for m in u.basket_members(bench) if m in q and q[m].change_pct is not None]
                if chg:
                    lines.append(f"- 板块 {label}：成分最近一个交易日平均 {pct(sum(chg) / len(chg))}")
            elif bench and (bq := q.get(bench)) is not None:
                lines.append(f"- 板块 {label}：昨收 {fmt_price(bench, _last_close(bq, today))}"
                             + (f"，盘前 {pct(bq.change_pct)}" if bq.session == "pre" else ""))
            peers = [f"{u.label(p)} {pct(q[p].change_pct)}" for p in peer_list if p in q]
            if peers:
                lines.append(f"- {th.name + ' ' if multi else ''}同业最近收盘：" + "｜".join(peers))
        return lines

    async def _close_snapshot(self, now: datetime) -> list[str]:
        u = self.settings.universe
        syms = self._fetch_list(u.us_peers)
        daily = await self.prices.daily_bars(syms, lookback_days=self.settings.monitor.price.beta_lookback_days + 5)
        today = self.calendar.exchange_date(now)
        baskets = {b: u.basket_members(b) for _, b, _ in self._sector_rows() if b and b.startswith(BASKET_PREFIX)}
        if baskets:
            _, daily = add_baskets(baskets, daily=daily)

        def day_change(sym: str) -> tuple[float | None, float | None]:
            df = daily.get(sym)
            if df is None or len(df) < 2:
                return None, None
            c = df["close"].dropna()
            if c.index[-1].date() != today:
                return float(c.iloc[-1]), None  # 今天的数据还没出来
            return float(c.iloc[-1]), (float(c.iloc[-1]) / float(c.iloc[-2]) - 1) * 100

        lines = []
        for sym in u.symbols:
            close, chg = day_change(sym)
            line = f"- {sym}：收 {fmt_price(sym, close)}（{pct(chg)}）"
            bench = u.benchmark_for(sym)
            bchg = day_change(bench)[1] if bench else None
            if chg is not None and bchg is not None:
                beta, fb = estimate_beta(daily.get(sym), daily.get(bench), before=today,
                                         lookback=self.settings.monitor.price.beta_lookback_days)
                line += f"｜板块贡献 {pct(beta * bchg)}（β{beta:.2f}）｜个股特异 {pct(chg - beta * bchg)}"
            lines.append(line)
        multi = len(u.active_themes) > 1
        for th, bench, peer_list in self._sector_rows():
            if bench:
                lines.append(f"- 板块 {self._sector_label(th, bench)}：{pct(day_change(bench)[1])}")
            peers = [f"{u.label(p)} {pct(day_change(p)[1])}" for p in peer_list if is_us_symbol(p)]
            if peers:
                lines.append(f"- {th.name + ' ' if multi else ''}同业：" + "｜".join(peers))
        return lines

    async def _weekly_snapshot(self) -> list[str]:
        u = self.settings.universe
        daily = await self.prices.daily_bars(self._fetch_list(u.peers), lookback_days=15)
        baskets = {b: u.basket_members(b) for _, b, _ in self._sector_rows() if b and b.startswith(BASKET_PREFIX)}
        if baskets:
            _, daily = add_baskets(baskets, daily=daily)
        benches = [b for _, b, _ in self._sector_rows() if b]
        syms = list(dict.fromkeys([*u.symbols, *benches, *u.peers]))
        lines = []
        for sym in syms:
            df = daily.get(sym)
            if df is None or df.empty:
                continue
            c = df["close"].dropna()
            last_day = c.index[-1]
            base = c[c.index <= last_day - pd.Timedelta(days=7)]
            if base.empty:
                continue
            chg = (float(c.iloc[-1]) / float(base.iloc[-1]) - 1) * 100
            lines.append(f"- {u.label(sym)}：本周 {pct(chg)}，收 {fmt_price(sym, float(c.iloc[-1]))}")
        return lines

    # ───────── 文本 ─────────

    def _prompt(self, kind: BriefKind, now: datetime, snapshot: list[str], items: list[NewsItem],
                events: list[Event], recap: list[str]) -> str:
        tz = self.settings.tz
        parts = [
            BRIEF_FOCUS[kind],
            f"【时间】{fmt_local(now, tz, '%Y-%m-%d %H:%M')} {tz_abbr(now, tz)} {weekday_cn(now, tz)}",
            # 本地小模型常把日期对应的星期算错：直接给出未来一周的对照
            "【日期参照】" + "、".join(f"{fmt_local(d, tz, '%m-%d')} {weekday_cn(d, tz)}"
                                     for d in (now + timedelta(days=i) for i in range(8))),
            "【行情】", *(snapshot or ["（无）"]),
        ]
        if events:
            parts += ["【近期日程】（宏观：议息、经济数据、国债拍卖、休市等；公司：财报、内部人拟减持、锁定期到期等）",
                      *events_prompt_lines(events, tz)]
        if recap:
            parts += ["【盘中异动记录】", *recap]
        parts.append("【新闻与公告条目】（编号. [时间|来源|相关标的] 标题 — 摘要）")
        for i, it in enumerate(items, 1):
            tag = self._news_tag(it)
            src = "SEC公告" if it.kind == "filing" else (it.source or it.provider)
            summary = f" — {truncate(it.summary, 160)}" if it.summary else ""
            parts.append(f"{i}. [{fmt_local(it.published_at, tz, '%m-%d %H:%M')}|{src}|{tag}] {it.title}{summary}")
        if not items:
            parts.append("（本时段没有新的新闻条目）")
        return "\n".join(parts)

    def _fallback(self, snapshot: list[str], items: list[NewsItem], events: list[Event], recap: list[str]) -> str:
        u, tz = self.settings.universe, self.settings.tz
        parts = ["**行情**", *(snapshot or ["- （无）"])]
        if events:
            parts += ["", "**近期日程**", *[event_line(e, tz) for e in events]]
        if recap:
            parts += ["", "**盘中异动**", *recap]
        parts += ["", "**新闻**（未配置大模型，仅列标题）"]
        groups = [*u.symbols, *dict.fromkeys(i.symbols[0] for i in items if i.symbols and is_sector(i.symbols[0]))]
        for g in groups:
            sel = [i for i in items if (i.symbols[:1] == (g,))][:5]
            if not sel:
                continue
            parts.append(f"{self._sector_name(g) if is_sector(g) else g}：")
            parts += [f"- {fmt_local(i.published_at, tz, '%H:%M')} {link(truncate(i.title, 70), i.url)} — {short_source(i)}" for i in sel]
        if not items:
            parts.append("- 本时段没有新的新闻")
        return "\n".join(parts)
