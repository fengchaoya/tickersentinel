"""开盘前估计的基线参数 —— 借用事件研究的思路：用“估计窗口”（此前若干交易日）估参数，
在“事件窗口”（当日盘中）里判断收益是否异常。

- β：前 60 个交易日日收益对板块基准回归（每只股票用自己主题的基准：ETF，或同业等权篮子，见 detect/basket.py）；
- σ：前 5 个交易日、会话内不重叠的 5 分钟对数收益，用 MAD 稳健估计后折算到每分钟
  （用 5 分钟而不是 1 分钟，是为了减弱买卖价跳动和稀疏成交带来的噪声；IEX 数据尤其需要）；
- 成交量画像：开盘后第 k 分钟的历史成交量中位数，用于“同时段相对成交量”。
"""

from __future__ import annotations

import logging
import math
import warnings
from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import pandas as pd

from tickersentinel.config import PriceRules
from tickersentinel.market_calendar import Session, session_grid

log = logging.getLogger(__name__)


@dataclass
class Baseline:
    symbol: str
    beta: float
    beta_fallback: bool  # True 表示数据不足，β 用了默认值 1.0
    sigma_1m: float | None  # 每分钟对数收益波动
    sigma_ar_1m: float | None  # 每分钟异常收益（r - β·r_m）波动
    volume_profile: np.ndarray | None
    n_obs: int  # 估计 σ 所用的 5 分钟收益样本数

    def describe(self) -> str:
        s = f"{self.sigma_1m * math.sqrt(15) * 100:.2f}%" if self.sigma_1m else "n/a"
        return f"{self.symbol}: β={self.beta:.2f}{'(默认)' if self.beta_fallback else ''}，15分钟σ≈{s}，样本 {self.n_obs}"


@dataclass
class SessionContext:
    session: Session
    baselines: dict[str, Baseline]  # 监控标的
    prev_close: dict[str, float]  # 所有相关代码（含基准、同业）的昨收
    benchmark: str | None = None  # 所有标的共用的基准（benchmarks 里没有的标的用它）
    benchmarks: dict[str, str | None] = field(default_factory=dict)  # 标的 → 自己主题的基准（可能是 basket:<主题>）
    peers: dict[str, list[str]] = field(default_factory=dict)  # 标的 → 同期对比的代码；没有的标的对比全部其他代码
    basket_daily: dict[str, pd.DataFrame] = field(default_factory=dict)  # 篮子成分的日线：检测时用来把分钟线算成篮子
    # 同一个 key（同一个昨收）里更早那一段的最高 / 最低点：盘中段带上当天盘前的，“全天累计”的起点才能跨段连起来
    # （否则盘前已经推过的档位开盘后会再推一次）。代码 → (最高价, 时间, 最低价, 时间)
    carry: dict[str, tuple[float, datetime, float, datetime]] = field(default_factory=dict)

    def benchmark_for(self, symbol: str) -> str | None:
        return self.benchmarks[symbol] if symbol in self.benchmarks else self.benchmark

    def peers_for(self, symbol: str) -> list[str] | None:
        return self.peers.get(symbol)


def session_frame(df: pd.DataFrame | None, session: Session) -> pd.DataFrame:
    """把分钟 K 线对齐到完整的交易时段网格：close 前向填充到最后一根真实 K 线为止，之后为 NaN；
    volume 缺失记 0。attrs["n_obs"] = 已有数据的分钟数。"""
    grid = session_grid(session)
    out = pd.DataFrame({"close": np.nan, "volume": np.nan}, index=grid)
    out.attrs["n_obs"] = 0
    if df is None or df.empty:
        return out
    sub = df[(df.index >= session.open) & (df.index < session.close)]
    if sub.empty:
        return out
    idx = sub.index.floor("min")
    sub = sub.set_axis(idx)
    sub = sub[~sub.index.duplicated(keep="last")]
    n = int(grid.get_indexer([sub.index[-1]])[0]) + 1
    close = sub["close"].astype(float).reindex(grid).ffill()
    vol = sub["volume"].astype(float).reindex(grid).fillna(0.0) if "volume" in sub else pd.Series(0.0, index=grid)
    close.iloc[n:] = np.nan
    vol.iloc[n:] = np.nan
    out = pd.DataFrame({"close": close, "volume": vol}, index=grid)
    out.attrs["n_obs"] = n
    if session.break_offsets is not None:
        out.attrs["break"] = session.break_offsets
    return out


THIN_SEGMENTS = ("pre", "post")  # 成交稀疏、零星错价多的时段


def despike(v: pd.DataFrame, n: int = 3) -> pd.DataFrame:
    """盘前盘后用最近 n 根 K 线收盘价的中位数代替最新价：一笔错价（实测：收盘集合竞价的成交 17:21 才补报，
    分钟线上凭空出现 -2% 再弹回）过不了中位数，真实的移动只晚 1–2 分钟。盘前盘后 yfinance 的成交量都是 0，
    没法按成交量过滤。"""
    if v.empty:
        return v
    out = v.copy()
    out["close"] = v["close"].rolling(n, min_periods=1).median()
    return out


def day_extremes(intraday: dict[str, pd.DataFrame], symbols: list[str], seg: Session
                 ) -> dict[str, tuple[float, datetime, float, datetime]]:
    """各代码在 seg 时段里的最高 / 最低价和第一次出现的时间。算法和检测时完全一样（session_frame 网格、
    稀疏时段取 3 根中位数、第一次出现的极值），这样开盘后的起点和盘前推送时的起点是同一个，推过的档位不会重推。"""
    out = {}
    for sym in symbols:
        f = session_frame(intraday.get(sym), seg)
        n = f.attrs.get("n_obs", 0)
        if n == 0:
            continue
        v = f.iloc[:n]
        if getattr(seg, "kind", "") in THIN_SEGMENTS:
            v = despike(v)
        c = v["close"].to_numpy(dtype=float)
        if not np.isfinite(c).any():
            continue
        hi, lo = float(np.nanmax(c)), float(np.nanmin(c))
        j_hi, j_lo = int(np.flatnonzero(c == hi)[0]), int(np.flatnonzero(c == lo)[0])
        out[sym] = (hi, v.index[j_hi].to_pydatetime(), lo, v.index[j_lo].to_pydatetime())
    return out


def robust_std(x: np.ndarray) -> float | None:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) < 30:
        return None
    mad = float(np.median(np.abs(x - np.median(x))))
    s = 1.4826 * mad
    if s <= 0:
        s = float(np.std(x))
    return s if s > 0 else None


def five_min_returns(frame: pd.DataFrame) -> pd.Series:
    """会话内、不重叠的 5 分钟对数收益（不跨隔夜，也不跨午休：午休前后的价差不是 5 分钟的波动，
    午休里前向填充的 0 收益也会把 σ 压低）。"""
    n = frame.attrs.get("n_obs", 0)
    parts = [(0, n)]
    if "break" in frame.attrs:
        b0, b1 = frame.attrs["break"]
        parts = [(0, min(n, b0)), (b1, n)]
    out = []
    for lo, hi in parts:
        if hi - lo < 2:
            continue
        c = frame["close"].iloc[lo:hi].iloc[::5].dropna()
        c = c[c > 0]
        out.append(np.log(c).diff().dropna())
    return pd.concat(out) if out else pd.Series(dtype=float)


def volume_profile(frames: list[pd.DataFrame]) -> np.ndarray | None:
    rows = []
    for f in frames:
        n = f.attrs.get("n_obs", 0)
        if n > 0:
            rows.append(f["volume"].to_numpy(dtype=float)[:n])
    if not rows:
        return None
    width = max(len(r) for r in rows)
    mat = np.full((len(rows), width), np.nan)
    for i, r in enumerate(rows):
        mat[i, : len(r)] = r
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # 某些分钟全为 NaN 时 nanmedian 会告警
        return np.nanmedian(mat, axis=0)


def estimate_beta(stock: pd.DataFrame | None, bench: pd.DataFrame | None, *, before: date, lookback: int) -> tuple[float, bool]:
    if stock is None or bench is None or stock.empty or bench.empty:
        return 1.0, True
    col = "adj_close" if "adj_close" in stock.columns and "adj_close" in bench.columns else "close"
    df = pd.concat({"s": stock[col], "b": bench[col]}, axis=1, join="inner").dropna()
    df = df[df.index < pd.Timestamp(before)]
    df = df[(df > 0).all(axis=1)]
    r = np.log(df).diff().dropna().tail(lookback)
    if len(r) < 20:
        return 1.0, True
    var = float(r["b"].var())
    if not np.isfinite(var) or var <= 0:
        return 1.0, True
    beta = float(r["s"].cov(r["b"]) / var)
    if not np.isfinite(beta):
        return 1.0, True
    return float(np.clip(beta, 0.0, 3.0)), False


def prev_close_from_daily(df: pd.DataFrame | None, day: date) -> float | None:
    if df is None or df.empty or "close" not in df.columns:
        return None
    c = df["close"].dropna()
    c = c[c.index < pd.Timestamp(day)]
    return float(c.iloc[-1]) if len(c) else None


def build_session_context(
    *,
    session: Session,
    prior_sessions: list[Session],
    intraday: dict[str, pd.DataFrame],
    daily: dict[str, pd.DataFrame],
    watchlist: list[str],
    benchmark: str | None = None,
    others: list[str],
    cfg: PriceRules,
    prev_close: dict[str, float] | None = None,
    benchmarks: dict[str, str | None] | None = None,
    peers: dict[str, list[str]] | None = None,
) -> SessionContext:
    """prev_close：参照价（全天累计的“较昨收”）。不给时按日线取 session.day 之前的最后一个收盘；
    盘后段、期货段等参照价不是“昨收”的，由调用方算好传进来。
    benchmark：所有标的共用的基准；benchmarks：按标的的基准（主题不同的持仓用不同的基准），优先于 benchmark。
    基准可以为空（期货、海外股票不做板块拆分）。peers：按标的的同期对比代码。"""
    bmap = dict(benchmarks or {})
    used = [bmap.get(w, benchmark) for w in watchlist]
    symbols = list(dict.fromkeys(s for s in [*watchlist, benchmark, *used, *others] if s))
    given = prev_close or {}
    prior = {sym: [session_frame(intraday.get(sym), s) for s in prior_sessions] for sym in symbols}

    refs: dict[str, float] = {}
    for sym in symbols:
        if sym in given:
            refs[sym] = given[sym]
            continue
        if prev_close is not None:  # 调用方负责参照价：没给的就是没有（不能退回“日线昨收”，口径会错）
            continue
        pc = prev_close_from_daily(daily.get(sym), session.day)
        if pc is None and prior[sym]:
            last = prior[sym][-1]
            n = last.attrs.get("n_obs", 0)
            if n:
                pc = float(last["close"].iloc[n - 1])
        if pc:
            refs[sym] = pc

    baselines: dict[str, Baseline] = {}
    for sym in watchlist:
        bench = bmap.get(sym, benchmark)
        bench_frames = (prior.get(bench) if bench else None) or []
        if bench:
            beta, fallback = estimate_beta(daily.get(sym), daily.get(bench), before=session.day,
                                           lookback=cfg.beta_lookback_days)
        else:
            beta, fallback = 1.0, True
        rets, ars = [], []
        for i, f in enumerate(prior[sym]):
            rs = five_min_returns(f)
            rets.append(rs)
            if i < len(bench_frames):
                rb = five_min_returns(bench_frames[i])
                j = pd.concat({"s": rs, "b": rb}, axis=1, join="inner").dropna()
                ars.append(j["s"] - beta * j["b"])
        r = pd.concat(rets) if rets else pd.Series(dtype=float)
        ar = pd.concat(ars) if ars else pd.Series(dtype=float)
        s5 = robust_std(r.to_numpy()) if len(r) >= cfg.min_sigma_obs else None
        sa5 = robust_std(ar.to_numpy()) if len(ar) >= cfg.min_sigma_obs else None
        base = Baseline(
            symbol=sym,
            beta=beta,
            beta_fallback=fallback,
            sigma_1m=s5 / math.sqrt(5) if s5 else None,
            sigma_ar_1m=sa5 / math.sqrt(5) if sa5 else None,
            volume_profile=volume_profile(prior[sym]),
            n_obs=len(r),
        )
        if base.sigma_1m is None:
            log.warning("%s 波动率样本不足（%d < %d），急涨急跌规则只按百分比阈值判断", sym, len(r), cfg.min_sigma_obs)
        baselines[sym] = base
    return SessionContext(session=session, baselines=baselines, prev_close=refs, benchmark=benchmark,
                          benchmarks={w: bmap.get(w, benchmark) for w in watchlist}, peers=dict(peers or {}))
