"""发现联动、给出方案（tsen scope add / suggest，飞书 /scope add）。只出方案，确认后才由 scope/edit.py 写入配置。

流程：
1. 先看能不能并进已有主题：和主题里美股成员（同业 + 持仓）剔除大盘后的平均残差相关 ≥ JOIN_MIN 就建议并入，不新建；
2. 否则新建主题。候选 = Yahoo 相似股票 + Yahoo 行业龙头 + Finnhub 同业（有 key 时）+ 已有主题里整理过的海外代码
   + 大模型提名（同业、海外对应公司、大客户 / 供应商、检索词、板块指标；默认来自 Claude Code 准备的
   data/scope_candidates/<代码>.json，没有时才用 llm.tasks.scope，默认不调用）。提名的代码要能在 Yahoo 查到才留下；
3. 用近一年日线按四种联动分别挑（决策 19）：
   - 同业：剔除大盘（SPY）后的残差相关（近 120 个交易日）≥ PEER_MIN，最多 MAX_PEERS 只；
   - 板块基准：目标对各行业 ETF 与对同业等权篮子的 R²，篮子高出 BASKET_GAIN 以上才用篮子；
   - 夜间信号（海外代码）：海外当天涨跌与目标次日开盘跳空的偏相关（剔除目标前一天涨跌）≥ OVERNIGHT_MIN，
     样本至少 MIN_DAYS 天，最多 MAX_OVERNIGHT 只；
   - 催化剂公司：大模型给的大客户 / 供应商，不按相关性筛（平时不一定同涨同跌，但财报会牵动板块）；
4. 警报标准按目标自己的日波动换算：类别默认值 × 日波动 / 默认值针对的日波动（suggest_rapid / suggest_day）；
5. 方案里写明数字、样本天数、候选从哪来、没选上的是谁，读者确认后才写入。

阈值是经验值（2026-10-23 用 LITE / SKHY 实测校准，见 CLAUDE.md 决策 19），不是统计检验；样本短时会注明。
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from tickersentinel.config import DEFAULT_THEME, Settings, _infer_class, is_us_symbol
from tickersentinel.detect.baseline import robust_std
from tickersentinel.sources.base import PriceSource, SourceError
from tickersentinel.sources.peer_candidates import (
    Profile,
    finnhub_peers,
    yahoo_industry_top,
    yahoo_names,
    yahoo_profile,
    yahoo_similar,
)
from tickersentinel.timeutil import utcnow

if TYPE_CHECKING:
    from tickersentinel.llm.client import LLMClient

log = logging.getLogger(__name__)

# 剔除大盘用的市场因子：SPY（标准的市场模型）。不用 QQQ：它本身由科技大盘股主导，剔除它会把 AI 板块的共同部分也剔掉
# （2026-10-23 实测 AMD–AVGO 从 0.58 掉到 0.03、MU–WDC 掉到 0.47）；也不用等权的 RSP：它对科技股几乎剔不掉什么（≈ 原始相关）
MARKET = "SPY"
# 板块基准的候选：各行业的大类 ETF（不写死某个行业）；再加上已有主题用的基准
BENCH_CANDIDATES = ["SOXX", "SMH", "QQQ", "SPY", "XLK", "IGV", "XLC", "XLY", "XLP", "XLF", "XLE", "XLV", "XLI", "XLB", "XLU", "XLRE"]
PEER_MIN = 0.50
PEER_WEAK = 0.35
OVERNIGHT_MIN = 0.30
JOIN_MIN = 0.60  # 并入已有主题要求更高：AMD 和存储芯片股也有 0.6 上下的相关（同属 AI 半导体），但不该算进存储主题
BASKET_GAIN = 0.10
MIN_DAYS = 60
PEER_WINDOW = 120
GAP_WINDOW = 160
MAX_PEERS, MAX_OVERNIGHT, MAX_CATALYSTS, MAX_INDICATORS = 6, 3, 10, 2
LOOKBACK_DAYS = 260
_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


_SUFFIXES = re.compile(r"[,.]?\s+(inc\.?|corp\.?|corporation|holdings?|co\.?,?\s*ltd\.?|ltd\.?|plc|group|limited|company|"
                       r"technologies?|n\.v\.?|s\.a\.?|ag|se)$", re.I)


def short_name(name: str) -> str:
    """“Lumentum Holdings Inc.” → “Lumentum”；“NXP Semiconductors N.V.” → “NXP Semiconductors”。"""
    s = (name or "").strip()
    for _ in range(3):
        t = _SUFFIXES.sub("", s).strip(" ,.")
        if t == s or not t:
            break
        s = t
    return s or name


CANDIDATES_DIR = "scope_candidates"  # data/scope_candidates/<代码>.json：在 Claude Code 里准备的候选（见 .claude/skills/add-holding）
CANDIDATES_MAX_AGE_DAYS = 30


def candidates_path(settings: Settings, symbol: str) -> "Path":
    from pathlib import Path

    data_dir = Path(settings.resolve_path(settings.storage.sqlite_path)).parent
    return data_dir / CANDIDATES_DIR / f"{symbol.strip().upper()}.json"


def load_candidates(settings: Settings, symbol: str, path: "str | Path | None" = None) -> dict | None:
    """读候选文件（格式同 SCOPE_SCHEMA）。没给路径时找 data/scope_candidates/<代码>.json，超过 30 天的不用（联动会变）。"""
    import json
    import time
    from pathlib import Path

    p = Path(path) if path else candidates_path(settings, symbol)
    if not p.exists():
        if path:
            raise ScopeError(f"候选文件不存在：{p}")
        return None
    if not path and time.time() - p.stat().st_mtime > CANDIDATES_MAX_AGE_DAYS * 86400:
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ScopeError(f"候选文件不是合法的 JSON：{p}（{exc}）") from exc
    if not isinstance(data, dict):
        raise ScopeError(f"候选文件应该是一个 JSON 对象：{p}")
    return data


class ScopeError(Exception):
    """方案出不来（代码查不到、已经是持仓……），消息直接给读者看。"""


@dataclass
class Pick:
    symbol: str
    name: str = ""
    score: float | None = None  # 同业：残差相关；夜间信号：偏相关；催化剂：None
    n: int = 0  # 样本天数
    note: str = ""
    chosen: bool = True


@dataclass
class Proposal:
    symbol: str
    name: str
    asset_class: str
    theme_key: str
    theme_name: str
    new_theme: bool
    sigma_pct: float | None = None
    focus: str = ""
    benchmark: str = "SPY"  # ETF 代码，或 basket
    bench_scores: dict[str, float] = field(default_factory=dict)  # 代码（或 basket）→ R²
    join_score: float | None = None  # 并入已有主题时：和主题成员的平均残差相关
    join_scores: dict[str, float] = field(default_factory=dict)  # 各已有主题 → 平均残差相关
    peers: list[Pick] = field(default_factory=list)
    overnight: list[Pick] = field(default_factory=list)
    catalysts: list[Pick] = field(default_factory=list)
    rejected: list[Pick] = field(default_factory=list)
    news: dict[str, list[str]] = field(default_factory=dict)
    queries: dict[str, list[str]] = field(default_factory=dict)  # 这只股票自己的检索词
    indicators: list[dict[str, Any]] = field(default_factory=list)  # MacroIndicator 字段
    day_pct: list[float] = field(default_factory=list)  # 建议的全天累计档位；空 = 用类别默认
    rapid: dict[int, float] = field(default_factory=dict)  # 建议的急涨急跌下限；空 = 用类别默认
    overnight_alert: dict[str, Any] | None = None  # 夜间信号（或它们的篮子）按日波动换算的警报标准；和默认一样时为空
    notes: list[str] = field(default_factory=list)
    sources: dict[str, list[str]] = field(default_factory=dict)
    created: str = ""

    def chosen(self, kind: str) -> list[Pick]:
        return [p for p in getattr(self, kind) if p.chosen]

    def drop(self, symbol: str) -> bool:
        """去掉某个候选（飞书里回“去掉 GLW”）。篮子基准的成分随之减少，不足 2 只时退回 R² 最高的 ETF。"""
        hit = False
        for kind in ("peers", "overnight", "catalysts"):
            for p in getattr(self, kind):
                if p.symbol.upper() == symbol.upper() and p.chosen:
                    p.chosen, hit = False, True
        for ind in list(self.indicators):
            if str(ind.get("symbol", "")).upper() == symbol.upper():
                self.indicators.remove(ind)
                hit = True
        if self.benchmark == "basket" and len([p for p in self.chosen("peers") if is_us_symbol(p.symbol)]) < 2:
            etfs = {k: v for k, v in self.bench_scores.items() if k != "basket"}
            self.benchmark = max(etfs, key=etfs.get) if etfs else "SPY"
        return hit

    def restore(self, symbol: str) -> bool:
        """把默认不选的候选加回来（飞书里回“加上 MPWR”）。"""
        hit = False
        for kind in ("peers", "overnight", "catalysts"):
            for p in getattr(self, kind):
                if p.symbol.upper() == symbol.upper() and not p.chosen:
                    p.chosen, hit = True, True
        for p in list(self.rejected):
            if p.symbol.upper() == symbol.upper():
                self.rejected.remove(p)
                p.chosen = True
                (self.overnight if not is_us_symbol(p.symbol) else self.peers).append(p)
                hit = True
        return hit

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Proposal":
        """读回 to_dict 的结果（飞书 /scope 的待确认方案存在 kv 里，是 JSON：字典的数字键会变成字符串，要还原）。"""
        d = dict(d)
        if "levels" in d:  # 旧版（关口）存下的待确认方案
            d["day_pct"] = d.pop("levels")
        d["rapid"] = {int(k): float(v) for k, v in (d.get("rapid") or {}).items()}
        oa = d.get("overnight_alert")
        if isinstance(oa, dict) and oa.get("rapid_pct"):
            d["overnight_alert"] = {**oa, "rapid_pct": {int(k): float(v) for k, v in oa["rapid_pct"].items()}}
        for kind in ("peers", "overnight", "catalysts", "rejected"):
            d[kind] = [Pick(**p) for p in d.get(kind) or []]
        return cls(**d)


# ───────── 打分（纯函数，离线可测） ─────────


def log_returns(daily: dict[str, pd.DataFrame]) -> pd.DataFrame:
    cols = {}
    for sym, df in daily.items():
        if df is None or df.empty:
            continue
        col = "adj_close" if "adj_close" in df.columns else "close"
        c = df[col].dropna().astype(float)
        c = c[c > 0]
        if len(c) > 5:
            cols[sym] = np.log(c).diff()
    return pd.DataFrame(cols).sort_index()


def _resid(y: pd.Series, x: pd.Series) -> pd.Series | None:
    d = pd.concat({"y": y, "x": x}, axis=1).dropna()
    if len(d) < MIN_DAYS or d["x"].var() <= 0:
        return None
    beta = d["y"].cov(d["x"]) / d["x"].var()
    return d["y"] - beta * d["x"]


def residual_corr(r: pd.DataFrame, target: str, other: str, market: str = MARKET,
                  window: int = PEER_WINDOW) -> tuple[float | None, int]:
    """剔除大盘后的日收益相关（近 window 个共同交易日）。"""
    if target not in r or other not in r:
        return None, 0
    if market in r and market not in (target, other):
        a, b = _resid(r[target], r[market]), _resid(r[other], r[market])
    else:
        a, b = r[target], r[other]
    if a is None or b is None:
        return None, 0
    d = pd.concat({"a": a, "b": b}, axis=1).dropna().tail(window)
    if len(d) < MIN_DAYS:
        return None, len(d)
    return float(d.corr().iloc[0, 1]), len(d)


def r_squared(y: pd.Series, x: pd.Series, window: int = PEER_WINDOW) -> tuple[float | None, int]:
    d = pd.concat({"y": y, "x": x}, axis=1).dropna().tail(window)
    if len(d) < MIN_DAYS:
        return None, len(d)
    return float(d.corr().iloc[0, 1] ** 2), len(d)


def overnight_signal(daily: dict[str, pd.DataFrame], target: str, foreign: str,
                     window: int = GAP_WINDOW) -> tuple[float | None, float | None, int]:
    """(偏相关, 跟随相关, 样本数)：海外当天涨跌 vs 目标同一日期的开盘跳空，剔除目标前一天的涨跌。
    海外市场在同一日期先收盘（亚洲），所以它的当天涨跌发生在目标开盘之前；跟随相关 = 海外当天 vs 目标前一天（只是跟着美股走）。"""
    t, f = daily.get(target), daily.get(foreign)
    if t is None or f is None or t.empty or f.empty or "open" not in t:
        return None, None, 0
    tc = t["close"].astype(float)
    gap = np.log(t["open"].astype(float) / tc.shift(1))
    prev = np.log(tc).diff().shift(1)
    fcol = "adj_close" if "adj_close" in f.columns else "close"
    fr = np.log(f[fcol].astype(float)).diff()
    d = pd.DataFrame({"f": fr, "g": gap, "p": prev}).dropna().tail(window)
    if len(d) < MIN_DAYS:
        return None, None, len(d)
    X = np.column_stack([np.ones(len(d)), d["p"].to_numpy()])
    rg = d["g"].to_numpy() - X @ np.linalg.lstsq(X, d["g"].to_numpy(), rcond=None)[0]
    rf = d["f"].to_numpy() - X @ np.linalg.lstsq(X, d["f"].to_numpy(), rcond=None)[0]
    if rg.std() == 0 or rf.std() == 0:
        return None, None, len(d)
    return float(np.corrcoef(rg, rf)[0, 1]), float(d[["f", "p"]].corr().iloc[0, 1]), len(d)


def daily_sigma(r: pd.Series, n: int = 120) -> float | None:
    x = r.dropna().tail(n).to_numpy()
    s = robust_std(x) if len(x) >= 30 else None
    return float(s * 100) if s else None


# 阈值按资产自己的日波动换算：下面这组是日波动 REF_SIGMA（5%，MU / SNDK / SKHY 近一年 5–6%）的股票用的，
# 日波动 2.5% 的股票就减半、黄金（约 1.2%）约为四分之一——相当于各资产用同样的“几倍波动”。不需要大模型：纯计算 + 回放验证
REF_SIGMA = 5.0
BASE_RAPID = {5: 1.5, 15: 2.5, 30: 3.0, 60: 3.0}  # 急涨急跌：最近 w 分钟内从高（低）点回落（反弹）的下限
BASE_DAY = [5.0, 8.0, 12.0]  # 全天累计的档位


def nice_pct(x: float) -> float:
    """取整到好读的数：≥ 2% 取到 0.5，0.5–2% 取到 0.1，更小的取到 0.05。"""
    step = 0.5 if x >= 2 else 0.1 if x >= 0.5 else 0.05
    return round(round(x / step) * step, 2)


def _close(a: list[float], b: list[float], tol: float = 0.1) -> bool:
    """两组阈值每一项都相差不到 10%：算作一样，不为这点差别改配置。"""
    return len(a) == len(b) and all(abs(x - y) <= tol * max(abs(y), 1e-9) for x, y in zip(a, b))


def suggest_rapid(sigma_pct: float | None, default: dict[int, float], ref: float = REF_SIGMA) -> dict[int, float]:
    """急涨急跌的下限 = 类别默认值 × 日波动 / ref（ref = 这类默认值针对的日波动）；没有默认值时用 BASE_RAPID。
    没有日波动、或和 default 相差不到 10% 时照用 default。"""
    if not sigma_pct:
        return dict(default)
    out = {w: nice_pct(v * sigma_pct / ref) for w, v in (default or BASE_RAPID).items()}
    same = sorted(out) == sorted(default) and _close([out[w] for w in sorted(out)], [default[w] for w in sorted(out)])
    return dict(default) if same else out


def suggest_day(sigma_pct: float | None, default: list[float], ref: float = REF_SIGMA) -> list[float]:
    """全天累计的档位 = 类别默认值 × 日波动 / ref；没有默认值时用 BASE_DAY。没有日波动、或和 default 相差不到 10% 时照用 default。"""
    if not sigma_pct:
        return list(default)
    out = sorted({nice_pct(v * sigma_pct / ref) for v in (default or BASE_DAY)})
    return list(default) if _close(out, sorted(default)) else out


def class_rules(settings: Settings, asset_class: str) -> tuple[dict[int, float], list[float], float]:
    """某类资产默认的（急涨急跌, 全天累计, 这组默认值针对的日波动）：assets.yaml 的 defaults 覆盖内置默认，
    都没写时用 monitor.price。"""
    from tickersentinel.config import AlertRules, default_class_rules

    r = default_class_rules().get(asset_class, AlertRules()).merged(settings.universe.classes.get(asset_class))
    p = settings.monitor.price
    return dict(r.rapid_pct or p.min_move_pct), list(r.day_pct or p.day_pct), float(r.ref_sigma_pct or REF_SIGMA)


def basket_series(r: pd.DataFrame, members: list[str]) -> pd.Series | None:
    cols = [m for m in members if m in r]
    return r[cols].mean(axis=1, skipna=True) if len(cols) >= 2 else None


# ───────── 大模型给候选 ─────────

SCOPE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "theme_key": {"type": "string"},
        "theme_name": {"type": "string"},
        "focus": {"type": "string"},
        "company_queries": {"type": "object", "additionalProperties": {"type": "array", "items": {"type": "string"}}},
        "us_peers": {"type": "array", "items": {"$ref": "#/$defs/item"}},
        "overseas": {"type": "array", "items": {"$ref": "#/$defs/item"}},
        "catalysts": {"type": "array", "items": {"$ref": "#/$defs/item"}},
        "news": {"type": "object", "additionalProperties": {"type": "array", "items": {"type": "string"}}},
        "indicators": {"type": "array", "items": {"$ref": "#/$defs/item"}},
    },
    "required": ["theme_key", "theme_name", "focus", "company_queries", "us_peers", "overseas", "catalysts", "news",
                 "indicators"],
    "additionalProperties": False,
    "$defs": {"item": {"type": "object", "properties": {"symbol": {"type": "string"}, "name": {"type": "string"},
                                                        "why": {"type": "string"}},
                       "required": ["symbol", "name", "why"], "additionalProperties": False}},
}


def scope_system() -> str:
    return """你是买方研究助理，帮读者给一只新持仓整理“联动圈”：哪些股票和它同涨同跌、美股休市时哪些海外股票能提前反映它的消息、
哪些公司（大客户、供应商）的财报会牵动它、检索新闻用什么关键词、有哪些和这个板块相关的市场指标。
只输出一个 JSON 对象，字段：
- theme_key：板块的英文短名，小写字母 / 数字 / 连字符，如 optics、gold-miners；theme_name：中文板块名（4–8 字）；focus：一句话描述领域；
- company_queries：这家公司自己的新闻检索词，按语言 {"en": [...], "zh": [...]}（必要时加 "ko" / "ja"），每种 1–2 个；
- us_peers：美股同业（主营相同、通常同涨同跌），最多 8 个；
- overseas：海外上市的对应公司或核心同业（同一条产业链、在亚洲 / 欧洲交易），代码用 Yahoo Finance 格式并带交易所后缀
  （.SS / .SZ 上海深圳、.HK 香港、.KS 首尔、.T 东京、.TW / .TWO 台湾、.AS 阿姆斯特丹……），最多 5 个；
- catalysts：大客户、主要供应商或风向标公司（平时不一定同涨同跌，但财报会影响这个板块），美股代码，最多 8 个；
- news：板块新闻检索词，按语言，每种 2–3 个，写行业关键词，不要写公司名；
- indicators：和这个板块特别相关的市场指标（汇率、商品期货、海外指数），Yahoo Finance 代码，最多 2 个，没有就给空列表。
每一项都给 symbol、name（中文或常用名）、why（一句话理由）。只给你有把握的代码，不确定就不要给：系统会用行情数据逐个验证，
查不到或不联动的会被剔除。不要编造。
如果你有联网检索工具，用它核实不确定的代码和最新情况（新上市、改名、拆分、合并、退市），检索次数够用就行。
最后只输出那一个 JSON 对象，不要输出其他文字。"""


async def llm_candidates(llm: "LLMClient", symbol: str, profile: Profile, similar: list[str],
                         themes: dict[str, str]) -> dict[str, Any]:
    from tickersentinel.llm.client import extract_json

    lines = [f"新持仓：{symbol}（{profile.name}）",
             f"Yahoo 分类：{profile.sector or '—'} / {profile.industry or '—'}；交易所：{profile.exchange or '—'}",
             f"Yahoo“相似股票”：{'、'.join(similar) or '—'}"]
    if themes:
        lines.append("读者已有的板块（若它明显属于其中一个，theme_key 就写那个）：" + "；".join(f"{k} = {v}" for k, v in themes.items()))
    # 线上模型联网核实代码（Claude 联网时不能同时强制 JSON 格式，靠提示词 + extract_json；本地模型照样用 json_schema）
    res = await llm.complete("scope", system=scope_system(), prompt="\n".join(lines), json_schema=SCOPE_SCHEMA,
                             web_search=True, max_searches=8)
    data = extract_json(res.text, required_key="theme_key")
    if data is None:
        raise SourceError("大模型没有返回预期的 JSON")
    return data


_ITEM = {"symbol": "代码", "name": "名称", "why": "一句话理由"}
CANDIDATES_SKELETON = {
    "theme_key": "英文短名", "theme_name": "中文板块名", "focus": "一句话描述领域",
    "company_queries": {"en": ["..."], "zh": ["..."]}, "us_peers": [_ITEM], "overseas": [_ITEM], "catalysts": [_ITEM],
    "news": {"en": ["..."], "zh": ["..."]}, "indicators": [_ITEM],
}


async def candidates_brief(settings: Settings, symbol: str) -> str:
    """tsen scope prompt：在 Claude Code 里准备候选需要的上下文（Yahoo 资料、相似股票、已有主题）、要求和输出格式。"""
    import json

    u = settings.universe
    sym = symbol.strip().upper()
    try:
        profile = await yahoo_profile(sym)
    except SourceError:
        profile = Profile(sym, sym)
    try:
        similar = await yahoo_similar(sym)
    except SourceError:
        similar = []
    themes = {k: f"{t.name}（同业 {'、'.join(t.peers) or '—'}；夜间信号 {'、'.join(t.overnight) or '—'}）"
              for k, t in u.themes.items() if k != DEFAULT_THEME}
    lines = [f"# 给 {sym} 准备联动候选", "",
             f"- Yahoo：{profile.name}；{profile.sector or '—'} / {profile.industry or '—'}；{profile.exchange or '—'}",
             f"- Yahoo“相似股票”：{'、'.join(similar) or '—'}",
             "- 已有主题：" + ("；".join(f"{k} = {v}" for k, v in themes.items()) or "（还没有）"), "",
             "## 要求", scope_system(), "",
             "## 输出格式（字段说明见上；列表每一项都是 {symbol, name, why}）", json.dumps(CANDIDATES_SKELETON, ensure_ascii=False), "",
             "## 写到这里（UTF-8 的 JSON 文件，只放这一个对象；30 天内有效）", str(candidates_path(settings, sym)),
             f"然后运行 tsen scope add {sym} 预览方案（不是交互终端时只预览、不写入），确认后加 --yes 写入；"
             f"飞书里发 /scope add {sym} 也会用这份候选。候选只是提名：代码要能在 Yahoo 查到，同业要过残差相关、"
             "夜间信号要过偏相关，最后由数据决定。"]
    return "\n".join(lines)


# ───────── 出方案 ─────────


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:30] or "theme"


def _items(data: dict, key: str) -> list[dict]:
    return [x for x in data.get(key) or [] if isinstance(x, dict) and str(x.get("symbol", "")).strip()]


def _queries(d: Any) -> dict[str, list[str]]:
    if not isinstance(d, dict):
        return {}
    out = {}
    for lang, qs in d.items():
        if isinstance(qs, list):
            vals = [str(q).strip() for q in qs if str(q).strip()][:3]
            if vals and lang in ("en", "zh", "ko", "ja"):
                out[lang] = vals
    return out


def _indicator(sym: str, name: str, group: str, key: str) -> dict[str, Any]:
    up = sym.upper()
    kind, ref = ("level", "24h") if up.endswith("=X") else ("price", "24h") if up.endswith("=F") else ("level", "close")
    return {"key": key, "name": name or sym, "symbol": sym, "group": group, "kind": kind, "ref": ref}


async def propose(settings: Settings, symbol: str, prices: PriceSource, *, llm: "LLMClient | None" = None,
                  theme: str | None = None, now: datetime | None = None, candidates: dict | None = None,
                  candidates_file: "str | None" = None) -> Proposal:
    """candidates：别处（Claude Code 用订阅额度）准备好的候选，格式同 SCOPE_SCHEMA；不给时找 data/scope_candidates/<代码>.json，
    再没有才问 llm.tasks.scope 配的大模型（默认不配：不调用任何 API）。候选都只是提名，最后由数据打分决定。"""
    u = settings.universe
    sym = symbol.strip().upper()
    if not sym:
        raise ScopeError("需要一个代码，例如 LITE")
    if sym in u.symbols:
        raise ScopeError(f"{sym} 已经是持仓（tsen scope 查看现在的范围）")
    if theme is not None and theme not in u.themes:
        have = "、".join(k for k in u.themes if k != DEFAULT_THEME)
        raise ScopeError(f"没有主题 {theme}；现有：{have}")
    try:
        names = await yahoo_names([sym])
    except SourceError as exc:
        raise ScopeError(f"Yahoo 暂时查不到 {sym} 的资料（{exc}），稍后再试") from exc
    if sym not in names:
        raise ScopeError(f"Yahoo 上查不到 {sym}：代码对吗？海外股票要带交易所后缀，如 300308.SZ、000660.KS")
    try:
        profile = await yahoo_profile(sym)
    except SourceError:
        profile = names[sym]
    profile.name = names[sym].name
    cls = _infer_class(sym)
    notes: list[str] = []
    sources: dict[str, list[str]] = {}
    prop = Proposal(symbol=sym, name=profile.name, asset_class=cls, theme_key=theme or "", theme_name="", new_theme=False,
                    created=(now or utcnow()).isoformat())
    if cls != "us_equity":  # 期货、加密货币、海外股票：只加资产，不找联动（板块拆分只对美股）
        prop.theme_key = theme or DEFAULT_THEME
        prop.theme_name = u.themes[prop.theme_key].name
        prop.notes.append("不是美股：只按类别默认的警报标准盯盘，不做板块拆分；需要联动时可以手工写进某个主题")
        prop.queries = {"en": [profile.name]}
        return prop

    # 1) 已有主题的成员（美股），用来判断能不能并进去
    theme_members: dict[str, list[str]] = {}
    for key, th in u.themes.items():
        if key == DEFAULT_THEME:
            continue
        members = [s for s in [*u.theme_holdings(key), *th.peers] if is_us_symbol(s) and s != sym]
        if len(members) >= 2:
            theme_members[key] = list(dict.fromkeys(members))

    # 2) 候选
    async def safe(name: str, coro) -> list[str]:
        try:
            got = await coro
            sources[name] = list(got)
            return list(got)
        except SourceError as exc:
            notes.append(f"{name} 没取到（{exc}）")
            return []

    similar = [x for x in await safe("Yahoo 相似股票", yahoo_similar(sym)) if x.upper() != sym]
    industry = [x for x in await safe("Yahoo 行业龙头", yahoo_industry_top(profile.industry_key)) if x.upper() != sym] \
        if profile.industry_key else []
    for k in list(sources):
        sources[k] = [x for x in sources[k] if x.upper() != sym]
    fh_key = settings.secrets.get("finnhub_api_key")
    finnhub = await safe("Finnhub 同业", finnhub_peers(sym, fh_key)) if fh_key else []
    data: dict[str, Any] = {}
    if candidates is None:
        candidates = load_candidates(settings, sym, candidates_file)
        stale = candidates_path(settings, sym)
        if candidates is None and not candidates_file and stale.exists():
            import time

            age = (time.time() - stale.stat().st_mtime) / 86400
            notes.append(f"{stale.name} 是 {age:.0f} 天前准备的（超过 {CANDIDATES_MAX_AGE_DAYS} 天，联动可能变了），这次没用上："
                         f"在 Claude Code 里重新准备，或用 --candidates 明确指定这个文件")
    if candidates is not None:
        data = candidates
        sources["Claude Code 准备的候选"] = [x["symbol"] for k in ("us_peers", "overseas", "catalysts") for x in _items(data, k)]
    elif llm is not None and llm.available("scope"):
        try:
            data = await llm_candidates(llm, sym, profile, similar, {k: u.themes[k].name for k in theme_members})
            sources["大模型"] = [x["symbol"] for k in ("us_peers", "overseas", "catalysts") for x in _items(data, k)]
        except Exception as exc:
            notes.append(f"大模型没给出候选（{type(exc).__name__}: {exc}），只用 Yahoo / Finnhub 的候选")
    else:
        notes.append(f"这次只用了 Yahoo / Finnhub / 已有主题的候选。想让大模型提名海外对应公司、大客户：在 Claude Code 里说"
                     f"“给 {sym} 准备联动候选”（项目技能 add-holding，用订阅额度，不花 API 的钱），它会写好 "
                     f"data/scope_candidates/{sym}.json，再运行一次就会用上")
    llm_names = {str(x["symbol"]).strip().upper(): str(x.get("name") or "") for k in ("us_peers", "overseas", "catalysts", "indicators")
                 for x in _items(data, k)}
    llm_why = {str(x["symbol"]).strip().upper(): str(x.get("why") or "") for k in ("us_peers", "overseas", "catalysts")
               for x in _items(data, k)}
    us_c = list(dict.fromkeys(s.upper() for s in [*similar, *finnhub, *industry, *(x["symbol"] for x in _items(data, "us_peers"))]
                              if s and is_us_symbol(s) and s.upper() != sym))
    # 海外候选：大模型给的 + 相似股票里的海外代码 + 已有主题里整理过的海外代码（大模型每次给的不一样，这些是稳定的候选）
    curated = [c for t in u.themes.values() for c in [*t.overnight, *t.peers] if not is_us_symbol(c)]
    os_c = list(dict.fromkeys(s.strip().upper() for s in [*(x["symbol"] for x in _items(data, "overseas")), *similar, *curated]
                              if s and not is_us_symbol(s) and not s.upper().endswith(("=X", "=F")) and not s.startswith("^")))
    cat_c = list(dict.fromkeys(str(x["symbol"]).strip().upper() for x in _items(data, "catalysts")
                               if is_us_symbol(str(x["symbol"])) and str(x["symbol"]).strip().upper() not in (sym, *u.symbols)))
    # 板块指标只要汇率（=X）、期货（=F）、指数（^）：股票和 ETF 已经在同业 / 基准里了
    ind_c = [x for x in _items(data, "indicators")
             if str(x["symbol"]).strip().upper().endswith(("=X", "=F")) or str(x["symbol"]).strip().startswith("^")][:MAX_INDICATORS]

    # 3) 代码核对：Yahoo 上查不到的去掉（大模型可能记错代码）
    want = list(dict.fromkeys([*us_c, *os_c, *cat_c, *(str(x["symbol"]).strip() for x in ind_c)]))
    try:
        found = await yahoo_names(want) if want else {}
    except SourceError as exc:
        notes.append(f"候选代码没法核对（{exc}），按原样打分")
        found = {s: Profile(s, llm_names.get(s, s)) for s in want}
    missing = [s for s in want if s not in found and s.upper() not in found]
    if missing:
        notes.append("Yahoo 上查不到、已去掉：" + "、".join(missing))
    us_c = [s for s in us_c if s in found and found[s].quote_type not in ("ETF", "MUTUALFUND", "INDEX")]
    os_c = [s for s in os_c if s in found]
    cat_c = [s for s in cat_c if s in found]

    # 4) 行情
    benches = list(dict.fromkeys([*BENCH_CANDIDATES, *(t.benchmark for t in u.themes.values() if not t.basket)]))
    member_syms = [s for ms in theme_members.values() for s in ms]
    syms = list(dict.fromkeys([sym, MARKET, *benches, *us_c, *os_c, *cat_c, *member_syms]))
    try:
        daily = await prices.daily_bars(syms, lookback_days=LOOKBACK_DAYS)
    except Exception as exc:
        raise ScopeError(f"取日线失败，没法打分：{type(exc).__name__}: {exc}") from exc
    r = log_returns(daily)
    if sym not in r or r[sym].dropna().shape[0] < MIN_DAYS:
        raise ScopeError(f"{sym} 的历史太短（不到 {MIN_DAYS} 个交易日），打不了分；可以先手工写进某个主题")
    prop.sigma_pct = daily_sigma(r[sym])

    def label(s: str) -> str:
        """配置里有名称就用配置的；否则大模型的名称 +（Yahoo 登记的名称），两者对不上时一眼能看出代码可能记错了。"""
        if u.label(s) != s:
            return u.label(s)
        yname = short_name(found[s].name) if s in found else ""
        lname = (llm_names.get(s) or "").strip()
        if lname and yname and lname.lower() not in yname.lower() and yname.lower() not in lname.lower():
            return f"{lname}（{yname}）"
        return lname or yname or s

    # 5) 已有主题：平均残差相关
    for key, members in theme_members.items():
        vals = [c for m in members if (c := residual_corr(r, sym, m)[0]) is not None]
        if vals:
            prop.join_scores[key] = float(np.mean(vals))
    best_theme = max(prop.join_scores, key=prop.join_scores.get) if prop.join_scores else None
    llm_key = _slug(str(data.get("theme_key") or "")) if data.get("theme_key") else ""
    if theme is None and best_theme and prop.join_scores[best_theme] >= JOIN_MIN:
        theme = best_theme
    if theme is None and llm_key in u.themes and llm_key != DEFAULT_THEME:
        theme = llm_key
        notes.append(f"大模型认为它属于已有主题 {llm_key}，但和主题成员的联动不强"
                     f"（平均残差相关 {prop.join_scores.get(llm_key, float('nan')):.2f}），请留意")
    prop.queries = _queries(data.get("company_queries")) or {"en": [profile.name.split(",")[0]]}

    if theme is not None:  # 并入已有主题：只加持仓，主题文件不动
        th = u.themes[theme]
        prop.theme_key, prop.theme_name, prop.new_theme = theme, th.name, False
        prop.join_score = prop.join_scores.get(theme)
        prop.benchmark = th.benchmark
        for m in theme_members.get(theme, []):
            c, n = residual_corr(r, sym, m)
            prop.peers.append(Pick(m, label(m), c, n, note="主题里已有"))
        d_rapid, d_day, ref = class_rules(settings, prop.asset_class)
        prop.day_pct = suggest_day(prop.sigma_pct, d_day, ref)
        prop.rapid = suggest_rapid(prop.sigma_pct, d_rapid, ref)
        extra = [s for s in us_c if s not in th.peers and s not in u.symbols]
        strong = [(s, c) for s in extra if (c := residual_corr(r, sym, s)[0]) is not None and c >= PEER_MIN]
        if strong:
            notes.append("主题里没有、但和它联动很强的：" + "、".join(f"{s} {c:.2f}" for s, c in strong)
                         + "（需要的话手工加进主题的 peers）")
        prop.notes, prop.sources = notes, sources
        return prop

    # 6) 新建主题
    key = llm_key or _slug(profile.industry_key or profile.industry or sym)
    if key in u.themes:
        key = f"{key}-{sym.lower()}"
    prop.theme_key, prop.new_theme = key, True
    prop.theme_name = str(data.get("theme_name") or "").strip() or (profile.industry or sym)
    prop.focus = str(data.get("focus") or "").strip()
    scored = []
    for s in us_c:
        c, n = residual_corr(r, sym, s)
        if c is not None:
            scored.append(Pick(s, label(s), c, n, note=llm_why.get(s, "")))
    scored.sort(key=lambda p: -(p.score or 0))
    llm_peers = {str(x["symbol"]).strip().upper() for x in _items(data, "us_peers")}
    demoted: list[Pick] = []
    for p in scored:
        if p.score >= PEER_MIN and len(prop.chosen("peers")) < MAX_PEERS:
            prop.peers.append(p)
        elif p.score >= PEER_WEAK:
            p.chosen = False
            reason = "名额已满" if p.score >= PEER_MIN else "联动偏弱"
            p.note = reason + (f"；{p.note}" if p.note else "")
            prop.peers.append(p)
        elif p.symbol in llm_peers:  # 大模型认为是同业、日常却不同涨同跌（例如 NVDA 之于 AMD）：财报仍然重要 → 相关公司
            p.note = f"日常联动弱（{p.score:.2f}），按相关公司处理" + (f"；{p.note}" if p.note else "")
            demoted.append(p)
        else:
            p.chosen = False
            prop.rejected.append(p)
    # 板块基准
    for b in benches:
        if b in r and b != sym:
            v, _ = r_squared(r[sym], r[b])
            if v is not None:
                prop.bench_scores[b] = v
    chosen_us = [p.symbol for p in prop.chosen("peers")]
    bs = basket_series(r, chosen_us)
    if bs is not None:
        v, _ = r_squared(r[sym], bs)
        if v is not None:
            prop.bench_scores["basket"] = v
    etfs = {k: v for k, v in prop.bench_scores.items() if k != "basket"}
    best_etf = max(etfs, key=etfs.get) if etfs else "SPY"
    if "basket" in prop.bench_scores and prop.bench_scores["basket"] - etfs.get(best_etf, 0) >= BASKET_GAIN:
        prop.benchmark = "basket"
    else:
        prop.benchmark = best_etf
    # 夜间信号
    night_sig: list[float] = []
    for s in os_c:
        pc, follow, n = overnight_signal(daily, sym, s)
        name = label(s)
        if pc is None:
            prop.rejected.append(Pick(s, name, None, n, note=f"样本不足（{n} 天）", chosen=False))
            continue
        pick = Pick(s, name, pc, n, note=llm_why.get(s, ""))
        if follow is not None:
            pick.note = (pick.note + "；" if pick.note else "") + f"跟随美股前一天 {follow:+.2f}"
        if pc >= OVERNIGHT_MIN and len(prop.chosen("overnight")) < MAX_OVERNIGHT:
            prop.overnight.append(pick)
            if (sg := daily_sigma(r[s])) is not None:
                night_sig.append(sg)
        else:
            pick.chosen = False
            prop.rejected.append(pick)
    if night_sig:  # 夜间信号（或它们的篮子）的阈值按它们自己的日波动换算；和海外股票默认一样就不写
        d_rapid, d_day, ref = class_rules(settings, "foreign_equity")
        sg = float(np.mean(night_sig))
        alert = {k: v for k, v, d in (("rapid_pct", suggest_rapid(sg, d_rapid, ref), d_rapid),
                                      ("day_pct", suggest_day(sg, d_day, ref), d_day)) if v != d}
        prop.overnight_alert = alert or None
    # 催化剂：不按相关性筛（残差相关只供参考）。日常联动弱、但大模型认为是大客户 / 供应商的放在这里，不再列进“没选上”
    for s in cat_c[:MAX_CATALYSTS]:
        if s in chosen_us:
            continue
        c, n = residual_corr(r, sym, s)
        prop.catalysts.append(Pick(s, label(s), c, n, note=llm_why.get(s, "")))
    for p in demoted:
        if all(c.symbol != p.symbol for c in prop.catalysts) and len(prop.catalysts) < MAX_CATALYSTS:
            prop.catalysts.append(p)
    cats = {c.symbol for c in prop.catalysts}
    prop.rejected = [x for x in prop.rejected if x.symbol not in cats]
    prop.peers = [x for x in prop.peers if x.chosen or x.symbol not in cats]
    prop.news = _queries(data.get("news")) or ({"en": [profile.industry]} if profile.industry else {})
    for i, x in enumerate(ind_c):
        s = str(x["symbol"]).strip()
        if s in found:
            prop.indicators.append(_indicator(s, str(x.get("name") or s), prop.theme_name, f"ind{i + 1}"))
    d_rapid, d_day, ref = class_rules(settings, prop.asset_class)
    prop.day_pct = suggest_day(prop.sigma_pct, d_day, ref)
    prop.rapid = suggest_rapid(prop.sigma_pct, d_rapid, ref)
    short =[p for p in [*prop.peers, *prop.overnight] if p.chosen and p.n and p.n < 120]
    if short:
        notes.append("样本偏短（不到 120 天）：" + "、".join(f"{p.symbol} {p.n} 天" for p in short))
    if not prop.chosen("peers"):
        notes.append(f"没找到联动够强的美股同业（残差相关 ≥ {PEER_MIN:g}）：板块基准退回 ETF，同期对比为空")
    prop.notes, prop.sources = notes, sources
    return prop


# ───────── 排版 ─────────


def _picks(ps: list[Pick], fmt: str = "{:.2f}") -> str:
    return "｜".join(f"{p.name + ' ' if p.name and p.name != p.symbol else ''}{p.symbol}"
                     + (f" {fmt.format(p.score)}" if p.score is not None else "") for p in ps)


def alert_text(alert: dict[str, Any]) -> str:
    """{"rapid_pct": {...}, "day_pct": [...]} → “急涨急跌 5分钟 1.9%、…；全天 6/10/15%”。"""
    parts = []
    if alert.get("rapid_pct"):
        parts.append("急涨急跌 " + "、".join(f"{w}分钟 {v:g}%" for w, v in sorted(alert["rapid_pct"].items())))
    if alert.get("day_pct"):
        parts.append("全天 " + "/".join(f"{x:g}" for x in alert["day_pct"]) + "%")
    return "；".join(parts)


def render_proposal(p: Proposal, settings: Settings) -> str:
    u = settings.universe
    sig = f"，日波动 σ≈{p.sigma_pct:.1f}%" if p.sigma_pct else ""
    lines = [f"**新增持仓 {p.symbol}**（{p.name}{sig}）"]
    if p.asset_class != "us_equity":
        lines.append(f"- 归入：{p.theme_name}（{p.theme_key}）")
    elif not p.new_theme:
        js = f"，和主题成员剔除大盘后的平均相关 {p.join_score:.2f}" if p.join_score is not None else ""
        lines.append(f"- 并入已有主题「{p.theme_name}」（{p.theme_key}{js}）：基准、同业、夜间信号、相关公司都沿用主题里的")
        if p.peers:
            lines.append(f"- 和主题成员的联动：{_picks(p.peers)}")
    else:
        lines.append(f"- 新建主题「{p.theme_name}」（文件 config/themes/{p.theme_key}.yaml）" + (f"：{p.focus}" if p.focus else ""))
        others = "、".join([f"{k} {v:.2f}" for k, v in sorted(p.bench_scores.items(), key=lambda kv: -kv[1])
                           if k not in ("basket", p.benchmark)][:4])
        if p.benchmark == "basket":
            members = "/".join(x.symbol for x in p.chosen("peers") if is_us_symbol(x.symbol))
            lines.append(f"- 板块基准：同业篮子 {members}，R² {p.bench_scores.get('basket', 0):.2f}（{others}）")
        else:
            lines.append(f"- 板块基准：{p.benchmark}，R² {p.bench_scores.get(p.benchmark, 0):.2f}"
                         + (f"（篮子 {p.bench_scores['basket']:.2f}）" if "basket" in p.bench_scores else "")
                         + (f"；其他 {others}" if others else ""))
        if p.chosen("peers"):
            lines.append(f"- 同业：{_picks(p.chosen('peers'))}（剔除大盘 {MARKET} 后的残差相关，近 {PEER_WINDOW} 个交易日）")
        weak = [x for x in p.peers if not x.chosen]
        if weak:
            lines.append(f"- 默认不选（联动偏弱或名额已满，回复“加上 代码”可以加回）：{_picks(weak)}")
        if p.chosen("overnight"):
            lines.append(f"- 夜间信号：{_picks(p.chosen('overnight'))}（对 {p.symbol} 次日开盘跳空的偏相关，剔除前一天涨跌）")
            if p.overnight_alert:
                lines.append(f"  - 警报标准按它们的日波动换算：{alert_text(p.overnight_alert)}")
        if p.chosen("catalysts"):
            lines.append(f"- 相关公司（财报进事件日历、归因时对照）：{'、'.join(x.name or x.symbol for x in p.chosen('catalysts'))}")
        if p.rejected:
            more = f"；另有 {len(p.rejected) - 8} 只更弱" if len(p.rejected) > 8 else ""
            lines.append(f"- 没选上：{_picks(p.rejected[:8])}{more}")
        if p.news:
            lines.append("- 板块新闻检索词：" + "；".join(f"{k} {'、'.join(v)}" for k, v in p.news.items()))
        if p.indicators:
            lines.append("- 板块指标：" + "、".join(f"{i['name']} {i['symbol']}" for i in p.indicators))
    if p.queries:
        lines.append("- 公司新闻检索词：" + "；".join(f"{k} {'、'.join(v)}" for k, v in p.queries.items()))
    rules = settings.rules_for(p.symbol)
    changed = {k: v for k, v, d in (("rapid_pct", p.rapid, rules.min_move_pct), ("day_pct", p.day_pct, rules.day_pct)) if v and v != d}
    if changed and p.sigma_pct:
        ref = class_rules(settings, p.asset_class)[2]
        lines.append(f"- 警报标准（按日波动 {p.sigma_pct:.1f}% 换算，默认值按 {ref:g}% 定）：{alert_text(changed)}")
    if p.sources:
        lines.append("- 候选来源：" + "；".join(f"{k} {'、'.join(v[:8])}" for k, v in p.sources.items() if v))
    for n in p.notes:
        lines.append(f"- 注意：{n}")
    return "\n".join(x for x in lines if x)


__all__ = ["Pick", "Proposal", "ScopeError", "propose", "render_proposal", "residual_corr", "overnight_signal", "short_name",
           "load_candidates", "candidates_path", "candidates_brief",
           "r_squared", "suggest_rapid", "suggest_day", "log_returns", "basket_series", "alert_text"]
