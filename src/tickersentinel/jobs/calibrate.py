"""tsen assets：列出每个盯盘资产的警报标准，并和它最近的波动对比，帮助在 config/assets.yaml 里调灵敏度。

- 急涨急跌：窗口下限换算成“相当于几倍的窗口波动”（σ 用前几个同类段的 5 分钟收益稳健估计，和实盘检测一致）。
  例：5 分钟 1.5% 而 5 分钟 σ≈0.2%，就是 7.5σ —— 很少触发；同样的 1.5% 对黄金（5 分钟 σ≈0.05%）就是 30σ，几乎永不触发。
- 全天累计：换算成“相当于几倍的日波动”（近 60 个交易日日收益的稳健标准差）。
"""

from __future__ import annotations

import math
from datetime import datetime

import numpy as np
import pandas as pd

from tickersentinel.config import ASSET_CLASS_NAMES, Settings, is_pseudo
from tickersentinel.detect.baseline import robust_std
from tickersentinel.detect.basket import add_baskets
from tickersentinel.jobs.monitor import build_groups, prepare_context
from tickersentinel.market_calendar import MarketCalendar
from tickersentinel.sources.base import PriceSource
from tickersentinel.timeutil import utcnow


def daily_sigma_pct(df: pd.DataFrame | None, n: int = 60) -> float | None:
    if df is None or df.empty or "close" not in df:
        return None
    c = df["close"].dropna()
    c = c[c > 0]
    r = np.log(c).diff().dropna().tail(n).to_numpy()
    s = robust_std(r) if len(r) >= 30 else None
    return s * 100 if s else None


async def asset_report(settings: Settings, calendar: MarketCalendar, prices: PriceSource, now: datetime | None = None) -> str:
    now = now or utcnow()
    u = settings.universe
    lines = ["盯盘资产与警报标准（改 config/assets.yaml；σ 为最近的典型波动，倍数越大越难触发）", ""]
    for g in build_groups(settings, calendar):
        segs = g.clock.completed(now, 2)
        main = [s for s in segs if s.kind in ("regular", "cme", "utc", "exchange")] or segs
        if not main:
            continue
        seg = main[-1]
        try:
            ctx = await prepare_context(settings, g, seg, prices)
        except Exception as exc:  # 数据源失败：只列阈值
            ctx = None
            lines.append(f"（{g.key} 组取数据失败：{type(exc).__name__}: {exc}）")
        try:
            daily = await prices.daily_bars(g.fetch, lookback_days=90)
            if g.baskets:  # 篮子的日波动由成分算
                _, daily = add_baskets(g.baskets, daily=daily, tz=g.basket_tz)
        except Exception:
            daily = {}
        for sym in g.symbols:
            inst = u.find(sym)
            rules = settings.rules_for(sym)
            if rules.rules == ["idio"]:  # 夜间篮子的成分：只在相对篮子单独大动时推送
                idio = rules.idiosyncratic
                lines.append(f"**{sym}** {inst.name if inst else ''}（{u.label(u.night_basket_of(sym) or '')}的成分）")
                lines.append(f"- 只在单独大动时推送：自己涨跌 ≥{idio.min_raw_move_pct or idio.min_move_pct:g}%、相对篮子偏离 "
                             f"≥{idio.min_move_pct:g}%、z ≥ {idio.z_threshold:g}（整体的涨跌由篮子触发；"
                             "成分门槛改主题的 overnight_single_pct，篮子的阈值用 tsen scope tune 主题名）")
                continue
            cls = ASSET_CLASS_NAMES.get(inst.asset_class if inst else "us_equity", "")
            hours = ""
            if g.key == "us":
                hours = "，盘前 + 盘中 + 盘后" if g.hours.get(sym) == "extended" else "，只盯常规时段"
            role = "" if inst is None or inst.holding else f"，只盯盘（{'、'.join(inst.related_to) or '无关联持仓'}）"
            lines.append(f"**{u.display(sym)}** {inst.name if inst and not is_pseudo(sym) else ''}（{cls}{hours}{role}）")
            base = ctx.baselines.get(sym) if ctx else None
            parts = []
            for w in rules.windows_minutes:
                floor = rules.min_move_pct[w]
                if base is not None and base.sigma_1m:
                    sw = base.sigma_1m * math.sqrt(w) * 100
                    parts.append(f"{w}分钟 {floor:g}%（σ≈{sw:.2f}%，{floor / sw:.1f}σ）")
                else:
                    parts.append(f"{w}分钟 {floor:g}%")
            lines.append(f"- 急涨急跌（从近期高 / 低点算）：{'；'.join(parts)}；另需 z ≥ {rules.z_threshold:g}")
            ds = daily_sigma_pct(daily.get(sym))
            day = "/".join(f"{lv:g}%" for lv in rules.day_pct)
            if ds and rules.day_pct:
                lines.append(f"- 全天累计：{day}（日波动 σ≈{ds:.1f}%，最低一档 ≈ {rules.day_pct[0] / ds:.1f}σ）")
            else:
                lines.append(f"- 全天累计：{day or '不推'}")
        lines.append("")
    lines.append("参考：急涨急跌下限在 4–8σ、全天最低一档在 1σ 上下时，推送量一般适中；用 tsen replay --days 10 实测，"
                 "tsen scope tune 代码 按日波动给建议。")
    return "\n".join(lines)
