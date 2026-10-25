"""同业等权篮子：主题的 benchmark 写成 basket 时，用这个主题里美股同业的等权组合做板块基准。

为什么需要：现成的 ETF 不一定贴合。2026-10-23 实测 LITE 对 SOXX 的 R² 只有 0.40，对 5 只光模块同业的等权篮子 0.71；
SKHY 对 SOXX 0.60、对存储同业篮子 0.70。

口径（让分钟线和日线的“较昨收”一致，β 估计与全天累计规则可以直接复用 ETF 的那一套）：
- 分钟线：每根 K 线 = 100 × 成分“现价 / 该交易日的昨收”的平均（同一交易日内前向填充，某成分还没成交时不计入）。
  于是篮子在每个交易日的昨收都是 100，“篮子较昨收” = 成分较昨收的平均。
- 日线：成分日收益的等权平均连乘（每天再平衡），再缩放到“anchor 那天之前的最后一个收盘 = 100”，
  与分钟线的口径对齐；不给 anchor 时缩放到最后一行 = 100（只用来算涨跌和 β 时，缩放不影响结果）。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from tickersentinel.timeutil import ET


def _daily_close(df: pd.DataFrame | None) -> pd.Series | None:
    if df is None or df.empty:
        return None
    col = "adj_close" if "adj_close" in df.columns else "close"
    c = df[col].dropna().astype(float)
    return c[c > 0] if not c.empty else None


def basket_daily(daily: dict[str, pd.DataFrame], members: list[str], *, anchor: date | None = None) -> pd.DataFrame | None:
    """成分日线 → 篮子日线（close 与 adj_close 相同）。"""
    closes = {m: c for m in members if (c := _daily_close(daily.get(m))) is not None}
    if not closes:
        return None
    frame = pd.DataFrame(closes).sort_index()
    rets = frame.pct_change(fill_method=None)
    mean = rets.mean(axis=1, skipna=True).fillna(0.0)
    level = (1.0 + mean).cumprod()
    if anchor is not None:
        before = level[level.index < pd.Timestamp(anchor)]
        base = float(before.iloc[-1]) if len(before) else float(level.iloc[0])
    else:
        base = float(level.iloc[-1])
    level = 100.0 * level / base
    return pd.DataFrame({"close": level, "adj_close": level})


def _prev_closes(daily_close: pd.Series, days: np.ndarray) -> np.ndarray:
    """每个交易日（美东日期）之前的最后一个日线收盘。"""
    idx = daily_close.index.values.astype("datetime64[ns]")
    pos = np.searchsorted(idx, days.astype("datetime64[ns]"), side="left") - 1
    vals = daily_close.to_numpy(dtype=float)
    out = np.full(len(days), np.nan)
    ok = pos >= 0
    out[ok] = vals[pos[ok]]
    return out


def basket_intraday(intraday: dict[str, pd.DataFrame], daily: dict[str, pd.DataFrame], members: list[str], *,
                    tz=ET) -> pd.DataFrame | None:
    """成分分钟线 + 日线（用来取每个交易日的昨收）→ 篮子分钟线（close，volume 记 0：篮子没有成交量画像）。
    tz：按哪个时区的日期划分交易日——美股用美东；海外篮子用交易所当地时区（首尔的交易时段跨过美东午夜，按美东日期会被劈成两天）。"""
    rel: dict[str, pd.Series] = {}
    for m in members:
        df, d = intraday.get(m), daily.get(m)
        if df is None or df.empty or d is None or d.empty or "close" not in d:
            continue
        c = df["close"].dropna().astype(float)
        if c.empty:
            continue
        days = np.array(c.index.tz_convert(tz).date, dtype="datetime64[D]")
        pc = _prev_closes(d["close"].dropna().astype(float), days)
        rel[m] = pd.Series(c.to_numpy() / pc, index=c.index)
    if not rel:
        return None
    frame = pd.DataFrame(rel).sort_index()
    day_key = pd.Index(frame.index.tz_convert(tz).date)
    frame = frame.groupby(day_key).ffill()  # 只在同一交易日内前向填充：不能把昨天的比值带进今天的盘前
    level = 100.0 * frame.mean(axis=1, skipna=True)
    level = level.dropna()
    return pd.DataFrame({"close": level, "volume": 0.0}, index=level.index)


def add_baskets(baskets: dict[str, list[str]], *, intraday: dict[str, pd.DataFrame] | None = None,
                daily: dict[str, pd.DataFrame] | None = None, anchor: date | None = None,
                tz=ET) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """把篮子（伪代码 basket:<主题>）加进分钟线 / 日线字典，返回新的字典（不改调用方的）。
    日线每次按 anchor 重算（回放里每一段的 anchor 不同）；分钟线要用成分的日线取昨收，已经有的不重算
    （口径与 anchor 无关，回放时整段分钟线只算一次）。"""
    intra = dict(intraday or {})
    day = dict(daily or {})
    for code, members in baskets.items():
        b = basket_daily(day, members, anchor=anchor)
        if b is not None:
            day[code] = b
        if intraday is not None and code not in intra:
            m = basket_intraday(intra, day, members, tz=tz)
            if m is not None:
                intra[code] = m
    return intra, day
