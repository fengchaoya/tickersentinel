"""价格异动检测（纯计算，无网络、无状态；冷却逻辑在 cooldown.py）。

三条规则（2026-10-24 按用户要求改写：要报的是“日内急涨急跌”，不只是较昨收的关口）：
  A 急涨急跌 rapid：最近 w 分钟（5/15/30/60）里的最高点到现价跌了多少（或最低点到现价涨了多少），同时满足
        ① 幅度 ≥ 这一档的下限（“幅度够大”）
        ② |z| = |r| / (σ_1m·√w) ≥ z 阈值（“相对这只股票近期的波动足够反常”）
     看高低点而不是窗口起点：“先涨 2% 再跌到 -1%”这种冲高回落，起点到现价只有 -1%，从高点算是 -3%。
     z 用窗口长度 √w 而不是“离高点几分钟”：窗口里最高点到现价的回撤，在随机游走下和窗口内的最大涨幅同分布
     （反射原理），按 σ√w 标准化才有稳定的含义。窗口不早于开盘（和午休后的复盘）。
  B 全天累计 day：从“参照价（昨收；盘后为今日收盘）与这一段的最高点里更高的那个”算起的跌幅（涨的方向对称：
     参照价与最低点里更低的那个），越过 5/8/12% 这样的档位时推送，每档一次；创出新的高 / 低点后重新计。
     负责跳空（起点就是昨收）、几个小时的一路下跌、慢速的冲高回落 / 探底回升。取代原来较昨收的“关口”。
  C 个股特异 idio（默认关闭）：剔除板块后的异常收益 AR = r - β·r_基准 显著（夜间篮子的成分只跑这一条）。

每个标的每次检测最多产生一个 Alert，多条规则同时触发时合并展示。
"""

from __future__ import annotations

import math
from dataclasses import asdict
from datetime import datetime

import numpy as np
import pandas as pd

from tickersentinel.config import PriceRules
from tickersentinel.detect.baseline import THIN_SEGMENTS, Baseline, SessionContext, despike
from tickersentinel.models import RULE_PRIORITY, Alert, SplitContext, Trigger


def _sign(x: float) -> int:
    return 1 if x > 0 else -1


def log_ret(close: np.ndarray, w: int) -> float | None:
    """最新价相对 w 分钟前的对数收益；数据不足返回 None。"""
    if len(close) <= w:
        return None
    a, b = close[-1 - w], close[-1]
    if not (np.isfinite(a) and np.isfinite(b)) or a <= 0 or b <= 0:
        return None
    return math.log(b / a)


def extreme_at(seg: np.ndarray, kind: str, *, latest: bool) -> int | None:
    """seg 里最高（kind="max"）/ 最低（"min"）点的位置。latest=True 取最后一次出现（算“用了几分钟”更准）；
    False 取第一次出现（全天累计的起点：之后再碰到同一个价位不算新的起点）。"""
    if not np.isfinite(seg).any():
        return None
    v = np.nanmax(seg) if kind == "max" else np.nanmin(seg)
    idx = np.flatnonzero(seg == v)
    return int(idx[-1] if latest else idx[0])


def window_pct(close: np.ndarray, w: int | None, prev_close: float | None) -> float | None:
    """窗口涨跌（%）。w=None 表示较昨收。有参照价时按参照价的百分点算（和触发条件同一口径：
    “+2% 跌到 -1%”是 -3），没有时按窗口起点的百分比。"""
    if len(close) == 0 or not np.isfinite(close[-1]):
        return None
    if w is None:
        return (close[-1] / prev_close - 1) * 100 if prev_close else None
    r = log_ret(close, w)
    if r is None:
        return None
    if prev_close:
        return (close[-1] - close[-1 - w]) / prev_close * 100
    return math.expm1(r) * 100


def align_to(close: np.ndarray, n: int) -> np.ndarray:
    """把基准 / 同业的收盘价对齐到标的的 n 根 K 线：多出来的截掉（不看标的最新一根之后的），不够的用最后一个价格补齐
    （截至那一刻最近的成交价）。否则基准成交稀疏时，它的“最近 w 分钟”停在自己最后一笔成交，和标的的窗口对不上。"""
    if len(close) >= n:
        return close[:n]
    if len(close) == 0:
        return np.full(n, np.nan)
    return np.concatenate([close, np.full(n - len(close), close[-1])])


def volume_ratio(volume: np.ndarray, profile: np.ndarray | None, w: int | None) -> float | None:
    if profile is None:
        return None
    n = len(volume)
    lo = 0 if w is None else n - w
    if lo < 0:
        return None
    actual = float(np.nansum(volume[lo:n]))
    expected = float(np.nansum(profile[lo:n]))
    return actual / expected if expected > 0 else None


class PriceAnomalyDetector:
    def __init__(self, cfg: PriceRules, rules: dict[str, PriceRules] | None = None):
        """cfg：默认检测参数；rules：按资产的检测参数（config/assets.yaml 的警报标准，见 Settings.rules_for）。"""
        self.cfg = cfg
        self.rules = rules or {}

    @staticmethod
    def bars_until(ctx: SessionContext, as_of: datetime | None) -> int:
        """as_of 之前（含正在形成的那一分钟）可用的 K 线根数。"""
        if as_of is None:
            return 10**9
        return max(0, math.ceil((as_of - ctx.session.open).total_seconds() / 60))

    def evaluate(self, ctx: SessionContext, frames: dict[str, pd.DataFrame], as_of: datetime | None = None) -> list[Alert]:
        """frames：经 session_frame() 对齐的当日分钟数据（需包含基准与同业）。返回候选异动（未经冷却过滤）。"""
        k = self.bars_until(ctx, as_of)
        views = {s: f.iloc[: min(f.attrs.get("n_obs", 0), k)] for s, f in frames.items()}
        if getattr(ctx.session, "kind", "regular") in THIN_SEGMENTS:
            views = {s: despike(v) for s, v in views.items()}
        alerts = []
        for sym, base in ctx.baselines.items():
            v = views.get(sym)
            if v is None or v.empty:
                continue
            alert = self._evaluate_symbol(sym, v, views, base, ctx)
            if alert is not None:
                alerts.append(alert)
        return alerts

    def _evaluate_symbol(self, sym: str, v: pd.DataFrame, views: dict[str, pd.DataFrame],
                         base: Baseline, ctx: SessionContext) -> Alert | None:
        cfg = self.rules.get(sym, self.cfg)
        close = v["close"].to_numpy(dtype=float)
        price = close[-1]
        if not np.isfinite(price):
            return None
        n = len(close)
        prev = ctx.prev_close.get(sym)
        bench = ctx.benchmark_for(sym)
        bench_view = views.get(bench) if bench else None
        bclose = (align_to(bench_view["close"].to_numpy(dtype=float), n)
                  if bench_view is not None and not bench_view.empty else None)
        # 开盘竞价的噪声：段开头 open_warmup_minutes 分钟不跑 A/C。盘后段例外——财报常在 16:05 发布，
        # 前几分钟正是要盯的时候（跳空由全天累计规则兜住，急涨急跌也要攒够 K 线才有高低点可比）
        kind = getattr(ctx.session, "kind", "regular")
        warm = kind == "post" or n > cfg.open_warmup_minutes
        # 午休（东京、上海深圳、香港）：复盘也是集合竞价，和开盘一样先预热；窗口不能跨午休（否则午休前后的价差
        # 会被当成几分钟内的急涨急跌），所以复盘后最多只往回看到复盘那一刻
        brk = ctx.session.break_offsets if hasattr(ctx.session, "break_offsets") else None
        since, lo_bound = n, 0  # 当前这半段已有几根 K 线、窗口最早能看到哪一根
        if brk is not None and n > brk[1]:
            since, lo_bound = n - brk[1], brk[1]
            warm = warm and since > cfg.open_warmup_minutes
        triggers: list[Trigger] = []
        only = set(cfg.rules) if cfg.rules is not None else None  # 只跑部分规则（夜间篮子的成分只跑个股特异）
        ts = v.index

        def ref_pct(x: float) -> float | None:
            return (x / prev - 1) * 100 if prev else None

        def points(anchor: float) -> float:
            """从起点到现价走了多少，按参照价的百分点算（“+2% 跌到 -1%”是 3 个点）；没有参照价时按起点的百分比。"""
            return (price - anchor) / prev * 100 if prev else (price / anchor - 1) * 100

        # 规则 A：急涨急跌（最近 w 分钟内的最高 / 最低点 → 现价）
        if warm and (only is None or "rapid" in only):
            best: dict[int, tuple[float, Trigger, int]] = {}  # 方向 → (分数, 触发, 起点位置)
            shortest = min(cfg.windows_minutes)
            for w in cfg.windows_minutes:
                lo = max(lo_bound, n - 1 - w)  # 开盘（复盘）不到 w 分钟时，从开盘（复盘）算起
                teff = n - 1 - lo
                if teff < min(w, shortest):  # 至少攒够最短一档的分钟数（盘后段没有预热，开头一两分钟的跳动不算）
                    continue
                for ext, dirn in (("max", -1), ("min", 1)):
                    j = extreme_at(close[lo:n], ext, latest=True)
                    if j is None:
                        continue
                    a = lo + j
                    anchor = close[a]
                    if not anchor > 0:
                        continue
                    r = math.log(price / anchor)
                    if r * dirn <= 0:
                        continue
                    pct = points(anchor)
                    if abs(pct) < cfg.min_move_pct[w]:
                        continue
                    z = r / (base.sigma_1m * math.sqrt(teff)) if base.sigma_1m else None
                    if z is not None and abs(z) < cfg.z_threshold:
                        continue
                    score = abs(z) if z is not None else abs(pct) / cfg.min_move_pct[w]
                    if dirn not in best or score > best[dirn][0]:
                        best[dirn] = (score, Trigger("rapid", dirn, pct, window_minutes=w, z=z,
                                                     anchor_ts=ts[a].to_pydatetime(), anchor_pct=ref_pct(anchor),
                                                     minutes=n - 1 - a), a)
            if best:  # 两个方向都够（大起大落）：取起点更近的那一段，也就是正在走的这一波
                triggers.append(max(best.values(), key=lambda x: x[2])[1])

        # 规则 B：全天累计（参照价、同一 key 更早那一段（盘前）和这一段的最高 / 最低点里最远的那个 → 现价）
        if prev and cfg.day_pct and (only is None or "day" in only):
            carry = ctx.carry.get(sym) if getattr(ctx, "carry", None) else None
            legs: list[tuple[float, Trigger]] = []
            for ext, dirn in (("max", -1), ("min", 1)):
                # 候选起点按时间先后：昨收、盘前的高（低）点、这一段的高（低）点；一样远时取更早的（起点不来回换）
                cands: list[tuple[float, datetime | None, int | None]] = [(prev, None, None)]
                if carry is not None:
                    cands.append((carry[0], carry[1], None) if dirn < 0 else (carry[2], carry[3], None))
                if (j := extreme_at(close[:n], ext, latest=False)) is not None:
                    cands.append((float(close[j]), ts[j].to_pydatetime(), j))
                anchor, a_ts, a = cands[0]
                for c in cands[1:]:
                    if (c[0] > anchor) if dirn < 0 else (c[0] < anchor):
                        anchor, a_ts, a = c
                move = points(anchor)
                crossed = [x for x in cfg.day_pct if move * dirn >= x]
                if crossed:
                    legs.append((a_ts.timestamp() if a_ts else float("-inf"), Trigger(
                        "day", dirn, move, level_pct=crossed[-1], anchor_ts=a_ts, anchor_pct=ref_pct(anchor),
                        minutes=None if a is None else n - 1 - a)))
            if legs:  # 两个方向都够：取起点更近的那一段
                triggers.append(max(legs, key=lambda x: x[0])[1])

        # 规则 C：个股特异
        idio = cfg.idiosyncratic
        if warm and idio.enabled and (only is None or "idio" in only) and bclose is not None and base.sigma_ar_1m:
            best = None
            for w in cfg.windows_minutes:
                if w >= since:
                    continue
                r, rb = log_ret(close, w), log_ret(bclose, w)
                if r is None or rb is None:
                    continue
                ar = r - base.beta * rb
                pct = math.expm1(ar) * 100
                z = ar / (base.sigma_ar_1m * math.sqrt(w))
                if idio.min_raw_move_pct is not None and (abs(math.expm1(r) * 100) < idio.min_raw_move_pct or r * ar <= 0):
                    continue  # 自己没怎么动（或方向和“特异”相反）：是别的成分在动
                if abs(pct) >= idio.min_move_pct and abs(z) >= idio.z_threshold and (best is None or abs(z) > best[0]):
                    best = (abs(z), Trigger("idio", _sign(ar), pct, window_minutes=w, z=z))
            if best:
                triggers.append(best[1])

        if not triggers:
            return None

        # 每个触发口径都算一份拆分上下文：冷却过滤后标题可能换成别的触发（见 Alert.with_triggers）
        contexts = {cw: split_context(sym, cw, views, base, ctx) for cw in {t.context_window for t in triggers}}
        head = min(triggers, key=lambda t: RULE_PRIORITY[t.rule])
        return Alert(
            symbol=sym,
            segment=kind,
            ts=v.index[-1].to_pydatetime(),
            price=float(price),
            prev_close=prev,
            triggers=triggers,
            beta=base.beta,
            benchmark=bench,
            **asdict(contexts[head.context_window]),
            contexts=contexts,
        )


def split_context(sym: str, w: int | None, views: dict[str, pd.DataFrame], base: Baseline,
                  ctx: SessionContext) -> SplitContext:
    """sym 在窗口 w（None 表示较昨收）下的拆分上下文：原始涨跌、板块涨跌、个股特异、成交量倍数、同业同期。
    同期只列 sym 自己主题的同业与基准（ctx.peers）；没有设定时列出全部其他代码。"""
    v = views[sym]
    n = len(v)
    move = window_pct(v["close"].to_numpy(dtype=float), w, ctx.prev_close.get(sym))
    bench = ctx.benchmark_for(sym)
    bench_view = views.get(bench) if bench else None
    bmove = None
    if bench_view is not None and not bench_view.empty:
        bmove = window_pct(align_to(bench_view["close"].to_numpy(dtype=float), n), w, ctx.prev_close.get(bench))
    abn = move - base.beta * bmove if move is not None and bmove is not None else None
    allowed = ctx.peers_for(sym)
    wanted = None if allowed is None else {*allowed, *([bench] if bench else [])}
    peers: dict[str, float] = {}
    for psym, pv in views.items():
        if psym == sym or pv.empty or (wanted is not None and psym not in wanted):
            continue
        pm = window_pct(align_to(pv["close"].to_numpy(dtype=float), n), w, ctx.prev_close.get(psym))
        if pm is not None:
            peers[psym] = pm
    return SplitContext(
        window_minutes=w,
        move_pct=move,
        benchmark_move_pct=bmove,
        abnormal_move_pct=abn,
        volume_ratio=volume_ratio(v["volume"].to_numpy(dtype=float), base.volume_profile, w),
        peers_move_pct=peers,
    )
