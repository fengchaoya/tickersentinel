"""监控范围的文字视图：tsen scope 与飞书 /scope 共用。"""

from __future__ import annotations

from tickersentinel.config import ASSET_CLASS_NAMES, BASKET_PREFIX, DEFAULT_THEME, EXCHANGE_NAMES, Settings, Theme, exchange_of


def _named(u, codes) -> str:
    parts = []
    for c in codes:
        name = u.label(c)
        parts.append(c if name == c else f"{name} {c}")
    return "、".join(parts)


def _rules_text(settings: Settings, symbol: str) -> str:
    r = settings.rules_for(symbol)
    rapid = "、".join(f"{w}分钟 {r.min_move_pct[w]:g}%" for w in r.windows_minutes)
    day = "/".join(f"{x:g}" for x in r.day_pct)
    return f"急涨急跌（从近期高 / 低点算）{rapid}（z≥{r.z_threshold:g}）；全天 {day}%"


def theme_lines(settings: Settings, th: Theme, *, active: bool = True) -> list[str]:
    u = settings.universe
    holders = u.theme_holdings(th.key)
    head = f"**主题「{th.name}」**（{th.key}" + (f"，持仓 {'、'.join(holders)}" if holders else "") + "）"
    lines = [head]
    if th.focus and th.focus != th.name:
        lines.append(f"- 领域：{th.focus}")
    if th.basket:
        lines.append(f"- 板块基准：同业等权篮子（{'、'.join(th.basket_members)}）")
    else:
        lines.append(f"- 板块基准：{u.label(th.benchmark)}" + ("" if u.label(th.benchmark) == th.benchmark else f" {th.benchmark}"))
    if th.peers:
        lines.append(f"- 同业（同期对比、事件）：{_named(u, th.peers)}")
    if th.overnight:
        rows = []
        for code, cfg in th.overnight.items():
            ex = EXCHANGE_NAMES.get(exchange_of(code) or "", "")
            rows.append(f"{cfg.name or code} {code}" + (f"（{ex}时段）" if ex else ""))
        lines.append(f"- 夜间信号（只盯盘，异动时推送）：{'、'.join(rows)}")
        baskets = [(code, ms) for code, (t, _, ms) in u.night_baskets.items() if t.key == th.key]
        for code, ms in baskets:
            alert = th.overnight_alert
            lv = ("/".join(f"{x:g}" for x in alert.day_pct) + "%") if alert and alert.day_pct else "海外股票默认"
            lines.append(f"  - 按篮子触发：{u.label(code)} = {'、'.join(u.label(m) for m in ms)} 等权，一波行情只推一条"
                         f"（全天 {lv}）；某一只自己涨跌 ≥{th.overnight_single_pct:g}%、且相对篮子也偏离这么多时单独推")
    if th.catalysts:
        names = [v or k for k, v in th.catalysts.items()]
        lines.append(f"- 相关公司（财报进事件日历、归因时对照）：{'、'.join(names)}（{len(names)} 家）")
    if th.news:
        lines.append("- 板块新闻检索词：" + "；".join(f"{lang} {'、'.join(qs)}" for lang, qs in th.news.items() if qs))
    if th.indicators:
        lines.append(f"- 板块指标（附在这个板块的异动推送里）：{'、'.join(i.name for i in th.indicators)}")
    if th.note:
        lines.append(f"- 背景：{th.note}")
    return lines


def describe_scope(settings: Settings) -> str:
    u = settings.universe
    active = u.active_themes
    real = [t for t in active if t.key != DEFAULT_THEME]
    lines = [f"持仓 {len(u.holdings)} 只，生效主题 {len(real)} 个"
             + ("（另有持仓没写 theme，归入默认主题）" if any(t.key == DEFAULT_THEME for t in active) else "") + "。",
             "", "**持仓**"]
    for inst in u.holdings:
        themes = "、".join(u.themes[k].name for k in u.theme_keys_of(inst.symbol))
        cls = ASSET_CLASS_NAMES.get(inst.asset_class, inst.asset_class)
        hours = ""
        if inst.asset_class == "us_equity":
            hours = "，盘前 + 盘中 + 盘后" if settings.hours_for(inst.symbol) == "extended" else "，只盯常规时段"
        mon = "" if inst.monitor else "，不做异动检测"
        lines.append(f"- {inst.symbol} {inst.name}（{cls}{hours}{mon}）｜主题：{themes}｜{_rules_text(settings, inst.symbol)}")
    extra = [i for i in u.watchlist if not i.holding]
    if extra:
        lines += ["", "**只盯盘的资产（assets.yaml）**"]
        lines += [f"- {i.symbol} {i.name}（对应 {'、'.join(i.related_to) or '—'}）｜{_rules_text(settings, i.symbol)}" for i in extra]
    for th in active:
        lines += ["", *theme_lines(settings, th)]
    spare = [t for k, t in u.themes.items() if k != DEFAULT_THEME and t not in active]
    if spare:
        lines += ["", "备用主题（没有持仓引用，不生效）：" + "、".join(f"{t.name}（{t.key}）" for t in spare)]
    if settings.events.related:
        lines += ["", f"另外收集财报的公司（settings.yaml 的 events.related）：{'、'.join(v or k for k, v in settings.events.related.items())}"]
    m = settings.macro
    lines += ["", "**基础宏观变量**（settings.yaml 的 macro，和具体股票无关）："
              + ("、".join(i.name for i in m.indicators) if m.enabled else "关闭")]
    mc = settings.events.macro
    if mc.enabled:
        parts = []
        if mc.fed:
            parts.append("美联储")
        if mc.us_data:
            parts.append("美国数据（" + "、".join(mc.us_data) + "）")
        if mc.treasury_auctions:
            parts.append("国债拍卖（" + "、".join(mc.treasury_auctions) + "）")
        parts += [n for flag, n in ((mc.boj, "日本央行"), (mc.korea_exports, "韩国出口"), (mc.opex, "期权到期"),
                                    (mc.us_market, "美股休市")) if flag]
        lines.append("**宏观日程**（settings.yaml 的 events.macro）：" + "、".join(parts))
    lines += ["", "改范围：config/assets.yaml（持仓、灵敏度）与 config/themes/（板块联动）；正在运行的 tsen run 一分钟内自动读到，不用重启（只有 settings.yaml 的改动要重启）。"]
    return "\n".join(lines)


__all__ = ["describe_scope", "theme_lines", "BASKET_PREFIX"]
