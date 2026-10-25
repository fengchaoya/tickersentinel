"""跨模块共享的数据结构。

约定：
- 所有时间为带时区的 UTC datetime；
- 名称以 _pct 结尾的字段均为“百分数”（-2.8 表示 -2.8%）。
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from typing import Any, Literal

from tickersentinel.textutil import canonical_url

SECTOR = "SECTOR"  # 新闻的伪代码：行业/板块新闻；按主题区分时写成 SECTOR:<主题>（见 sector_tag）


def sector_tag(theme: str) -> str:
    """某个主题的板块新闻标签（NewsItem.symbols 里用）。"""
    return f"{SECTOR}:{theme}"


def is_sector(tag: str) -> bool:
    return tag == SECTOR or tag.startswith(f"{SECTOR}:")


def sector_theme(tag: str) -> str | None:
    """SECTOR:optics → optics；不带主题的 SECTOR → None。"""
    return tag.split(":", 1)[1] if tag.startswith(f"{SECTOR}:") else None

# rapid 急涨急跌（相对最近 5/15/30/60 分钟内的最高 / 最低点）｜day 全天累计（从昨收与日内高 / 低点里更远的那个算起）｜
# idio 个股特异｜level：旧版“关口”（较昨收），只出现在 2026-10-24 以前的记录里
Rule = Literal["rapid", "day", "idio", "level"]
RULE_PRIORITY: dict[str, int] = {"rapid": 0, "idio": 1, "day": 2, "level": 3}


def _now_pct(anchor_pct: float, move_pct: float) -> float:
    """起点较参照价 anchor_pct%，从起点又走了 move_pct 个点（按参照价的百分点）：现价较参照价多少。"""
    return anchor_pct + move_pct


@dataclass(frozen=True)
class NewsItem:
    title: str
    url: str
    published_at: datetime
    source: str = ""  # 媒体名
    provider: str = ""  # 数据来源：finnhub / google_news / sec_edgar ...
    symbols: tuple[str, ...] = ()  # 相关代码；行业新闻为 (SECTOR:<主题>,)，泛泛的关键词检索为 (SECTOR,)
    summary: str = ""
    lang: str = "en"
    kind: Literal["news", "filing"] = "news"

    @property
    def key(self) -> str:
        return hashlib.sha1(canonical_url(self.url).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Trigger:
    """一条被触发的异动规则。"""

    rule: Rule
    direction: int  # +1 上涨 / -1 下跌
    # rapid / day：从起点（高点或低点）到现价走了几个点（按参照价的百分点：“+2% 跌到 -1%”是 -3）；
    # idio：窗口异常收益；level（旧）：较昨收；旧的 rapid 记录：窗口端点涨跌幅
    move_pct: float
    window_minutes: int | None = None  # rapid：哪一档窗口（5/15/30/60）；idio：窗口
    z: float | None = None
    level_pct: float | None = None  # day：本次越过的最高一档（正数）；level（旧）：本次突破的最高关口
    # rapid / day 的起点：高点（下跌时）或低点（上涨时）。day 的起点是参照价（昨收）本身时 anchor_ts 为 None
    anchor_ts: datetime | None = None
    anchor_pct: float | None = None  # 起点较参照价的涨跌（%）
    minutes: int | None = None  # 从起点到现在用了几分钟

    @property
    def context_window(self) -> int | None:
        """作为标题时，拆分上下文用的窗口（分钟）：rapid / day 为从起点到现在（day 从昨收算起时为 None，即较昨收）；
        idio 为触发窗口；旧的关口为 None。"""
        if self.rule == "rapid":
            return self.minutes or self.window_minutes
        if self.rule == "day":
            return self.minutes if self.anchor_ts is not None else None
        if self.rule == "level":
            return None
        return self.window_minutes

    @property
    def _unit(self) -> str:
        """有参照价时幅度是“个点”（参照价的百分点）；取不到参照价时是相对起点的百分比。"""
        return " 个点" if self.anchor_pct is not None else "%"

    def _path(self, ref: str) -> str:
        """“较昨收 -1.8% → -4.7%”：起点和现价各较参照价多少。"""
        if self.anchor_pct is None:
            return ""
        return f"{ref} {self.anchor_pct:+.1f}% → {_now_pct(self.anchor_pct, self.move_pct):+.1f}%"

    def describe(self, ref: str = "较昨收") -> str:
        """ref：参照价（盘后为“较今日收盘”、期货为“较上一交易时段”，见 Alert.ref_label）。"""
        up = self.direction > 0
        if self.rule == "rapid":
            z = f"z={self.z:+.1f}" if self.z is not None else ""
            if self.minutes is None:  # 旧记录：固定窗口的端点涨跌
                return f"{self.window_minutes}分钟内{'急涨' if up else '急跌'} {self.move_pct:+.1f}%（{z}）"
            extra = "，".join(x for x in (self._path(ref), z) if x)
            return (f"{self.minutes}分钟内从{'低点反弹' if up else '高点回落'} {abs(self.move_pct):.1f}{self._unit}"
                    + (f"（{extra}）" if extra else ""))
        if self.rule == "idio":
            return (
                f"{self.window_minutes}分钟个股特异{'走强' if up else '走弱'} {self.move_pct:+.1f}%"
                f"（剔除板块后，z={self.z:+.1f}）"
            )
        if self.rule == "day":
            if self.anchor_ts is None:
                return f"全天：{ref}{'涨' if up else '跌'}超 {self.level_pct:g}%（现 {self.move_pct:+.1f}%）"
            took = f"，用时 {self.minutes} 分钟" if self.minutes is not None else ""
            return (f"全天：从日内{'低点反弹' if up else '高点回落'}超 {self.level_pct:g}{self._unit}"
                    f"（{self._path(ref)}{took}）")
        sign = "+" if up else "-"
        return f"{'突破' if up else '跌破'}{ref} {sign}{self.level_pct:g}% 关口（现 {self.move_pct:+.1f}%）"

    def short(self, ref: str = "较昨收") -> str:
        up = self.direction > 0
        if self.rule == "rapid":
            if self.minutes is None:  # 旧记录
                return f"{self.window_minutes}分钟{'急涨' if up else '急跌'} {self.move_pct:+.1f}%"
            return f"{self.minutes}分钟{'急涨' if up else '急跌'} {abs(self.move_pct):.1f}{self._unit}"
        if self.rule == "idio":
            return f"个股特异{'走强' if up else '走弱'} {self.move_pct:+.1f}%"
        if self.rule == "day":
            if self.anchor_ts is None:
                return f"{ref}{'涨' if up else '跌'}超 {self.level_pct:g}%"
            return f"从日内{'低点反弹' if up else '高点回落'} {abs(self.move_pct):.1f}{self._unit}"
        sign = "+" if up else "-"
        return f"{'突破' if up else '跌破'} {sign}{self.level_pct:g}% 关口"


REF_LABELS = {"post": "较今日收盘", "cme": "较上一交易时段", "utc": "较 UTC 0 点"}
SEGMENT_TAGS = {"pre": "盘前", "post": "盘后", "cme": "期货", "utc": "", "exchange": ""}


@dataclass(frozen=True)
class SplitContext:
    """某一窗口口径下的拆分上下文，字段与 Alert 上的同名字段一一对应。"""

    window_minutes: int | None  # None 表示“较昨收（全天）”
    move_pct: float | None
    benchmark_move_pct: float | None
    abnormal_move_pct: float | None
    volume_ratio: float | None
    peers_move_pct: dict[str, float] = field(default_factory=dict)


@dataclass
class Alert:
    """一次（经过冷却过滤前/后的）价格异动。一个标的在一次检测中最多产生一个 Alert。"""

    symbol: str
    ts: datetime  # 最新 K 线时间（UTC）
    price: float
    prev_close: float | None
    triggers: list[Trigger]  # 按 RULE_PRIORITY 排序，第一个为标题
    # —— 拆分上下文（以标题触发的口径为准；从昨收算的全天累计用“较昨收”）——
    window_minutes: int | None  # None 表示“较昨收（全天）”
    move_pct: float | None
    benchmark_move_pct: float | None
    beta: float | None
    abnormal_move_pct: float | None  # ≈ 原始涨跌 − β × 板块涨跌
    volume_ratio: float | None  # 窗口成交量 / 历史同时段中位数
    peers_move_pct: dict[str, float] = field(default_factory=dict)
    is_escalation: bool = False
    # 发生在哪一段：regular 盘中 / pre 盘前 / post 盘后（参照价是当天收盘）/ cme 期货 / utc 加密货币 / exchange 海外交易所
    segment: str = "regular"
    benchmark: str | None = None  # 拆分用的板块基准（ETF 代码，或同业篮子 basket:<主题>）；旧记录为空
    id: int | None = None  # 入库后的 ID
    # 检测时为每个触发窗口口径（Trigger.context_window）算好的拆分上下文。
    # 冷却过滤去掉部分触发、标题随之变化时，用它换成新标题的口径。不入库。
    contexts: dict[int | None, SplitContext] = field(default_factory=dict, repr=False, compare=False)
    # 推送时附上的宏观快照（sources.macro.MacroSnapshot）；入库的是它的 JSON（alerts.macro_json）
    macro: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.triggers = sorted(self.triggers, key=lambda t: RULE_PRIORITY[t.rule])

    def with_triggers(self, triggers: list[Trigger], **changes) -> "Alert":
        """换一组触发条件（如冷却过滤后），拆分上下文同步换成新标题的口径。

        没有对应口径的上下文时（手工构造或从数据库读出的 Alert）保留原值。
        """
        out = replace(self, triggers=triggers, **changes)
        ctx = self.contexts.get(out.headline.context_window)
        return replace(out, **asdict(ctx)) if ctx is not None else out

    @property
    def headline(self) -> Trigger:
        return self.triggers[0]

    @property
    def ref_label(self) -> str:
        """全天累计和“较 X”涨跌的参照：盘后是当天收盘，期货是上一交易时段结束（美东 17:00），其余是昨收。"""
        return REF_LABELS.get(self.segment, "较昨收")

    @property
    def segment_label(self) -> str:
        """推送标题里的时段标记：常规时段不标。"""
        return SEGMENT_TAGS.get(self.segment, "")

    @property
    def direction(self) -> int:
        return self.headline.direction

    @property
    def day_change_pct(self) -> float | None:
        if not self.prev_close:
            return None
        return (self.price / self.prev_close - 1) * 100


@dataclass
class Message:
    """与平台无关的推送消息。body 使用轻量 Markdown：**粗体**、- 列表、[文字](链接)。"""

    title: str
    body: str
    kind: Literal["brief", "alert", "attribution", "events", "macro", "info", "error"] = "info"
    reply_to: str | None = None  # 平台消息 ID：把归因挂在对应的异动消息下面


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float | None
    prev_close: float | None
    ts: datetime | None = None  # 最新成交时间（有分钟数据时）
    session: Literal["pre", "regular", "post", "closed"] | None = None
    day: date | None = None  # 只有日线时：该收盘价所属交易日

    @property
    def change_pct(self) -> float | None:
        if self.price is None or not self.prev_close:
            return None
        return (self.price / self.prev_close - 1) * 100


EventKind = Literal["earnings", "insider_sale", "lockup_expiry", "custom", "macro"]
# 事件来源的可靠程度（越小越可靠）：手工录入 > 公司 / 交易所 / 官方机构公告 > 按规则推算 > Finnhub > yfinance 估计
EVENT_SOURCE_RANK: dict[str, int] = {
    "config": 0, "sec_6k": 1, "jpx": 1, "sec_144": 1,
    "fed": 1, "bls": 1, "bea": 1, "census": 1, "treasury": 1, "boj": 1, "calendar_rules": 2,  # 宏观日程（sources/events_macro.py）
    "finnhub": 3, "yfinance": 4,
}


@dataclass(frozen=True)
class Event:
    """需要提前知道、到时提醒的事件：财报发布、内部人拟减持（Form 144）、锁定期到期、手工录入的日程……

    - day：事件日期。有精确时间时取 at 换算到美东的日期；只有日期时取数据源给的日期（该上市地的当地日期）。
    - at：精确时间（UTC）；只知道日期时为 None，时段写在 time_hint（"盘前" / "盘后" / "盘中"）。
    - key：稳定 ID。同一事件的日期被更新（估计 → 确认、改期）时 key 不变，据此识别“变更”；
      财报用 earnings_key()，按日历季度区分；Form 144 用 accession 编号。
    - confirmed：公司或交易所已经公布为 True；数据源只是估计（如按历史节奏推算）为 False。
    - 宏观事件（kind = "macro"）：symbol 是分组代码（FED / US / UST / BOJ / KR / MKT，见 sources/events_macro.MACRO_GROUPS），
      不是股票代码。
    """

    kind: EventKind
    symbol: str
    day: date
    title: str  # 中文一句话，不含日期，例如 “SK海力士 2026 年三季度业绩”
    key: str
    at: datetime | None = None
    time_hint: str = ""
    confirmed: bool = False
    source: str = ""  # yfinance / jpx / sec_6k / sec_144 / finnhub / config，可靠程度见 EVENT_SOURCE_RANK
    url: str = ""
    details: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)  # 结构化补充信息


def earnings_key(symbol: str, day: date) -> str:
    """财报事件的稳定 ID：同一代码、同一日历季度内只有一次财报，日期在季度内变动时 key 不变。"""
    return f"earnings:{symbol}:{day.year}Q{(day.month - 1) // 3 + 1}"
