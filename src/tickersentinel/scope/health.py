"""联动体检：主题里的联动关系会随行情阶段变化（2026-10 的数字来自 AI 行情的一年），定期重算一遍。
周报末尾附一段（只有发现问题时才附），tsen scope check、飞书 /scope check 随时查。只给建议，不自动改。

对每个生效主题：
- 同业：每个美股持仓 × 每个美股同业，剔除大盘后的残差相关（近 120 个交易日），平均低于 WEAK_PEER 提示“联动变弱”；
- 夜间信号：对主题里每个美股持仓次日开盘跳空的偏相关，最高也低于 WEAK_NIGHT 提示“信号变弱”；
- 板块基准：持仓对基准的 R² 与对同业篮子的 R²，篮子高出 0.1 以上提示可以改用 basket（反过来也提示）；
- 新候选：Yahoo 相似股票里、主题里还没有、和持仓的残差相关 ≥ NEW_PEER 的美股，提示可以考虑加进同业。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from tickersentinel.config import Settings, Theme, is_us_symbol
from tickersentinel.scope.discover import (
    BASKET_GAIN,
    LOOKBACK_DAYS,
    MARKET,
    basket_series,
    log_returns,
    overnight_signal,
    r_squared,
    residual_corr,
)
from tickersentinel.sources.base import PriceSource, SourceError
from tickersentinel.sources.peer_candidates import yahoo_similar

log = logging.getLogger(__name__)

WEAK_PEER = 0.30
WEAK_NIGHT = 0.20
NEW_PEER = 0.60


@dataclass
class ThemeHealth:
    theme: Theme
    peers: dict[str, float] = field(default_factory=dict)  # 同业 → 与持仓的平均残差相关
    night: dict[str, tuple[float, str]] = field(default_factory=dict)  # 夜间信号 → (最高偏相关, 对应的持仓)
    bench_r2: dict[str, tuple[float | None, float | None]] = field(default_factory=dict)  # 持仓 → (对基准, 对篮子)
    advice: list[str] = field(default_factory=list)


async def check_themes(settings: Settings, prices: PriceSource, *, similar: bool = True) -> list[ThemeHealth]:
    u = settings.universe
    themes = [t for t in u.active_themes if t.key != "default" or t.peers]
    if not themes:
        return []
    holders = {t.key: [s for s in u.theme_holdings(t.key) if is_us_symbol(s)] for t in themes}
    sim: dict[str, list[str]] = {}
    if similar:
        for t in themes:
            for h in holders[t.key]:
                try:
                    sim[h] = await yahoo_similar(h)
                except SourceError as exc:
                    log.info("联动体检：%s 的相似股票取不到：%s", h, exc)
    benches = [t.benchmark for t in themes if not t.basket]
    syms = list(dict.fromkeys([MARKET, *benches, *(s for hs in holders.values() for s in hs),
                               *(p for t in themes for p in t.peers), *(o for t in themes for o in t.overnight),
                               *(s for ss in sim.values() for s in ss if is_us_symbol(s))]))
    daily = await prices.daily_bars(syms, lookback_days=LOOKBACK_DAYS)
    r = log_returns(daily)
    out = []
    for t in themes:
        h = ThemeHealth(t)
        hs = [s for s in holders[t.key] if s in r]
        us_peers = [p for p in t.peers if is_us_symbol(p)]
        for p in us_peers:
            vals = [c for s in hs if (c := residual_corr(r, s, p)[0]) is not None]
            if vals:
                h.peers[p] = float(np.mean(vals))
                if h.peers[p] < WEAK_PEER:
                    h.advice.append(f"「{t.name}」的同业 {u.label(p)}（{p}）和持仓的联动变弱了（剔除大盘后的平均相关 {h.peers[p]:.2f}），"
                                    "可以考虑从 peers 挪到 catalysts 或去掉")
        for o in t.overnight:
            best: tuple[float, str] | None = None
            for s in hs:
                pc, _, n = overnight_signal(daily, s, o)
                if pc is not None and (best is None or pc > best[0]):
                    best = (pc, s)
            if best is not None:
                h.night[o] = best
                if best[0] < WEAK_NIGHT:
                    h.advice.append(f"「{t.name}」的夜间信号 {u.label(o)}（{o}）对持仓次日开盘的预示变弱了（偏相关最高 {best[0]:.2f}）")
        bs = basket_series(r, us_peers)
        for s in hs:
            rb = r_squared(r[s], r[t.benchmark])[0] if not t.basket and t.benchmark in r else None
            rk = r_squared(r[s], bs)[0] if bs is not None else None
            h.bench_r2[s] = (rb, rk)
        pairs = [v for v in h.bench_r2.values() if v[0] is not None and v[1] is not None]
        if pairs and not t.basket:
            gain = float(np.mean([k - b for b, k in pairs]))
            if gain >= BASKET_GAIN:
                h.advice.append(f"「{t.name}」的持仓对同业篮子的 R² 比对 {t.benchmark} 平均高 {gain:.2f}，可以考虑把 benchmark 改成 basket")
        elif pairs and t.basket and len(us_peers) >= 2:
            pass  # 已经是篮子：没有可比的 ETF，不提示
        known = {*t.peers, *t.catalysts, *u.symbols}
        news: dict[str, float] = {}
        for s in hs:
            for c in sim.get(s, []):
                if c in known or not is_us_symbol(c) or c in news:
                    continue
                v = residual_corr(r, s, c)[0]
                if v is not None and v >= NEW_PEER:
                    news[c] = v
        if news:
            h.advice.append(f"「{t.name}」可以考虑加进同业：" + "、".join(f"{c}（{v:.2f}）" for c, v in
                                                              sorted(news.items(), key=lambda kv: -kv[1])))
        out.append(h)
    return out


def render_health(settings: Settings, rows: list[ThemeHealth], *, brief: bool = False) -> str:
    """brief=True（周报用）：只列建议；没有建议时返回空字符串。"""
    u = settings.universe
    advice = [a for h in rows for a in h.advice]
    if brief:
        if not advice:
            return ""
        return "\n".join(["**联动体检**（近 120 个交易日；只是建议，改用 tsen scope 或编辑 config/themes/）",
                          *[f"- {a}" for a in advice]])
    lines = ["**联动体检**（近 120 个交易日的日线；只是建议，不会自动改）"]
    for h in rows:
        t = h.theme
        lines += ["", f"主题「{t.name}」"]
        if h.peers:
            lines.append("- 同业（与持仓剔除大盘后的平均残差相关）：" + "、".join(f"{u.label(p)} {v:.2f}" for p, v in h.peers.items()))
        if h.night:
            lines.append("- 夜间信号（对持仓次日开盘跳空的偏相关，取最高的那只持仓）："
                         + "、".join(f"{u.label(o)} → {s} {v:.2f}" for o, (v, s) in h.night.items()))
        if h.bench_r2:
            bname = "篮子" if t.basket else t.benchmark
            lines.append(f"- 板块基准（{bname}）的 R²：" + "、".join(
                f"{s} " + "/".join(x for x in ((f"{t.benchmark} {b:.2f}" if b is not None else ""),
                                               (f"篮子 {k:.2f}" if k is not None else "")) if x)
                for s, (b, k) in h.bench_r2.items()))
    lines += ["", "**建议**", *([f"- {a}" for a in advice] or ["- 联动关系没有明显变化"])]
    return "\n".join(lines)


async def linkage_report(settings: Settings, prices: PriceSource, *, brief: bool = False) -> str:
    try:
        rows = await check_themes(settings, prices)
    except Exception as exc:
        return "" if brief else f"联动体检没做成：{type(exc).__name__}: {exc}"
    return render_health(settings, rows, brief=brief)
