"""对话用的查询工具（原生 tool calling）：宏观变量、盘中走势与板块 / 个股拆分、新闻检索、历史异动与归因、事件日历。

（个股最新价不做成工具：读者在券商 App 里看得更直观，2026-10-21 按用户要求换成宏观变量。）

约定：
- 全部只读；每个工具自己处理错误，返回 ToolOutput(is_error=True) 的一句说明，模型据此告诉读者“没查到 / 查询失败”，
  绝不抛异常打断对话。
- 结果是紧凑的 JSON 或短文本：本地模型生成约 17 token/秒，处理长提示词也要时间。时间一律换成读者本地时区。
- 依赖缺失的工具不提供：没有宏观数据源就没有 get_macro，没有行情源就没有 get_intraday，没有 NewsHub 就没有 search_news；
  异动记录与事件日历只读数据库，总是可用。
- 新闻条目编号为 [n1] [n2] ……（同一次提问内连续编号），模型只引用编号，链接由 ToolSession.link_refs 替换，
  不让模型抄写或编造网址（与简报、归因的做法一致）。
- 代码和提示词里不写死行业或股票：标的、基准、同业、名称都来自 settings.universe。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from tickersentinel.agents.render import alert_benchmark, link, macro_prompt_lines, sector_judgment, short_source
from tickersentinel.config import BASKET_PREFIX, is_us_symbol
from tickersentinel.detect.basket import add_baskets
from tickersentinel.detect.baseline import (
    Baseline,
    SessionContext,
    estimate_beta,
    prev_close_from_daily,
    session_frame,
)
from tickersentinel.detect.price import split_context
from tickersentinel.events.digest import events_prompt_lines, local_day
from tickersentinel.llm.base import ToolOutput, ToolSpec
from tickersentinel.models import SECTOR, Alert, NewsItem
from tickersentinel.news.dedupe import dedupe
from tickersentinel.sources.base import NewsQuery, SourceError
from tickersentinel.textutil import truncate
from tickersentinel.timeutil import ET, fmt_local, tz_abbr, utcnow, weekday_cn

if TYPE_CHECKING:
    import pandas as pd

    from tickersentinel.config import Settings
    from tickersentinel.events.hub import EventHub
    from tickersentinel.market_calendar import MarketCalendar
    from tickersentinel.news.hub import NewsHub
    from tickersentinel.sources.base import PriceSource
    from tickersentinel.sources.macro import MacroSource
    from tickersentinel.storage import Storage

log = logging.getLogger(__name__)

TOOL_TIMEOUT = 60.0  # 单个工具的超时（秒）：yfinance 偶尔很慢，不能让读者一直等
MAX_NEWS = 10
MAX_FILINGS = 4  # search_news 里留给 SEC 公告的名额（包含在 MAX_NEWS 之内）
MAX_ALERTS = 15
MAX_EVENTS = 40  # 加入宏观日程后，两周内常有 30 多条
DAILY_CACHE_SECONDS = 900  # β 用的日线在盘中不会变，缓存 15 分钟，连续追问时少一次下载

_TICKER = re.compile(r"^[A-Z0-9^][A-Z0-9.\-=^]{0,14}$")
_SPLIT = re.compile(r"[,，、;；\s]+")
_HANGUL = re.compile(r"[가-힯]")
_CJK = re.compile(r"[一-鿿]")
# 新闻编号：[n3]、【n3】、[n1, n2]，以及模型偶尔写成的 [**n7**]（实测 qwen3.8-27b 会这样加粗）
_MD_URL_LINK = re.compile(r"\[([^\]\n]*)\]\(\s*https?://[^)\s]*\s*\)")
_BARE_URL = re.compile(r"\(?<?https?://[^\s)>\]，。；、]+>?\)?")
_REF = re.compile(r"[\[【]\s*\**\s*(n\d{1,3}(?:\s*[,，、]\s*n\d{1,3})*)\s*\**\s*[\]】]", re.I)


# ───────────────────────── 小工具 ─────────────────────────


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _with_official_close(view: "pd.DataFrame", daily: "pd.DataFrame | None", day: Any) -> "pd.DataFrame":
    """已收盘的交易日：把分钟线最后一根的收盘价换成日线的官方收盘价（有的话）。"""
    if view is None or view.empty or daily is None or daily.empty or "close" not in daily.columns:
        return view
    c = daily["close"].dropna()
    c = c[c.index.date == day]
    if c.empty:
        return view
    out = view.copy()
    out.iloc[-1, out.columns.get_loc("close")] = float(c.iloc[-1])
    return out


def _num(x: float | None, digits: int = 2) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return round(v, digits) if digits else round(v)


def _price_digits(symbol: str) -> int:
    return 0 if symbol.endswith((".KS", ".KQ", ".T")) else 2


def _int_arg(args: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    """本地模型偶尔把数字写成字符串（"30"）或小数；无法解析时用默认值，超出范围时截到边界。"""
    raw = args.get(key)
    try:
        value = int(float(raw)) if raw not in (None, "") else default
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def _str_arg(args: dict[str, Any], key: str) -> str:
    raw = args.get(key)
    return str(raw).strip() if raw is not None else ""


def detect_lang(text: str) -> str:
    if _HANGUL.search(text):
        return "ko"
    if _CJK.search(text):
        return "zh"
    return "en"


def market_status(calendar: "MarketCalendar", now: datetime, tz) -> str:
    """美股现在处于什么时段，例如 “盘中（15:00 CDT 收盘）” / “盘前（今天 08:30 CDT 开盘）” / “休市（下一个交易日 …）”。"""
    day = calendar.exchange_date(now)
    s = calendar.session(day)
    hhmm = lambda ts: f"{fmt_local(ts, tz, '%H:%M')} {tz_abbr(ts, tz)}"  # noqa: E731
    if s is not None:
        if s.open <= now < s.close:
            return f"盘中（{hhmm(s.close)} 收盘" + ("，今天提前收盘" if s.early_close else "") + "）"
        if now < s.open:
            label = "盘前" if now.astimezone(ET).hour >= 4 else "未开盘"
            return f"{label}（今天 {hhmm(s.open)} 开盘）"
        if now.astimezone(ET).hour < 20:
            return f"盘后（今天已于 {hhmm(s.close)} 收盘）"
    nxt = calendar.next_session(day + timedelta(days=1))
    label = "已收盘" if s is not None else "休市"
    return f"{label}（下一个交易日 {fmt_local(nxt.open, tz, '%m-%d')} {weekday_cn(nxt.open, tz)} {hhmm(nxt.open)} 开盘）"


# ───────────────────────── 工具集 ─────────────────────────


class ChatTools:
    """持有数据依赖，定义工具；每次提问用 session() 得到一个 ToolSession（新闻编号各自独立，并发提问互不干扰）。"""

    def __init__(self, settings: "Settings", storage: "Storage", *, prices: "PriceSource | None" = None,
                 news: "NewsHub | None" = None, events: "EventHub | None" = None,
                 calendar: "MarketCalendar | None" = None, macro: "MacroSource | None" = None,
                 clock: Callable[[], datetime] = utcnow):
        self.settings = settings
        self.macro = macro
        self.storage = storage
        self.prices = prices
        self.news = news
        self.events = events
        self.calendar = calendar
        self.clock = clock
        self._daily_cache: dict[tuple[str, ...], tuple[float, dict]] = {}

    # ───────── 名称与代码 ─────────

    def known_symbols(self) -> list[str]:
        u = self.settings.universe
        benches = [b for b in u.benchmarks if not b.startswith(BASKET_PREFIX)]
        return list(dict.fromkeys([*u.symbols, *benches, *u.peers, *u.foreign_refs, *(i.symbol for i in u.monitored),
                                   *u.catalysts, *self.settings.events.related]))

    def all_names(self) -> dict[str, str]:
        """代码 → 名称：主题里的同业 / 夜间信号 / 相关公司、display_names、events.related。"""
        return {k: v for k, v in self.settings.universe.event_names(self.settings.events.related).items() if v}

    def name_of(self, symbol: str) -> str:
        u = self.settings.universe
        for inst in u.instruments:
            if inst.symbol == symbol:
                return inst.name.split()[0] if inst.name.strip() else symbol
        return self.all_names().get(symbol, "")

    def resolve(self, text: str, *, allow_unknown: bool = True) -> str | None:
        """把读者 / 模型写的代码或名称（“MU”“美光”“SK hynix”“海力士”）对到代码；
        allow_unknown=True 时，像代码的字符串（如 NVDA）原样大写返回。"""
        t = (text or "").strip()
        if not t:
            return None
        low = t.lower()
        for sym in self.known_symbols():
            if sym.lower() == low:
                return sym
        u = self.settings.universe
        names: list[tuple[str, str]] = []  # 公司名 / 显示名：允许部分匹配（“海力士” → SK海力士 ADR）
        for inst in u.watchlist:
            names += [(inst.symbol, n) for n in [inst.name, *inst.name.split()]]
        names += [(s, n) for s, n in self.all_names().items()]
        # 推送里显示的名称（“SK海力士(首尔)”）也认：事件用的公司名会把主上市代码并到持仓名下，不能只看那一份
        names += [(s, n) for th in u.themes.values() for s, n in th.names().items()]
        names += list(u.display_names.items())
        terms = [(inst.symbol, q) for inst in u.watchlist for qs in inst.queries.values() for q in qs]
        for sym, n in [*names, *terms]:  # 检索词只做完全匹配：“NAND”“stock”这类词不能被当成某只股票
            if n and n.strip().lower() == low:
                return sym
        # 部分匹配：中文 / 韩文名称允许子串（“海力士”→ SK海力士）；英文只认 ≥4 个字母的整词（如名称是“Micron Technology”时的“Technology”），
        # 否则 ON、DIS、AD 这类真实代码会被当成 Micron、SanDisk、SK hynix ADR 的一部分（2026-10-21 审查发现）
        if len(t) >= 2:
            word = re.compile(rf"(?<![a-z0-9]){re.escape(low)}(?![a-z0-9])") if t.isascii() and len(t) >= 4 else None
            for sym, n in names:
                if not n:
                    continue
                if (not t.isascii() and low in n.lower()) or (word is not None and word.search(n.lower())):
                    return sym
        up = t.upper()
        return up if allow_unknown and _TICKER.match(up) else None

    # ───────── 工具定义 ─────────

    def specs(self) -> list[ToolSpec]:
        u = self.settings.universe
        out: list[ToolSpec] = []
        if self.macro is not None:
            out.append(ToolSpec(
                "get_macro",
                "查询宏观变量的最新值和变化：美债 10 年 / 30 年收益率（含财政部官方 2 年期与 2s10s 利差）、WTI 原油、黄金、白银、"
                "标普 500 与纳斯达克（美股休市时为股指期货）、VIX、美元指数、美元兑日元、比特币。"
                "读者问利率、油价、金银、大盘、美元、风险偏好 / 流动性，或想知道异动是不是宏观带动的时候调用。",
                {"type": "object", "properties": {}, "required": []},
            ))
        if self.prices is not None:
            if self.calendar is not None:
                out.append(ToolSpec(
                    "get_intraday",
                    "查询某只美股今天（休市时为最近一个交易日）的走势：较昨收涨跌、最近 N 分钟涨跌，"
                    f"各自拆成板块部分（β × {u.benchmark} 涨跌）和个股特异部分，并列出同业同期涨跌。"
                    "读者问“为什么涨 / 跌”“是板块带的还是个股的”“这半小时怎么了”时调用。",
                    {"type": "object", "properties": {
                        "symbol": {"type": "string", "description": "美股代码或名称，如 MU"},
                        "minutes": {"type": "integer", "description": "最近多少分钟的窗口，默认 30（1–390）"},
                    }, "required": ["symbol"]},
                ))
        if self.news is not None:
            out.append(ToolSpec(
                "search_news",
                "检索最近的新闻与公告（含 SEC 公告）。query 可以是持仓代码 / 名称——会自动用预设的中英韩检索词一起查，"
                "不需要换语言或换写法重复检索；也可以是任意关键词（如 “HBM 价格”）。"
                "读者问最近有什么新闻、某件事的进展、涨跌的可能原因时调用。结果带编号 [n1] [n2]…，回答引用时只写编号。",
                {"type": "object", "properties": {
                    "query": {"type": "string", "description": "代码、公司名或关键词"},
                    "hours": {"type": "integer", "description": "回看多少小时，默认 24（1–168）"},
                }, "required": ["query"]},
            ))
        out.append(ToolSpec(
            "recent_alerts",
            "查询最近几天系统推送过的盘中异动记录（时间、规则、涨跌、板块 / 个股拆分）及当时的归因结论。"
            "读者问“最近有没有异动”“上次那次急跌是什么原因”时调用。",
            {"type": "object", "properties": {
                "days": {"type": "integer", "description": "回看天数，默认 5（1–30）"},
                "symbol": {"type": "string", "description": "只看某个代码；不填为全部"},
            }, "required": []},
        ))
        out.append(ToolSpec(
            "upcoming_events",
            "查询事件日历：持仓及相关公司的财报发布、内部人拟减持（Form 144）、锁定期到期；以及宏观日程——"
            "美联储议息会议 / 纪要 / 主席讲话、美国 CPI / 非农 / PCE / GDP 等数据发布、长端国债拍卖、日本央行议息、"
            "美股休市与期权到期等。标明是否为预计日期。读者问“什么时候发财报”“接下来有哪些事件 / 日程”"
            "“下次议息 / CPI 是哪天”时调用。",
            {"type": "object", "properties": {
                "days": {"type": "integer", "description": f"看未来多少天，默认 14（14–{self.settings.events.lookahead_days}）；"
                                                           "填了 symbol 时自动查到日历的收集范围，不必填"},
                "symbol": {"type": "string", "description": "只看某个代码的公司事件；不填为全部"},
                "category": {"type": "string", "enum": ["all", "macro", "company"],
                             "description": "macro 只看宏观日程，company 只看公司事件；默认 all"},
            }, "required": []},
        ))
        return out

    def session(self) -> "ToolSession":
        return ToolSession(self)

    # ───────── 各工具的实现（出错直接抛，由 ToolSession.execute 统一转成错误说明） ─────────

    async def get_macro(self, args: dict[str, Any]) -> str:
        assert self.macro is not None
        now = self.clock()
        snap = await self.macro.snapshot()
        tz = self.settings.tz
        lines = [f"宏观快照（{fmt_local(snap.at, tz, '%m-%d %H:%M')} {tz_abbr(snap.at, tz)}；括号内：不带标注的为较上一个美股收盘，"
                 "标 24h 的为较 24 小时前，1h 为近 1 小时；利率单位 %、变化单位 bp；利率与期货约延迟 10–15 分钟）："]
        if self.calendar is not None:
            lines.append(f"美股现在：{market_status(self.calendar, now, tz)}" +
                         ("；股指显示的是期货，口径为“较美股收盘”" if any(q.off_hours for q in snap.quotes) else ""))
        lines += macro_prompt_lines(snap, self.settings)
        if snap.errors:
            lines.append("取不到：" + "、".join(snap.errors))
        return "\n".join(lines)

    async def _daily(self, symbols: list[str]) -> dict:
        key = tuple(sorted(symbols))
        hit = self._daily_cache.get(key)
        if hit and time.monotonic() - hit[0] < DAILY_CACHE_SECONDS:
            return hit[1]
        assert self.prices is not None
        data = await self.prices.daily_bars(symbols, lookback_days=self.settings.monitor.price.beta_lookback_days + 10)
        self._daily_cache[key] = (time.monotonic(), data)
        return data

    async def get_intraday(self, args: dict[str, Any]) -> str:
        assert self.prices is not None and self.calendar is not None
        u, tz = self.settings.universe, self.settings.tz
        sym = self.resolve(_str_arg(args, "symbol"))
        if sym is None:
            raise ValueError("需要 symbol（美股代码或名称）")
        if not is_us_symbol(sym):
            raise ValueError(f"{sym} 不是美股代码；盘中拆分只支持美股")
        minutes = _int_arg(args, "minutes", 30, 1, 390)
        now = self.clock()
        session = self.calendar.session_at(now)
        live = session is not None
        if session is None:
            session = self.calendar.last_completed_sessions(now, 1)[0]
        # 基准和同业来自这只股票的主题；不在持仓 / 主题里的代码用兜底基准、对比全部持仓
        known = u.find(sym) is not None or bool(u.theme_keys_of(sym))
        bench = (u.benchmark_for(sym) if known else None) or u.benchmark
        peer_src = u.peers_for(sym) if known else [*u.symbols, *u.us_peers]
        peers = [s for s in dict.fromkeys(peer_src) if s != sym and s != bench and is_us_symbol(s)]
        members = u.basket_members(bench)
        fetch = list(dict.fromkeys([sym, *(members or [bench]), *peers]))
        bars = await self.prices.intraday_bars(fetch, session.open, now if live else session.close)
        try:
            daily = await self._daily(fetch)
        except SourceError as exc:  # 没有日线时退化为：没有昨收（只算窗口涨跌）、β 用默认值
            log.warning("盘中拆分取日线失败：%s", exc)
            daily = {}
        if members:  # 同业篮子：由成分算出来（detect/basket.py）
            bars, daily = add_baskets({bench: members}, intraday=bars, daily=daily, anchor=session.day)
        syms = list(dict.fromkeys([sym, bench, *peers]))
        frames = {s: session_frame(bars.get(s), session) for s in syms}
        views = {s: f.iloc[: f.attrs.get("n_obs", 0)] for s, f in frames.items()}
        if views[sym].empty:
            raise SourceError(f"没有取到 {sym} 在 {session.day:%m-%d} 的分钟数据")
        if not live:  # 已收盘：最后一根分钟线的收盘价不是官方收盘价（差约 0.02%），换成日线收盘
            views = {s: _with_official_close(v, daily.get(s), session.day) for s, v in views.items()}
        prev_close = {s: pc for s in syms if (pc := prev_close_from_daily(daily.get(s), session.day))}
        beta, fallback = estimate_beta(daily.get(sym), daily.get(bench), before=session.day,
                                       lookback=self.settings.monitor.price.beta_lookback_days)
        base = Baseline(sym, beta, fallback, None, None, None, 0)
        ctx = SessionContext(session=session, baselines={sym: base}, prev_close=prev_close, benchmark=bench)
        last_ts = views[sym].index[-1].to_pydatetime()
        price = float(views[sym]["close"].iloc[-1])

        def part(w: int | None) -> tuple[dict[str, Any], dict[str, float | None]]:
            sc = split_context(sym, w, views, base, ctx)
            if sc.move_pct is None:
                return {"note": "数据不足" if w else "缺少昨收"}, {}
            d: dict[str, Any] = {"move_pct": _num(sc.move_pct)}
            if sc.benchmark_move_pct is not None and sc.abnormal_move_pct is not None:
                proxy = Alert(symbol=sym, ts=last_ts, price=price, prev_close=prev_close.get(sym), triggers=[],
                              window_minutes=w, move_pct=sc.move_pct, benchmark_move_pct=sc.benchmark_move_pct,
                              beta=beta, abnormal_move_pct=sc.abnormal_move_pct, volume_ratio=None)
                d.update(benchmark_pct=_num(sc.benchmark_move_pct), sector_pct=_num(beta * sc.benchmark_move_pct),
                         idio_pct=_num(sc.abnormal_move_pct))
                if judgment := sector_judgment(proxy):
                    d["judgment"] = judgment
            return d, {p: _num(sc.peers_move_pct[p]) for p in peers if p in sc.peers_move_pct}

        day_part, day_peers = part(None)
        win_part, win_peers = part(minutes)
        if live:
            status = "盘中"
        elif self.calendar.exchange_date(now) == session.day:
            status = "今天已收盘"
        else:
            status = f"美股现在{market_status(self.calendar, now, tz)}；以下是最近一个交易日的全天数据"
        out: dict[str, Any] = {
            "symbol": sym, "date": session.day.isoformat(), "status": status,
            "last_bar": f"{fmt_local(last_ts, tz, '%H:%M')} {tz_abbr(last_ts, tz)}",
            "price": _num(price), "prev_close": _num(prev_close.get(sym)),
            "benchmark": u.label(bench), "beta": _num(beta) if not fallback else None,
            "vs_prev_close": day_part, f"last_{minutes}m": win_part,
            "peers_vs_prev_close": day_peers, f"peers_last_{minutes}m": win_peers,
            "note": ("" if live else f"last_{minutes}m 是 {session.day:%m-%d} 收盘前最后 {minutes} 分钟，不是现在。")
                    + f"sector_pct = β × {bench} 涨跌；idio_pct = move_pct − sector_pct（个股特异）。"
                    f"β 为前 {self.settings.monitor.price.beta_lookback_days} 个交易日日收益对 {bench} 的回归"
                    + ("；β 数据不足，按 1.0 计算" if fallback else ""),
        }
        return _dump(out)

    async def collect_news(self, query: str, hours: int) -> tuple[str, list[NewsItem], dict[str, str]]:
        """返回（检索说明, 条目, 失败的来源）。持仓 / 已知代码走 NewsHub.collect（预设的多语言检索词 + 公告），
        其他关键词只交给支持关键词检索的来源（Google News），按查询语言选版本。"""
        assert self.news is not None
        now = self.clock()
        start = now - timedelta(hours=hours)
        u = self.settings.universe
        sym = self.resolve(query, allow_unknown=False)
        sym = u.underlying_map.get(sym, sym) if sym else None
        if sym and is_us_symbol(sym):
            items = await self.news.collect([sym], start, now, include_sector=False)
            preset = "已含预设的多语言检索词与公告" if sym in u.symbols else "按代码检索，含公告"
            return f"{sym}（{self.name_of(sym) or sym}；{preset}）", items, dict(self.news.last_errors)
        text = (self.name_of(sym) if sym else "") or query.strip()
        lang = detect_lang(text)
        editions = self.settings.sources.news.google_news_rss.editions
        if lang not in editions:
            lang = "en"
        q = NewsQuery(start=start, end=now, symbols=(), queries={SECTOR: {lang: [text]}}, langs=(lang,))
        results = await asyncio.gather(*(src.fetch(q) for src in self.news.sources), return_exceptions=True)
        items: list[NewsItem] = []
        errors: dict[str, str] = {}
        for src, res in zip(self.news.sources, results):
            if isinstance(res, BaseException):
                errors[src.name] = f"{type(res).__name__}: {res}"
                continue
            items.extend(i for i in res if start <= i.published_at <= now)
        return f"“{text}”", dedupe(items), errors


class ToolSession:
    """一次提问的工具调用上下文：执行工具、记录新闻编号、把回答里的 [n3] 换成链接。"""

    def __init__(self, tools: ChatTools):
        self.tools = tools
        self.refs: dict[str, NewsItem] = {}
        self._by_key: dict[str, str] = {}

    async def execute(self, name: str, args: dict[str, Any]) -> ToolOutput:
        handlers: dict[str, Callable[[dict[str, Any]], Awaitable[str]]] = {
            "get_macro": self.tools.get_macro, "get_intraday": self.tools.get_intraday,
            "search_news": self.search_news, "recent_alerts": self.recent_alerts,
            "upcoming_events": self.upcoming_events,
        }
        offered = {s.name for s in self.tools.specs()}
        if name not in handlers or name not in offered:
            return ToolOutput(f"错误：没有可用的工具 {name!r}（可用：{', '.join(sorted(offered))}）", True)
        try:
            return ToolOutput(await asyncio.wait_for(handlers[name](args or {}), TOOL_TIMEOUT))
        except ValueError as exc:
            return ToolOutput(f"参数错误：{exc}", True)
        except SourceError as exc:
            return ToolOutput(f"数据源失败：{truncate(str(exc), 300)}", True)
        except asyncio.TimeoutError:
            return ToolOutput(f"查询超时（超过 {TOOL_TIMEOUT:.0f} 秒），数据源可能暂时不可用", True)
        except Exception as exc:  # 任何意外都转成说明交给模型，不打断对话
            log.exception("对话工具 %s 出错", name)
            return ToolOutput(f"查询出错：{type(exc).__name__}: {truncate(str(exc), 200)}", True)

    def _ref(self, item: NewsItem) -> str:
        rid = self._by_key.get(item.key)
        if rid is None:
            rid = f"n{len(self.refs) + 1}"
            self.refs[rid] = item
            self._by_key[item.key] = rid
        return rid

    def link_refs(self, text: str) -> str:
        """把回答里的 [n3] / 【n3】 换成 [来源](链接)。这次提问里没有出现过的编号直接去掉——
        实测模型会沿用上一轮对话里的编号，而编号每次提问重新开始，留着只会指错或显示成乱码。"""

        # 模型自己写的网址一律去掉（只保留链接文字）：链接只能来自工具结果，提示词之外再加一道保险
        text = _MD_URL_LINK.sub(lambda m: m.group(1), text)
        text = _BARE_URL.sub("", text)

        def sub(m: re.Match) -> str:
            items = [self.refs.get(r.strip().lower()) for r in re.split(r"[,，、]", m.group(1))]
            known = [link(short_source(it), it.url) for it in items if it is not None]
            return "、".join(known) if known else "\x00"

        marked = _REF.sub(sub, text)
        marked = re.sub(r"(\]\(https?://[^)\s]+\))(?=\[[^\]]+\]\(https?://)", r"\1、", marked)  # 相邻的 [n3][n6]：链接之间加顿号，否则读起来像一个链接

        def drop(m: re.Match) -> str:  # 去掉的编号连同两侧空格一起去掉；只有两侧都是英文单词时留一个空格
            before = marked[m.start() - 1] if m.start() > 0 else ""
            after = marked[m.end()] if m.end() < len(marked) else ""
            ascii_word = lambda c: c.isascii() and c.isalnum()  # noqa: E731
            return " " if m.group(1) and m.group(2) and ascii_word(before) and ascii_word(after) else ""

        return re.sub(r"([ \t]*)\x00([ \t]*)", drop, marked)

    async def search_news(self, args: dict[str, Any]) -> str:
        query = _str_arg(args, "query") or _str_arg(args, "symbol")
        if not query:
            raise ValueError("需要 query（代码、公司名或关键词）")
        hours = _int_arg(args, "hours", 24, 1, 168)
        label, items, errors = await self.tools.collect_news(query, hours)
        tz = self.tools.settings.tz
        now = self.tools.clock()
        failed = f"（部分来源失败：{', '.join(errors)}）" if errors else ""
        if not items:
            if errors and len(errors) >= len(self.tools.news.sources if self.tools.news else []):
                raise SourceError("新闻源全部失败：" + "；".join(f"{k}: {truncate(v, 80)}" for k, v in errors.items()))
            return f"最近 {hours} 小时没有找到关于 {label} 的新闻或公告。{failed}"
        items = sorted(items, key=lambda i: i.published_at, reverse=True)
        # 公告单独留名额：新闻一天上百条，只取最新 10 条时 SEC 公告（Form 4、8-K）几乎总被挤掉（审查实测排第 89）
        filings = [i for i in items if i.kind == "filing"][:MAX_FILINGS]
        news = [i for i in items if i.kind != "filing"][: MAX_NEWS - len(filings)]
        shown = sorted([*filings, *news], key=lambda i: i.published_at, reverse=True)
        n_filings = sum(1 for i in items if i.kind == "filing")
        head = (f"关于 {label} 的新闻与公告（最近 {hours} 小时共 {len(items)} 条，其中 SEC 公告 {n_filings} 条；"
                f"显示最新 {len(shown)} 条，公告优先；时间为 {tz_abbr(now, tz)}）{failed}：")
        lines = [head]
        for it in shown:
            src = "SEC公告" if it.kind == "filing" else short_source(it)
            lines.append(f"[{self._ref(it)}] {fmt_local(it.published_at, tz, '%m-%d %H:%M')}｜{src}｜{truncate(it.title, 110)}")
        lines.append("引用时只写编号，如 [n1]。")
        return "\n".join(lines)

    async def recent_alerts(self, args: dict[str, Any]) -> str:
        t = self.tools
        tz, u = t.settings.tz, t.settings.universe
        days = _int_arg(args, "days", 5, 1, 30)
        raw = _str_arg(args, "symbol")
        sym = t.resolve(raw) if raw else None
        now = t.clock()
        alerts = t.storage.alerts_between(now - timedelta(days=days), now + timedelta(minutes=1))
        if sym:
            # 异动按实际检测的代码入库：问持仓时把它的主上市代码、对应它的夜间信号和夜间篮子一起带上；
            # 问首尔 000660.KS 时就查它自己（不换成 SKHY）
            wanted = {sym, *[k for k, v in u.underlying_map.items() if v == sym],
                      *[i.symbol for i in u.monitored if sym in (i.related_to or [])]}
            alerts = [a for a in alerts if a.symbol in wanted]
        scope = f"{sym} " if sym else ""
        if not alerts:
            return f"最近 {days} 天没有 {scope}异动记录（系统只在涨跌超过阈值时推送；没有记录说明没有触发）。"
        shown = alerts[-MAX_ALERTS:][::-1]
        lines = [f"最近 {days} 天的 {scope}异动记录（共 {len(alerts)} 条，显示最近 {len(shown)} 条，新的在前；"
                 f"时间为 {tz_abbr(now, tz)}）："]
        for a in shown:
            line = f"- {fmt_local(a.ts, tz, '%m-%d %H:%M')} {u.display(a.symbol)}{'【' + a.segment_label + '】' if a.segment_label else ''} {a.headline.describe(a.ref_label)}"
            if a.day_change_pct is not None:
                line += f"；现价 {_num(a.price, _price_digits(a.symbol))}，{a.ref_label} {a.day_change_pct:+.1f}%"
            if a.move_pct is not None and a.benchmark_move_pct is not None and a.beta is not None:
                w = f"{a.window_minutes}分钟" if a.window_minutes else a.ref_label
                line += (f"｜{w}拆分：板块 {a.beta * a.benchmark_move_pct:+.1f}%"
                         f"（{u.label(alert_benchmark(a, t.settings) or '')} {a.benchmark_move_pct:+.1f}% × β{a.beta:.2f}）")
                if a.abnormal_move_pct is not None:
                    line += f"·个股特异 {a.abnormal_move_pct:+.1f}%"
            summary = t.storage.attribution_summary(a.id) if a.id is not None else None
            line += f"｜归因：{truncate(summary, 120)}" if summary else "｜归因：无记录"
            lines.append(line)
        return "\n".join(lines)

    async def upcoming_events(self, args: dict[str, Any]) -> str:
        t = self.tools
        tz, u = t.settings.tz, t.settings.universe
        # 至少看 14 天：模型常把“下周”理解成 7 天，漏掉下周后半段的事件（实测漏了 8 天后的 SNDK 财报）；多给的由模型自己筛。
        # 最多看到事件日历的收集范围（events.lookahead_days）：更远的日子库里本来就没有，说“没有事件”会误导模型去猜
        horizon = t.settings.events.lookahead_days
        days = min(max(14, _int_arg(args, "days", 14, 1, horizon)), horizon)
        raw = _str_arg(args, "symbol")
        sym = t.resolve(raw) if raw else None
        sym = u.underlying_map.get(sym, sym) if sym else None
        if sym:  # 只看一家公司时条目很少：直接查到收集范围，“下次财报哪天”不会因为模型填了 60 天而落空（审查后实测）
            days = horizon
        notes: list[str] = []
        if raw and sym is None:
            notes.append(f"没认出“{raw}”对应的代码，下面列的是全部事件。")
        tracked = {*u.event_symbols, *t.settings.events.related, *(m.symbol for m in t.settings.events.manual)}
        if sym and sym not in tracked:  # 不在收集范围的代码，“没有事件”不代表它真的没有财报
            notes.append(f"{sym} 不在事件日历的收集范围（只收集持仓、它们主题里的同业和相关公司、events.related 和手工录入），"
                         "不能据此判断它没有财报。")
        now = t.clock()
        today = now.astimezone(tz).date()
        start, end = today - timedelta(days=1), today + timedelta(days=days + 1)
        evs = t.events.upcoming(start, end) if t.events is not None else t.storage.events_between(start, end)
        evs = [e for e in evs if today <= local_day(e, tz) <= today + timedelta(days=days)]
        category = _str_arg(args, "category").lower() or "all"
        if category not in ("all", "macro", "company"):
            raise ValueError(f"category 只能是 all / macro / company：{category!r}")
        if sym:
            evs = [e for e in evs if e.symbol == sym and e.kind != "macro"]
        elif category == "macro":
            evs = [e for e in evs if e.kind == "macro"]
        elif category == "company":
            evs = [e for e in evs if e.kind != "macro"]
        last = t.events.last_refresh() if t.events is not None else None
        refreshed = (f"日历更新于 {fmt_local(last, tz, '%m-%d %H:%M')}" if last is not None
                     else "事件日历还没有刷新过，可能不完整" if t.events is not None else "")
        scope = f"{sym} 的" if sym else {"macro": "宏观", "company": "公司"}.get(category, "")
        coverage = (f"日历只收集到 {today + timedelta(days=horizon):%m-%d}（未来 {horizon} 天），"
                    "更远的事件还没收集，不要推测日期")
        if not evs:
            more = (f"\n{coverage}。" if days >= horizon
                    else f"\n只查了 {days} 天；日历收集到 {today + timedelta(days=horizon):%m-%d}，需要时可把 days 调到 {horizon}。")
            return ("".join(notes) + f"未来 {days} 天没有已知的{scope}事件。" + (f"（{refreshed}）" if refreshed else "")
                    + more)
        shown = evs[:MAX_EVENTS]
        holdings = set(u.symbols)
        cut = f"，只列最早的 {len(shown)} 条" if len(evs) > len(shown) else ""
        head = ("".join(notes) + f"未来 {days} 天（{today:%m-%d} 起）{scope}事件，共 {len(evs)} 条{cut}；时间为 {tz_abbr(now, tz)}"
                + (f"；{refreshed}" if refreshed else "") + "。每行开头是代码（宏观日程写“宏观”），标“持仓”的是读者的持仓：")
        # 每行带代码和持仓标记：只有标题时模型分不清谁是持仓（实测把 SK海力士当成同业）
        lines = [f"- {'宏观' if e.kind == 'macro' else e.symbol}{'（持仓）' if e.symbol in holdings and e.kind != 'macro' else ''}"
                 f" {line[2:]}" for e, line in zip(shown, events_prompt_lines(shown, tz))]
        tail = [coverage + "。"] if days >= horizon else []
        return "\n".join([head, *lines, *tail])
