"""推送排版：生成与平台无关的轻量 Markdown（**粗体**、- 列表、[文字](链接)），各推送渠道再转换成自己的格式。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from tickersentinel.config import NIGHT_PREFIX, Settings, is_pseudo
from tickersentinel.models import Alert, Message, NewsItem
from tickersentinel.textutil import truncate
from tickersentinel.timeutil import fmt_local, tz_abbr

if TYPE_CHECKING:
    from tickersentinel.sources.macro import MacroQuote, MacroSnapshot


def pct(x: float | None, digits: int = 1) -> str:
    return "n/a" if x is None else f"{x:+.{digits}f}%"


def fmt_price(symbol: str, x: float | None) -> str:
    if x is None:
        return "n/a"
    if is_pseudo(symbol):  # 篮子：等权点位，昨收 = 100
        return f"{x:,.2f}"
    if symbol.endswith((".KS", ".KQ")):
        return f"₩{x:,.0f}"
    if symbol.endswith(".T"):
        return f"¥{x:,.0f}"
    if symbol.endswith("=X"):
        return f"{x:,.2f}"
    return f"${x:,.2f}"


def arrow(direction: int) -> str:
    return "🔺" if direction > 0 else "🔻"


def link(text: str, url: str) -> str:
    text = text.replace("[", "(").replace("]", ")")
    return f"[{text}]({url})"


def short_source(item: NewsItem) -> str:
    return truncate(item.source or item.provider or "链接", 14)


def link_refs(text: str, items: list[NewsItem]) -> str:
    """把模型输出里的 [3] 替换成 [来源](链接)。不认识的编号保持原样。"""

    def sub(m: re.Match) -> str:
        i = int(m.group(1)) - 1
        if 0 <= i < len(items):
            return link(short_source(items[i]), items[i].url)
        return m.group(0)

    return re.sub(r"\[(\d{1,3})\](?!\()", sub, text)


def sector_judgment(alert: Alert) -> str | None:
    move, abn = alert.move_pct, alert.abnormal_move_pct
    if move is None or abn is None or alert.beta is None or alert.benchmark_move_pct is None or abs(move) < 1e-9:
        return None
    sector = alert.beta * alert.benchmark_move_pct
    if abs(abn) <= 0.35 * abs(move):
        return "以板块联动为主"
    if abs(sector) <= 0.35 * abs(move) or sector * move < 0:
        return "以个股因素为主"
    return "板块与个股因素兼有"


def alert_benchmark(alert: Alert, settings: Settings) -> str | None:
    """这条异动拆分用的基准：入库时记下的（旧记录没有，按现在的主题推）。"""
    return alert.benchmark or settings.universe.benchmark_for(alert.symbol)


def _peer_order(settings: Settings, symbol: str, benchmark: str | None) -> list[str]:
    """同期对比的顺序：同主题的其他持仓 → 同业 → 基准。"""
    u = settings.universe
    return list(dict.fromkeys([*u.peers_for(symbol), *([benchmark] if benchmark else [])]))


def related_text(settings: Settings, symbol: str) -> str | None:
    """只盯盘资产的说明：“这是 MU、SNDK、SKHY 的夜间信号（存储芯片）”。"""
    u = settings.universe
    inst = u.find(symbol)
    if inst is None or inst.holding or not inst.related_to:
        return None
    theme = u.theme_of(symbol)
    who = "、".join(inst.related_to)
    tname = f"（{theme.name}）" if theme.key != "default" and len(u.active_themes) > 1 else ""
    return f"这是 {who} 的夜间信号{tname}（{u.label(symbol)}），下一个美股时段可能跟随"


def alert_name(alert: Alert, settings: Settings, *, with_segment: bool = True) -> str:
    """推送里这只资产的叫法：MU、MU【盘前】、SK海力士(首尔) 的 000660.KS【SK海力士(首尔)】、夜间篮子的名称。"""
    u = settings.universe
    if alert.symbol.startswith(NIGHT_PREFIX):  # 夜间篮子：直接写篮子的名称
        return u.label(alert.symbol)
    inst = u.find(alert.symbol)
    tag = (alert.segment_label if with_segment else "") or (u.label(alert.symbol) if inst is None or not inst.holding else "")
    return f"{alert.symbol}【{tag}】" if tag else alert.symbol


def split_line(alert: Alert, settings: Settings) -> str | None:
    """“拆分（34分钟）：板块 -2.7%（SOXX -1.7% × β1.60）｜个股特异 -0.4% → 以板块联动为主”。"""
    u = settings.universe
    if alert.move_pct is None or alert.benchmark_move_pct is None or alert.beta is None:
        return None
    wlabel = f"{alert.window_minutes}分钟" if alert.window_minutes else alert.ref_label
    bench = alert_benchmark(alert, settings)
    sector = alert.beta * alert.benchmark_move_pct
    judgment = sector_judgment(alert)
    return (f"拆分（{wlabel}）：板块 {pct(sector)}（{u.label(bench or '')} {pct(alert.benchmark_move_pct)} × β{alert.beta:.2f}）"
            f"｜个股特异 {pct(alert.abnormal_move_pct)}" + (f" → {judgment}" if judgment else ""))


def render_alert(alert: Alert, settings: Settings, *, attribution_pending: bool = True,
                 macro: "MacroSnapshot | None" = None) -> Message:
    u = settings.universe
    head = alert.headline
    night = alert.symbol.startswith(NIGHT_PREFIX)
    name = alert_name(alert, settings)
    title = f"{arrow(alert.direction)} {name} {head.short(alert.ref_label)}" + ("（扩大）" if alert.is_escalation else "")
    ref = alert.ref_label
    lines = [f"- {tr.describe(ref)}" for tr in alert.triggers]
    if night:
        members = "、".join(u.label(m) for m in u.basket_members(alert.symbol))
        lines.append(f"- 篮子（{members} 等权）{ref} {pct(alert.day_change_pct)}")
    else:
        lines.append(f"- 现价 {fmt_price(alert.symbol, alert.price)}，{ref} {pct(alert.day_change_pct)}")
    if (rel := related_text(settings, alert.symbol)) is not None:
        lines.append(f"- {rel}")

    wlabel = f"{alert.window_minutes}分钟" if alert.window_minutes else ref
    bench = alert_benchmark(alert, settings)
    if (sl := split_line(alert, settings)) is not None:
        lines.append(f"- {sl}")
    order = _peer_order(settings, alert.symbol, bench)
    order += [s for s in alert.peers_move_pct if s not in order]  # 旧记录、或主题之外的对比代码，排在最后
    peers = [f"{u.label(s)} {pct(alert.peers_move_pct[s])}" for s in order if s in alert.peers_move_pct]
    if peers:
        lines.append(f"- 同期（{wlabel}）：" + "｜".join(peers))
    if alert.volume_ratio is not None:
        lines.append(f"- 成交量：{wlabel}为历史同时段的 {alert.volume_ratio:.1f} 倍")
    lines.append(f"- 时间：{fmt_local(alert.ts, settings.tz, '%H:%M')} {tz_abbr(alert.ts, settings.tz)}")
    if macro is not None:
        lines += render_macro_compact(macro, settings)
        lines += render_theme_indicators(macro, settings, u.theme_keys_of(alert.symbol))
    if attribution_pending:
        lines.append("\n⏳ 正在检索可能原因…")
    return Message(title=title, body="\n".join(lines), kind="alert")


def render_alert_group(alerts: list[Alert], settings: Settings, *, theme: str, macro: "MacroSnapshot | None" = None,
                       attribution_pending: bool = True, follow_of: "datetime | None" = None) -> Message:
    """同一主题、同方向的几条异动合成一条推送；follow_of 是前一条的时间时，这是挂在那一条下面的回复（不再附宏观快照）。"""
    u = settings.universe
    th = u.themes.get(theme)
    tname = th.name if th is not None else theme
    up = alerts[0].direction > 0
    segs = {a.segment_label for a in alerts}
    seg = f"【{segs.pop()}】" if len(segs) == 1 and next(iter(segs)) else ""
    names = "、".join(alert_name(a, settings, with_segment=False) for a in alerts)
    tz = settings.tz
    if follow_of is not None:
        verb = "也" + ("涨" if up else "跌")
        if len(alerts) == 1:
            a = alerts[0]
            title = (f"{arrow(a.direction)} {alert_name(a, settings)} {verb}了：{a.headline.short(a.ref_label)}"
                     + ("（扩大）" if a.is_escalation else ""))
        else:
            title = f"{arrow(alerts[0].direction)} {tname}{seg} {verb}了 {len(alerts)} 只：{names}"
    else:
        title = f"{arrow(alerts[0].direction)} {tname}{seg} {len(alerts)} 只一起{'涨' if up else '跌'}：{names}"
    lines = []
    for a in alerts:
        price = (f"篮子 {a.ref_label} {pct(a.day_change_pct)}" if a.symbol.startswith(NIGHT_PREFIX)
                 else f"现价 {fmt_price(a.symbol, a.price)}")
        lines.append(f"- **{alert_name(a, settings, with_segment=False)}** "
                     + "；".join(tr.describe(a.ref_label) for tr in a.triggers)
                     + ("（扩大）" if a.is_escalation else "") + f"；{price}")
        if (sl := split_line(a, settings)) is not None:
            lines.append(f"  {sl}")
    rel = next((r for a in alerts if (r := related_text(settings, a.symbol)) is not None), None)
    if rel is not None:
        lines.append(f"- {rel}")
    lines.append(f"- 时间：{fmt_local(alerts[0].ts, tz, '%H:%M')} {tz_abbr(alerts[0].ts, tz)}")
    if follow_of is not None:
        lines.append(f"- 和 {fmt_local(follow_of, tz, '%H:%M')} 那一条是同一波（{tname}同方向），宏观快照见那一条")
    elif macro is not None:
        lines += render_macro_compact(macro, settings)
        lines += render_theme_indicators(macro, settings, [theme])
    if attribution_pending:
        lines.append("\n⏳ 正在检索可能原因…")
    return Message(title=title, body="\n".join(lines), kind="alert")


def minutes_from(alert_ts: datetime, item_ts: datetime) -> int:
    return round((item_ts - alert_ts).total_seconds() / 60)


def alert_start(alert: Alert) -> datetime | None:
    """这波涨跌开始的时刻（日内高点 / 低点）：急涨急跌、从日内高低点算的全天累计才有；从昨收算的为 None。"""
    t = alert.headline.anchor_ts
    return t if t is not None and t < alert.ts else None


def rel_to_alert(alert: Alert, item_ts: datetime) -> str:
    """条目相对这条异动的时间：有起点时同时写“起点前后”和“检测前后”，原因应当出现在起点之前或几乎同时。"""
    start = alert_start(alert)
    if start is None:
        return rel_time(alert.ts, item_ts)
    return f"{rel_time(start, item_ts).replace('异动', '起点')}、{rel_time(alert.ts, item_ts).replace('异动', '检测')}"


def rel_time(alert_ts: datetime, item_ts: datetime) -> str:
    d = minutes_from(alert_ts, item_ts)
    if d < 0:
        return f"异动前{-d}分钟" if -d < 180 else f"异动前{-d / 60:.0f}小时"
    return "与异动同时" if d == 0 else f"异动后{d}分钟"


def render_attribution(alert: Alert, data: dict, candidates: list[NewsItem], citations: list[dict], settings: Settings) -> str:
    conf_icon = {"高": "🟢", "中": "🟡", "低": "⚪"}
    lines = [f"**结论**：{data.get('summary', '').strip()}（主导：{data.get('driver', '不明')}）"]
    cite_titles = {c["url"]: c.get("title") or "" for c in citations}
    for i, c in enumerate((data.get("causes") or [])[:3], 1):
        refs = []
        for ev in c.get("evidence") or []:
            ev = str(ev).strip()
            m = re.fullmatch(r"#?(\d{1,3})", ev)
            if m and 0 < int(m.group(1)) <= len(candidates):
                it = candidates[int(m.group(1)) - 1]
                refs.append(link(short_source(it), it.url))
            elif ev.startswith("http"):
                refs.append(link(truncate(cite_titles.get(ev) or "检索结果", 14), ev))
            elif ev.startswith("日程"):  # 事件日历里的条目（经济数据发布、议息、同业财报……）
                refs.append(truncate(ev.replace("：", " ", 1).replace(":", " ", 1), 24))
        timing = f"（{c['timing']}）" if c.get("timing") else ""
        src = f" 来源：{' '.join(refs)}" if refs else ""
        lines.append(f"{i}. {conf_icon.get(c.get('confidence', ''), '⚪')}[{c.get('confidence', '?')}] {c.get('cause', '').strip()}{timing}{src}")
    if data.get("watch"):
        lines.append(f"**关注**：{data['watch'].strip()}")
    return "\n".join(lines)


def render_attribution_fallback(alert: Alert, candidates: list[NewsItem], settings: Settings, reason: str) -> str:
    lines = [f"（{reason}，以下为异动前后的相关新闻，按时间接近程度排列）"]
    judgment = sector_judgment(alert)
    if judgment:
        lines.append(f"- 初步判断：{judgment}（基于板块拆分）")
    if not candidates:
        lines.append("- 时间窗口内没有找到相关新闻或公告：可能是板块传导、资金面或尚未见报的消息。")
    for it in sorted(candidates, key=lambda x: abs(minutes_from(alert.ts, x.published_at)))[:6]:
        lines.append(f"- {fmt_local(it.published_at, settings.tz, '%H:%M')}（{rel_to_alert(alert, it.published_at)}）"
                     f"{link(truncate(it.title, 60), it.url)} — {short_source(it)}")
    return "\n".join(lines)


# ───────── 宏观快照（sources/macro.py） ─────────

MACRO_GROUP_ORDER = ["利率", "大宗商品", "股指", "其他"]


def _macro_item(q: "MacroQuote") -> str:
    parts = [q.fmt_change(q.change) if q.change_label == "较昨收" else f"{q.change_label} {q.fmt_change(q.change)}"]
    if q.change_1h is not None:
        parts.append(f"1h {q.fmt_change(q.change_1h)}")
    return f"{q.name} {q.fmt_value()}（{'，'.join(parts)}）"


def _macro_groups(quotes: list["MacroQuote"]) -> list[tuple[str, list["MacroQuote"]]]:
    groups: dict[str, list] = {}
    for q in quotes:
        groups.setdefault(q.group, []).append(q)
    order = MACRO_GROUP_ORDER + [g for g in groups if g not in MACRO_GROUP_ORDER]
    return [(g, groups[g]) for g in order if g in groups]


def _curve_line(snap: "MacroSnapshot") -> str | None:
    c = snap.curve
    if c is None:
        return None
    items = [f"{k} {c.yields[k]:.2f}%" for k in ("2Y", "10Y", "30Y") if k in c.yields]
    if c.spread_2s10s_bp is not None:
        items.append(f"2s10s {c.spread_2s10s_bp:+.0f}bp")
    return f"财政部官方收盘（{c.day:%m-%d}）：" + "｜".join(items)


def render_macro_compact(snap: "MacroSnapshot", settings: Settings) -> list[str]:
    """附在异动推送末尾的宏观快照（只有基础宏观变量；板块指标见 render_theme_indicators）：每组一行。
    括号里默认是较昨收，标“24h”的是较 24 小时前。"""
    keys = {i.key for i in settings.macro.indicators if i.in_alert}
    quotes = [q for q in snap.quotes if q.theme is None and q.key in keys]
    if not quotes:
        return []
    lines = ["", f"🌐 **宏观**（{fmt_local(snap.at, settings.tz, '%H:%M')}；括号内为较昨收，标 24h 的为较 24 小时前，"
                 "1h 为近 1 小时；利率与期货约延迟 10–15 分钟）"]
    lines += [f"- {'｜'.join(_macro_item(q) for q in qs)}" for _, qs in _macro_groups(quotes)]
    if (curve := _curve_line(snap)) is not None:
        lines.append(f"- {curve}")
    return lines


def theme_quotes(snap: "MacroSnapshot", themes: list[str] | None) -> list["MacroQuote"]:
    """快照里属于这些主题的板块指标（themes 为 None 时取全部板块指标）。"""
    return [q for q in snap.quotes if q.theme is not None and (themes is None or q.theme in themes)]


def render_theme_indicators(snap: "MacroSnapshot", settings: Settings, themes: list[str]) -> list[str]:
    """异动推送里这只股票所属主题的板块指标（config/themes/ 的 indicators），每个主题一行。"""
    lines: list[str] = []
    for key in themes:
        qs = theme_quotes(snap, [key])
        th = settings.universe.themes.get(key)
        if qs and th is not None:
            lines.append(f"- 📌 {th.name}：" + "｜".join(_macro_item(q) for q in qs))
    return lines


def macro_prompt_lines(snap: "MacroSnapshot", settings: Settings, themes: list[str] | None = None) -> list[str]:
    """给大模型的纯文本宏观行（归因、对话工具用）：基础宏观变量，再加上指定主题的板块指标（themes 为 None 时全部）。"""
    base = [q for q in snap.quotes if q.theme is None]
    lines = [f"- {group}：" + "；".join(_macro_item(q) for q in qs) for group, qs in _macro_groups(base)]
    if (curve := _curve_line(snap)) is not None:
        lines.append(f"- {curve}")
    u = settings.universe
    for key in dict.fromkeys(q.theme for q in theme_quotes(snap, themes)):
        th = u.themes.get(key)
        if th is not None:
            lines.append(f"- 板块指标（{th.name}）：" + "；".join(_macro_item(q) for q in theme_quotes(snap, [key])))
    return lines


def render_macro(snap: "MacroSnapshot", settings: Settings) -> Message:
    """/macro 命令与 tsen macro：全部变量，每个一行。"""
    tz = settings.tz
    lines: list[str] = []

    def row(q: "MacroQuote") -> str:
        when = fmt_local(q.ts, tz, "%H:%M") if snap.at - q.ts < timedelta(hours=20) else fmt_local(q.ts, tz, "%m-%d %H:%M")
        label = "较美股收盘" if q.off_hours else q.change_label
        hour = f"｜近1小时 {q.fmt_change(q.change_1h)}" if q.change_1h is not None else ""
        return f"- {q.name} {q.fmt_value()}｜{label} {q.fmt_change(q.change)}{hour}（{when}，{q.symbol}）"

    for group, qs in _macro_groups([q for q in snap.quotes if q.theme is None]):
        lines.append(f"**{group}**")
        lines += [row(q) for q in qs]
    if (curve := _curve_line(snap)) is not None:
        lines += ["**官方收益率**", f"- {curve}"]
    for key in dict.fromkeys(q.theme for q in theme_quotes(snap, None)):  # 板块指标：每个生效主题一段
        th = settings.universe.themes.get(key)
        lines.append(f"**板块指标 · {th.name if th else key}**")
        lines += [row(q) for q in theme_quotes(snap, [key])]
    notes = ["较昨收 = 较上一个美股交易日收盘；24h = 较 24 小时前（几乎全天交易的期货、外汇、比特币）",
             "利率（Cboe 收益率指数）和期货在 Yahoo 上约延迟 10–15 分钟"]
    if any(q.off_hours for q in snap.quotes):
        notes.append("美股休市时股指换成期货，口径为“较美股收盘”（收盘以来期货的变化）")
    if snap.errors:
        notes.append("取不到：" + "、".join(snap.errors))
    lines += ["", *(f"注：{n}" for n in notes)]
    title = f"🌐 宏观快照 · {fmt_local(snap.at, tz, '%m-%d %H:%M')} {tz_abbr(snap.at, tz)}"
    return Message(title=title, body="\n".join(lines), kind="macro")
