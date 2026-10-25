"""配置加载：config/settings.yaml（非敏感参数，含基础宏观变量） + config/assets.yaml（盯哪些资产、各自的警报标准）
+ config/themes/*.yaml（每个板块的联动：基准、同业、夜间信号、相关公司、检索词、板块指标） + .env（API 密钥）。

- YAML 中写错字段名会直接报错（extra="forbid"），避免配置静默失效。
- 密钥只从环境变量读取，用 SecretStr 保存，打印/日志里不会泄露。
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, PrivateAttr, SecretStr, field_validator, model_validator

PROJECT_ROOT = Path(os.getenv("TICKERSENTINEL_HOME") or Path(__file__).resolve().parents[2])
DEFAULT_SETTINGS = PROJECT_ROOT / "config" / "settings.yaml"
ASSETS_FILE = "assets.yaml"  # 与 settings.yaml 放在同一个目录
THEMES_DIR = "themes"  # 主题（板块联动）目录：config/themes/<主题>.yaml

AssetClass = Literal["us_equity", "foreign_equity", "future", "crypto"]
ASSET_CLASS_NAMES = {"us_equity": "美股", "foreign_equity": "海外股票", "future": "期货", "crypto": "加密货币"}

# 非美股代码后缀：这些代码只在简报里展示，盘中不取分钟数据
FOREIGN_SUFFIXES = (".KS", ".KQ", ".T", ".HK", ".TW", ".TWO", ".SS", ".SZ", "=X")

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


# 后缀 → exchange_calendars 的交易所代码。深交所与上交所共用一个交易日历（exchange_calendars 没有 XSHE）；
# 台湾上柜股（.TWO）与上市股（.TW）同一个日历
FOREIGN_EXCHANGES = {".KS": "XKRX", ".KQ": "XKRX", ".T": "XTKS", ".HK": "XHKG", ".TWO": "XTAI", ".TW": "XTAI",
                     ".SS": "XSHG", ".SZ": "XSHG"}
EXCHANGE_NAMES = {"XKRX": "首尔", "XTKS": "东京", "XHKG": "香港", "XTAI": "台湾", "XSHG": "上海 / 深圳"}
BASKET_PREFIX = "basket:"  # 同业篮子（板块基准）的伪代码：basket:<主题>
NIGHT_PREFIX = "night:"  # 夜间信号篮子的伪代码：night:<主题>:<交易所>
PSEUDO_PREFIXES = (BASKET_PREFIX, NIGHT_PREFIX)  # 由成分算出来的伪代码，永远不去数据源取


def is_pseudo(symbol: str) -> bool:
    return symbol.startswith(PSEUDO_PREFIXES)


def pseudo_label(symbol: str) -> str:
    """没有配置可查时（多维表格同步）伪代码的可读写法：night:memory:XKRX → “memory 夜间篮子（首尔）”。"""
    parts = symbol.split(":")
    if symbol.startswith(NIGHT_PREFIX) and len(parts) == 3:
        return f"{parts[1]} 夜间篮子（{EXCHANGE_NAMES.get(parts[2], parts[2])}）"
    if symbol.startswith(BASKET_PREFIX) and len(parts) == 2:
        return f"{parts[1]} 同业篮子"
    return symbol


def exchange_of(symbol: str) -> str | None:
    """海外代码在哪个交易所（按后缀）；夜间篮子取伪代码里写的交易所。"""
    if symbol.startswith(NIGHT_PREFIX):
        return symbol.split(":")[-1] or None
    up = symbol.upper()
    return next((x for suffix, x in FOREIGN_EXCHANGES.items() if up.endswith(suffix)), None)


def is_us_symbol(symbol: str) -> bool:
    """是不是美股（含 ADR / ETF）代码。海外股票、汇率、期货（=F）、加密货币（-USD）、指数（^）、程序内部的篮子伪代码都不是：
    这些只有 yfinance 有（或者根本不去取），Alpaca 取不到。"""
    if is_pseudo(symbol):
        return False
    up = symbol.upper()
    return not (up.endswith(FOREIGN_SUFFIXES) or up.endswith(("=F", "-USD")) or up.startswith("^"))


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_hhmm(v: str) -> str:
    if not _HHMM.match(v):
        raise ValueError(f"时间格式应为 HH:MM（24小时制），收到 {v!r}")
    return v


class MacroIndicator(_Base):
    """一个宏观变量。数据来自 yfinance 的 1 分钟线（见 sources/macro.py）。"""
    key: str                      # 内部标识，如 us10y
    name: str                     # 显示名称
    symbol: str                   # yfinance 代码
    group: str = "其他"           # 推送里按组排成一行：利率 / 大宗商品 / 股指 / 其他
    kind: Literal["yield", "price", "level"] = "price"  # yield：数值是收益率（%），变化用 bp
    ref: Literal["close", "24h"] = "close"  # 涨跌口径：较上一个美股收盘 / 较 24 小时前（全天交易的期货、外汇、币）
    off_hours_symbol: str | None = None  # 美股休市时改看这个（如标普 → ES=F 期货），口径为“较美股收盘”
    off_hours_name: str | None = None
    in_alert: bool = True         # 是否附在异动推送里（/macro 查询总是全部显示）



# ───────────────────────── 标的 ─────────────────────────


class AlertRules(_Base):
    """一个资产（或一类资产）的警报标准。都可以省略：省略的项用资产类别的默认值，类别也没写的用 settings.yaml 的 monitor.price。"""
    # 急涨急跌：窗口（分钟）→ 下限（%）。最近这么多分钟里的最高点到现价跌了这么多（或最低点到现价涨了这么多），同时还要满足 z 值
    rapid_pct: dict[int, float] | None = None
    z: float | None = Field(default=None, gt=0)  # 急涨急跌的 z 值下限：涨跌 / (近期每分钟波动 × √窗口)
    # 全天累计：从昨收和今天的最高点里更高的那个算起跌了这么多（涨的方向对称），每一档推一次；创出新的高 / 低点后重新计。
    # 旧名 levels_pct（较昨收的“关口”）照样能读，按新规则解释
    day_pct: list[float] | None = Field(default=None, validation_alias=AliasChoices("day_pct", "levels_pct"))
    hours: Literal["regular", "extended"] | None = None  # 只对美股：regular 只盯常规时段；extended 盘前 + 盘中 + 盘后
    # 这一类资产的默认阈值是按多大的日波动（%）定的：tsen scope tune / scope add 按“默认值 × 它的日波动 / 这个数”换算
    ref_sigma_pct: float | None = Field(default=None, gt=0)

    @field_validator("rapid_pct")
    @classmethod
    def check_rapid(cls, v: dict[int, float] | None) -> dict[int, float] | None:
        if v is not None and (not v or any(w < 1 or pct <= 0 for w, pct in v.items())):
            raise ValueError("rapid_pct 应为 {窗口分钟: 涨跌幅下限%}，都为正数，至少一项")
        return v

    def merged(self, over: "AlertRules | None") -> "AlertRules":
        if over is None:
            return self
        return AlertRules(**{**self.model_dump(), **over.model_dump(exclude_none=True)})


def default_class_rules() -> dict[str, AlertRules]:
    """各类资产的默认警报标准（config/assets.yaml 的 defaults 可以覆盖）。
    期货（黄金、白银、原油）和加密货币的波动尺度和个股不同，所以分开设：黄金一天 ±2% 已经是大行情，个股 ±3% 很常见。"""
    return {
        "us_equity": AlertRules(hours="extended", ref_sigma_pct=5.0),  # 阈值用 monitor.price（按日波动约 5% 的股票定）
        "foreign_equity": AlertRules(ref_sigma_pct=5.0),
        "future": AlertRules(rapid_pct={5: 0.6, 15: 1.0, 30: 1.4, 60: 1.4}, day_pct=[1.5, 2.5, 4.0], ref_sigma_pct=2.0),
        "crypto": AlertRules(rapid_pct={5: 1.5, 15: 2.5, 30: 3.0, 60: 3.0}, day_pct=[5.0, 8.0, 12.0], ref_sigma_pct=5.0),
    }


BASKET = "basket"  # 主题的 benchmark 写成 basket：用这个主题里美股同业的等权篮子做板块基准
DEFAULT_THEME = "default"  # 没写 theme 的资产归入它（由 settings.yaml 的 universe.benchmark 等兜底字段生成）
_THEME_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _infer_class(symbol: str) -> str:
    up = symbol.upper()
    if up.endswith("=F"):
        return "future"
    if up.endswith("-USD"):
        return "crypto"
    return "us_equity" if is_us_symbol(symbol) else "foreign_equity"


def _name_map(v: Any) -> Any:
    """[代码, …] 或 {代码: 名称} 都接受，统一成 {代码: 名称}（名称可以为空）。"""
    if v is None:
        return {}
    if isinstance(v, (list, tuple)):
        return {str(x): "" for x in v}
    if isinstance(v, dict):
        return {str(k): ("" if x is None else x) for k, x in v.items()}
    return v


class Instrument(_Base):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    symbol: str
    name: str
    asset_class: AssetClass = Field(default="us_equity", alias="class")
    holding: bool = True  # False：只盯盘、不算持仓（例如夜里替持仓看首尔的 000660.KS），不进简报的持仓列表
    monitor: bool = True  # False：不做盘中 / 夜间异动检测（仍在简报、新闻、对话里）
    # 属于哪些主题（config/themes/<主题>.yaml）：板块基准、同业、夜间信号、相关公司、板块新闻都从主题来。第一个是主主题
    themes: list[str] = Field(default_factory=list, validation_alias=AliasChoices("theme", "themes"))
    related_to: list[str] = Field(default_factory=list)  # 只盯盘的资产对应哪些持仓：异动时按它们检索新闻、归因
    alert: AlertRules | None = None  # 这个资产自己的警报标准（覆盖类别默认）
    queries: dict[str, list[str]] = Field(default_factory=dict)  # 语言 → 检索词
    underlying: str | None = None  # 海外主上市代码，如 000660.KS
    adr_ratio: float | None = None  # 多少份 ADR = 1 股普通股
    fx_symbol: str | None = None  # 计算 ADR 隐含价格用的汇率代码，如 KRW=X
    # 补充背景（会写进所有提示词）：模型不知道的事实，例如新上市的证券、近期的公司变化。本地模型的知识往往过时
    note: str = ""
    # 由主题自动生成的只盯盘资产（主题的 overnight）为 "theme"；写在 assets.yaml 里的为 "assets"。不从配置读取
    origin: Literal["assets", "theme"] = Field(default="assets", exclude=True)

    @field_validator("themes", "related_to", mode="before")
    @classmethod
    def _as_list(cls, v: Any) -> Any:
        if v is None:
            return []
        return [v] if isinstance(v, str) else v


class OvernightCfg(_Base):
    """主题里的一个夜间信号：美股休市时在交易、能预示持仓下一个美股时段走势的海外标的（只盯盘，不持有）。"""

    name: str = ""
    alert: AlertRules | None = None  # 不写就用它所属资产类别（通常是 foreign_equity）的默认警报标准
    queries: dict[str, list[str]] = Field(default_factory=dict)


class Theme(_Base):
    """一个板块 / 题材的联动关系（config/themes/<key>.yaml），所有属于它的持仓共享。

    持仓在 assets.yaml 里用 theme 引用它；没有持仓引用的主题不生效（相当于主题库里的备用模块）。
    peers / catalysts / overnight 都可以写成列表或 {代码: 名称}。"""

    key: str = ""  # 文件名（不含 .yaml），加载时填
    name: str
    focus: str = ""  # 一句话描述，写进提示词（“专注……的买方研究助理”）
    benchmark: str = "SPY"  # 板块基准（拆分“板块 / 个股”的 β）：ETF / 指数代码，或 basket（美股同业的等权篮子）
    peers: dict[str, str] = Field(default_factory=dict)  # 同涨同跌的同业：同期对比、板块篮子、归因、事件日历
    overnight: dict[str, OvernightCfg] = Field(default_factory=dict)  # 夜间信号：自动成为只盯盘的资产
    catalysts: dict[str, str] = Field(default_factory=dict)  # 催化剂公司（客户、供应商……）：只收集财报等事件、归因时对照
    news: dict[str, list[str]] = Field(default_factory=dict)  # 板块新闻检索词：语言 → 检索词
    indicators: list[MacroIndicator] = Field(default_factory=list)  # 板块指标：附在这个板块的异动推送里（基础宏观变量在 settings.yaml）
    note: str = ""  # 补充背景，写进提示词
    # 同一交易所有 2 只以上夜间信号时合成一个等权篮子来触发：一波行情只推一条（几只常常同涨同跌）；
    # 某一只相对篮子单独动 ≥ overnight_single_pct 时（多半是这家公司自己的消息）仍单独推。false = 每只单独推
    overnight_basket: bool = True
    overnight_alert: AlertRules | None = None  # 夜间篮子的警报标准；不写就用海外股票类别的默认
    overnight_single_pct: float = Field(default=3.0, gt=0)  # 篮子成分“单独大动”的门槛：相对篮子的个股特异涨跌（%）

    @field_validator("peers", "catalysts", mode="before")
    @classmethod
    def _names(cls, v: Any) -> Any:
        return _name_map(v)

    @field_validator("overnight", mode="before")
    @classmethod
    def _overnight(cls, v: Any) -> Any:
        v = _name_map(v)
        return {k: ({"name": x} if isinstance(x, str) else x) for k, x in v.items()} if isinstance(v, dict) else v

    @property
    def basket(self) -> bool:
        return self.benchmark.strip().lower() == BASKET

    @property
    def basket_code(self) -> str:
        return f"{BASKET_PREFIX}{self.key}"

    @property
    def basket_members(self) -> list[str]:
        """篮子成分：同业里的美股（海外同业的交易时段不同，不能和美股逐分钟对齐）。"""
        return [p for p in self.peers if is_us_symbol(p)]

    def names(self) -> dict[str, str]:
        out = {k: v for k, v in {**self.catalysts, **self.peers}.items() if v}
        out.update({k: o.name for k, o in self.overnight.items() if o.name})
        out.update({i.symbol: i.name for i in self.indicators})
        return out


class Universe(_Base):
    """持仓与盯盘范围。资产清单在 config/assets.yaml，板块联动在 config/themes/；监控范围 = 持仓 × 它们的主题，
    其余（同业、夜间信号、相关公司、板块新闻、板块指标）全部由此推出：卖掉一个主题的最后一只持仓，这个主题就不再生效。"""

    model_config = ConfigDict(extra="forbid")

    watchlist: list[Instrument]  # assets.yaml 里的资产（含只盯盘的）；主题的夜间信号另外自动加入（见 instruments）
    classes: dict[str, AlertRules] = Field(default_factory=default_class_rules)  # 各类资产的默认警报标准

    @field_validator("classes")
    @classmethod
    def check_classes(cls, v: dict[str, AlertRules]) -> dict[str, AlertRules]:
        bad = [k for k in v if k not in ASSET_CLASS_NAMES]
        if bad:  # 写错类别名不能静默失效
            raise ValueError(f"assets.yaml 的 defaults 里没有这些资产类别：{bad}（可选：{list(ASSET_CLASS_NAMES)}）")
        return v
    themes: dict[str, Theme] = Field(default_factory=dict)  # config/themes/*.yaml
    benchmark: str = "SPY"  # 兜底：没写 theme 的资产用它拆分“板块 / 个股”
    display_names: dict[str, str] = Field(default_factory=dict)  # 其他代码在推送里显示的名称（主题里的代码在主题里写）

    @model_validator(mode="before")
    @classmethod
    def _legacy_fields(cls, data: Any) -> Any:
        """旧配置（settings.yaml 的 universe.focus / peers / sector_queries）→ 默认主题，不用改也能跑。"""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        legacy = {k: data.pop(k) for k in ("focus", "peers", "sector_queries") if k in data}
        themes = dict(data.get("themes") or {})
        if DEFAULT_THEME not in themes:
            themes[DEFAULT_THEME] = {"name": "默认", "focus": legacy.get("focus") or "",
                                     "benchmark": data.get("benchmark") or "SPY",
                                     "peers": legacy.get("peers") or [], "news": legacy.get("sector_queries") or {}}
        elif legacy:
            raise ValueError("旧字段 universe.focus / peers / sector_queries 和 config/themes/default.yaml 只能二选一")
        data["themes"] = themes
        return data

    @model_validator(mode="after")
    def _link(self) -> "Universe":
        for key, th in self.themes.items():
            if not _THEME_KEY.match(key):
                raise ValueError(f"主题名 {key!r} 只能用小写字母、数字、- 和 _（它就是 config/themes/ 下的文件名）")
            th.key = key
            if th.basket and len(th.basket_members) < 2:
                raise ValueError(f"主题 {key} 的 benchmark 写了 basket，但 peers 里的美股不到 2 只，凑不成篮子")
        for inst in self.watchlist:
            missing = [t for t in inst.themes if t not in self.themes]
            if missing:
                have = "、".join(k for k in self.themes if k != DEFAULT_THEME) or "（还没有）"
                raise ValueError(f"{inst.symbol} 的 theme 写了 {'、'.join(missing)}，config/themes/ 里没有这个主题；现有：{have}")
        return self

    # ───────── 资产 ─────────

    @property
    def instruments(self) -> list[Instrument]:
        """全部资产：assets.yaml 里的 + 生效主题的夜间信号（assets.yaml 里已经写了的以 assets.yaml 为准）。"""
        out = list(self.watchlist)
        listed = {i.symbol for i in out}
        auto: dict[str, Instrument] = {}
        for th in self.active_themes:
            members = self.theme_holdings(th.key)
            for code, cfg in th.overnight.items():
                if code in listed:
                    continue
                if code in auto:  # 几个主题都把它当夜间信号：对应的持仓合在一起，设置以第一个主题为准
                    inst = auto[code]
                    inst.related_to = list(dict.fromkeys([*inst.related_to, *members]))
                    inst.themes = list(dict.fromkeys([*inst.themes, th.key]))
                    continue
                auto[code] = Instrument(symbol=code, name=cfg.name or code, asset_class=_infer_class(code),
                                        holding=False, themes=[th.key], related_to=members, alert=cfg.alert,
                                        queries=cfg.queries, origin="theme")
        baskets = [Instrument(symbol=code, name=f"{th.name}夜间信号（{EXCHANGE_NAMES.get(ex, ex)}篮子）",
                              asset_class="foreign_equity", holding=False, themes=[th.key],
                              related_to=self.theme_holdings(th.key), alert=th.overnight_alert, origin="theme")
                   for code, (th, ex, _) in self.night_baskets.items()]
        return out + list(auto.values()) + baskets

    @property
    def night_baskets(self) -> dict[str, tuple[Theme, str, list[str]]]:
        """夜间信号篮子：伪代码 night:<主题>:<交易所> → (主题, 交易所, 成分)。同一主题、同一交易所、在盯盘的夜间信号有 2 只以上
        且 overnight_basket 打开时才有（不同交易所的交易日历不同，分钟线对不齐，各成一篮）。"""
        out: dict[str, tuple[Theme, str, list[str]]] = {}
        for th in self.active_themes:
            if not th.overnight_basket:
                continue
            by_ex: dict[str, list[str]] = {}
            for code in th.overnight:
                listed = next((i for i in self.watchlist if i.symbol == code), None)
                if listed is not None and not listed.monitor:
                    continue
                if ex := exchange_of(code):
                    by_ex.setdefault(ex, []).append(code)
            for ex, codes in by_ex.items():
                if len(codes) >= 2:
                    out[f"{NIGHT_PREFIX}{th.key}:{ex}"] = (th, ex, codes)
        return out

    def night_basket_of(self, symbol: str) -> str | None:
        """这只夜间信号属于哪个夜间篮子（不属于时为 None）。"""
        return next((code for code, (_, _, ms) in self.night_baskets.items() if symbol in ms), None)

    def display(self, symbol: str) -> str:
        """推送和列表里显示的代码：伪代码（篮子）换成名称，其余照旧。"""
        return self.label(symbol) if is_pseudo(symbol) else symbol

    def label(self, symbol: str) -> str:
        """推送里显示的名称：持仓显示代码，篮子显示“主题名篮子”，其他代码用主题 / display_names 里的名称。"""
        if symbol.startswith(BASKET_PREFIX):
            th = self.themes.get(symbol[len(BASKET_PREFIX):])
            return f"{th.name}篮子" if th else symbol
        inst = self.find(symbol)
        if inst is not None and inst.holding:
            return symbol
        if symbol in self.display_names:
            return self.display_names[symbol]
        for th in self.themes.values():
            if name := th.names().get(symbol):
                return name
        return inst.name if inst is not None and inst.name else symbol

    @property
    def holdings(self) -> list[Instrument]:
        return [i for i in self.watchlist if i.holding]

    @property
    def symbols(self) -> list[str]:
        """持仓 / 关注的代码（不含只盯盘的资产）：简报、新闻、事件、对话都用它。"""
        return [i.symbol for i in self.holdings]

    @property
    def monitored(self) -> list[Instrument]:
        """做异动检测的资产（含只盯盘的，含主题的夜间信号）。"""
        return [i for i in self.instruments if i.monitor]

    @property
    def equity_symbols(self) -> list[str]:
        """有财报、公告的持仓（股票）：SEC 公告、内部人减持只看这些。"""
        return [i.symbol for i in self.holdings if i.asset_class in ("us_equity", "foreign_equity")]

    def rules(self, symbol: str) -> AlertRules:
        """某个资产最终生效的警报标准（类别默认 + 资产自己的覆盖；没写的项为 None，由 monitor.price 兜底）。"""
        inst = self.find(symbol)
        cls = inst.asset_class if inst else "us_equity"
        base = default_class_rules().get(cls, AlertRules()).merged(self.classes.get(cls))
        return base.merged(inst.alert if inst else None)

    def find(self, symbol: str) -> Instrument | None:
        return next((i for i in self.instruments if i.symbol == symbol), None)

    def get(self, symbol: str) -> Instrument:
        inst = self.find(symbol)
        if inst is None:
            raise KeyError(symbol)
        return inst

    # ───────── 主题 ─────────

    def theme_keys_of(self, symbol: str) -> list[str]:
        """一个代码属于哪些主题：持仓 / 只盯盘资产按自己写的 theme（持仓没写的归默认主题）；
        同业、夜间信号、相关公司按列出它的主题。"""
        if symbol.startswith(PSEUDO_PREFIXES):
            key = symbol.split(":")[1] if ":" in symbol else ""
            return [key] if key in self.themes else []
        inst = next((i for i in self.watchlist if i.symbol == symbol), None)
        if inst is None:  # 主题的夜间信号自动生成的只盯盘资产：带着它自己的主题
            inst = next((i for i in self.instruments if i.symbol == symbol and i.origin == "theme"), None)
        if inst is not None:
            if inst.themes:
                return list(inst.themes)
            if inst.holding:
                return [DEFAULT_THEME]
            # 只盯盘、没写主题：跟它对应的持仓走
            return list(dict.fromkeys(k for r in inst.related_to for k in self.theme_keys_of(r)))
        listed = [k for k, th in self.themes.items()
                  if symbol in th.peers or symbol in th.overnight or symbol in th.catalysts]
        active = {t.key for t in self.active_themes}
        return [k for k in listed if k in active] + [k for k in listed if k not in active]  # 生效的主题在前

    def theme_of(self, symbol: str) -> Theme:
        """主主题（第一个）；都没有时为默认主题。"""
        keys = self.theme_keys_of(symbol)
        return self.themes[keys[0]] if keys else self.themes[DEFAULT_THEME]

    def theme_holdings(self, key: str) -> list[str]:
        return [i.symbol for i in self.holdings if key in (i.themes or [DEFAULT_THEME])]

    @property
    def active_themes(self) -> list[Theme]:
        """有持仓引用的主题（按持仓顺序）。"""
        keys: list[str] = []
        for inst in self.holdings:
            for k in inst.themes or [DEFAULT_THEME]:
                if k not in keys:
                    keys.append(k)
        return [self.themes[k] for k in keys]

    def benchmark_for(self, symbol: str) -> str | None:
        """拆分“板块 / 个股”用的基准：主主题的 benchmark（basket → basket:<主题>）。海外股票、期货、加密货币不拆分。"""
        if nb := self.night_basket_of(symbol):  # 夜间篮子的成分：相对篮子算“单独大动”
            return nb
        inst = self.find(symbol)
        if inst is not None and inst.asset_class != "us_equity":
            return None
        th = self.theme_of(symbol)
        return th.basket_code if th.basket else th.benchmark

    def basket_members(self, code: str) -> list[str]:
        if code.startswith(NIGHT_PREFIX):
            hit = self.night_baskets.get(code)
            return list(hit[2]) if hit else []
        th = self.themes.get(code[len(BASKET_PREFIX):]) if code.startswith(BASKET_PREFIX) else None
        return th.basket_members if th is not None else []

    def peers_for(self, symbol: str) -> list[str]:
        """同期对比的代码：同主题的其他持仓 + 主题的同业（不含它自己）。夜间篮子对比它的成分，成分对比篮子里的其他成分。"""
        if symbol.startswith(NIGHT_PREFIX):
            return self.basket_members(symbol)
        if nb := self.night_basket_of(symbol):
            return [m for m in self.basket_members(nb) if m != symbol]
        out: list[str] = []
        for key in self.theme_keys_of(symbol):
            th = self.themes[key]
            for s in [*self.theme_holdings(key), *th.peers]:
                if s != symbol and s not in out:
                    out.append(s)
        return out

    @property
    def peers(self) -> list[str]:
        """生效主题的全部同业（去重保序）。"""
        return list(dict.fromkeys(p for th in self.active_themes for p in th.peers))

    @property
    def us_peers(self) -> list[str]:
        return [p for p in self.peers if is_us_symbol(p)]

    @property
    def benchmarks(self) -> list[str]:
        """生效主题用到的基准（篮子写伪代码 basket:<主题>）。"""
        return list(dict.fromkeys(self.benchmark_for(s) or "" for s in self.symbols if self.benchmark_for(s)))

    @property
    def catalysts(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for th in self.active_themes:
            out.update({k: v for k, v in th.catalysts.items() if k not in out})
        return out

    def sector_news(self, symbols: list[str] | None = None) -> dict[str, dict[str, list[str]]]:
        """板块新闻检索词：{SECTOR:<主题>: {语言: [检索词]}}。symbols 为空时取全部生效主题。"""
        from tickersentinel.models import sector_tag

        keys = (list(dict.fromkeys(k for s in symbols for k in self.theme_keys_of(s))) if symbols
                else [th.key for th in self.active_themes])
        return {sector_tag(k): self.themes[k].news for k in keys if k in self.themes and self.themes[k].news}

    def focus_text(self, symbols: list[str] | None = None) -> str:
        """提示词里的领域描述：这些代码（默认全部持仓）所属主题的 focus，用顿号连起来。"""
        keys = (list(dict.fromkeys(k for s in symbols for k in self.theme_keys_of(s))) if symbols
                else [th.key for th in self.active_themes])
        return "、".join(dict.fromkeys(f for k in keys if k in self.themes and (f := self.themes[k].focus.strip())))

    # ───────── 数据范围 ─────────

    @property
    def foreign_refs(self) -> list[str]:
        """简报里展示的海外参照：主上市股票、汇率、海外同业。"""
        out: list[str] = []
        for inst in self.holdings:
            for s in (inst.underlying, inst.fx_symbol):
                if s and s not in out:
                    out.append(s)
        for p in self.peers:
            if not is_us_symbol(p) and p not in out:
                out.append(p)
        return out

    def event_names(self, related: dict[str, str] | None = None) -> dict[str, str]:
        """事件标题里用的公司名：监控标的取 name 的第一个词（“美光 Micron” → “美光”），
        海外主上市代码与对应的监控标的同名（000660.KS → SK海力士），其他代码用主题 / display_names / related 里的名称。"""
        out: dict[str, str] = {}
        for th in self.themes.values():
            out.update(th.names())
        out.update(self.display_names)
        out.update({k: v for k, v in (related or {}).items() if v})
        for inst in self.watchlist:
            short = inst.name.split()[0] if inst.name.strip() else inst.symbol
            out[inst.symbol] = short
            if inst.underlying:
                out[inst.underlying] = short
        return out

    @property
    def event_symbols(self) -> list[str]:
        """事件日历收集哪些公司：持仓（股票）、它们的海外主上市代码、生效主题的同业和催化剂公司。"""
        eq = [p for th in self.active_themes for p in [*th.peers, *th.catalysts] if _infer_class(p) in ("us_equity", "foreign_equity")
              and not p.upper().endswith("=X")]
        return list(dict.fromkeys([*self.equity_symbols, *self.underlying_map, *eq]))

    @property
    def underlying_map(self) -> dict[str, str]:
        """海外主上市代码 → 持仓（000660.KS → SKHY）：同一家公司的事件合并到持仓名下。
        只看 underlying（同一家公司）；夜间信号（related_to）不算，不然中国光模块股的财报会被记到美股持仓头上。"""
        return {inst.underlying: inst.symbol for inst in self.holdings if inst.underlying}

    @property
    def intraday_symbols(self) -> list[str]:
        """美股盘中需要分钟数据的代码：监控的美股 + 基准（篮子换成成分）+ 美股同业（去重保序）。"""
        out: list[str] = []
        us = [i.symbol for i in self.monitored if i.asset_class == "us_equity"]
        benches: list[str] = []
        for b in self.benchmarks:
            benches += self.basket_members(b) if b.startswith(BASKET_PREFIX) else [b]
        for s in [*us, *benches, *self.us_peers]:
            if s not in out:
                out.append(s)
        return out


# ───────────────────────── 简报 ─────────────────────────


class PremarketBrief(_Base):
    enabled: bool = True
    time: str = "07:30"
    trading_days_only: bool = True
    lookback_hours: int = 18

    @field_validator("time")
    @classmethod
    def check_time(cls, v: str) -> str:
        return _check_hhmm(v)


class CloseBrief(_Base):
    enabled: bool = True
    minutes_after_close: int = 15
    lookback_hours: int = 8


class WeeklyBrief(_Base):
    enabled: bool = True
    linkage_check: bool = True  # 周报末尾附“联动体检”（只在发现同业 / 夜间信号联动变弱、或有新的强联动候选时才附）
    day_of_week: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] = "sat"
    time: str = "09:00"
    lookback_hours: int = 168

    @field_validator("time")
    @classmethod
    def check_time(cls, v: str) -> str:
        return _check_hhmm(v)


class Briefs(_Base):
    premarket: PremarketBrief = Field(default_factory=PremarketBrief)
    close: CloseBrief = Field(default_factory=CloseBrief)
    weekly: WeeklyBrief = Field(default_factory=WeeklyBrief)
    max_news_per_symbol: int = 8
    events_days_ahead: int = 7  # 盘前 / 收盘简报里列出未来几天的事件（周报用 events.digest.days_ahead）


# ───────────────────────── 盘中监控 ─────────────────────────


class IdioRule(_Base):
    enabled: bool = False
    z_threshold: float = 4.5
    min_move_pct: float = 2.0
    # 自己的涨跌也要至少这么大才推（None = 不要求）。夜间篮子的成分要求：只有两三只成分时，一只大涨、另一只没动，
    # 没动的那只相对篮子也会“走弱”同样的幅度——它自己没动，不该推
    min_raw_move_pct: float | None = None


class PriceRules(_Base):
    """检测参数（detect/price.py）。默认值按日波动约 5% 的股票（MU / SNDK / SKHY）定，新增资产时 tsen scope 按它自己的波动换算。"""

    # 规则 A 急涨急跌：最近 w 分钟内的最高（最低）点到现价的跌（涨）幅 ≥ min_move_pct[w]，且 z ≥ z_threshold
    windows_minutes: list[int] = Field(default_factory=lambda: [5, 15, 30, 60])
    z_threshold: float = 4.0
    min_move_pct: dict[int, float] = Field(default_factory=lambda: {5: 1.5, 15: 2.5, 30: 3.0, 60: 3.0})
    # 规则 B 全天累计：从“昨收与日内最高点里更高的那个”算起的跌幅（涨幅对称）越过这些档位时推送，每档一次
    day_pct: list[float] = Field(default_factory=lambda: [5, 8, 12], validation_alias=AliasChoices("day_pct", "day_levels_pct"))
    idiosyncratic: IdioRule = Field(default_factory=IdioRule)
    # 只跑这些规则（None = 全部：急涨急跌、全天累计，个股特异看 idiosyncratic.enabled）。夜间篮子的成分只跑 idio：
    # 整体的涨跌由篮子触发，成分只在相对篮子单独大动时推送。旧名 level 等同 day
    rules: list[Literal["rapid", "day", "idio"]] | None = None
    cooldown_minutes: int = 15
    escalation_factor: float = 2.0  # 冷却期内幅度扩大到上次的这么多倍才再推（“扩大”）；1.5 时一波大行情会连推三四条
    open_warmup_minutes: int = 10
    sigma_lookback_sessions: int = 5
    beta_lookback_days: int = 60
    min_sigma_obs: int = 150

    @field_validator("rules", mode="before")
    @classmethod
    def legacy_rule_names(cls, v: Any) -> Any:
        return [("day" if x == "level" else x) for x in v] if isinstance(v, list) else v

    @model_validator(mode="after")
    def check_rules(self) -> "PriceRules":
        self.windows_minutes = sorted(set(self.windows_minutes))
        missing = [w for w in self.windows_minutes if w not in self.min_move_pct]
        if missing:
            raise ValueError(f"min_move_pct 缺少窗口 {missing} 分钟的阈值")
        if any(w < 1 for w in self.windows_minutes):
            raise ValueError("windows_minutes 必须为正整数")
        self.day_pct = sorted(set(self.day_pct))
        if self.escalation_factor <= 1:
            raise ValueError("escalation_factor 应大于 1")
        return self


class OptionsMonitor(_Base):
    enabled: bool = False
    poll_seconds: int = 120
    min_premium_usd: float = 500_000
    min_volume_oi_ratio: float = 1.0


class Monitor(_Base):
    enabled: bool = True
    interval_seconds: int = Field(default=60, ge=10, le=900)
    prepare_minutes_before_open: int = 20  # 已不用（监控改成全天候常驻任务后没有“开盘前准备”）；旧配置照样能读
    # 同一主题的几只同方向一起异动：同一次检测里的合成一条推送（归因一次）；之后这么多分钟内同主题同方向又触发的，
    # 挂在第一条下面作回复（见 jobs/merge.py）。0 = 不合并，每只单独推
    merge_minutes: int = Field(default=10, ge=0, le=60)
    price: PriceRules = Field(default_factory=PriceRules)
    options: OptionsMonitor = Field(default_factory=OptionsMonitor)


# ───────────────────────── 事件日历 ─────────────────────────


class EventSourcesCfg(_Base):
    yfinance: bool = True  # 财报日（全部代码，含 .KS / .T；多为估计日期）
    jpx: bool = True  # 东京交易所官方“決算発表予定日”（铠侠等 .T 代码，已确认）
    sec_6k: bool = True  # SKHY 等外国发行人用 6-K 公布的业绩发布 / 电话会时间（已确认）
    sec_144: bool = True  # 持仓（watchlist）的内部人拟减持通知 Form 144；相关公司不看


class EventDigestCfg(_Base):
    enabled: bool = True
    time: str = "20:00"  # 本地时间，每天汇总一次：明天的事件 + 新增 / 改期；两者都没有就不推送
    days_ahead: int = 14  # 汇总里附带的日历长度（周报也用它）

    @field_validator("time")
    @classmethod
    def check_time(cls, v: str) -> str:
        return _check_hhmm(v)


class ManualEvent(_Base):
    """手工录入的日程（锁定期到期、股东大会、产品发布会……）。"""

    symbol: str
    day: date
    title: str
    kind: Literal["lockup_expiry", "custom", "earnings"] = "custom"
    id: str | None = None  # 可选：以后要改这条的日期时保持同一个 id，就会按“改期”提醒，而不是删旧增新
    time_hint: str = ""
    url: str = ""
    note: str = ""


class MacroEventsCfg(_Base):
    """宏观日程（sources/events_macro.py）。不想要的项写成 [] 或 false。"""

    enabled: bool = True
    fed: list[Literal["fomc", "minutes", "chair", "beige_book"]] = Field(
        default_factory=lambda: ["fomc", "minutes", "chair"])
    us_data: list[Literal["cpi", "ppi", "jobs", "jolts", "eci", "pce", "gdp", "retail_sales", "durable_goods"]] = Field(
        default_factory=lambda: ["cpi", "ppi", "jobs", "jolts", "pce", "gdp", "retail_sales"])
    treasury_auctions: list[str] = Field(default_factory=lambda: ["10Y", "20Y", "30Y"])
    boj: bool = True
    korea_exports: bool = True
    opex: bool = True
    us_market: bool = True  # 美股休市与提前收盘

    @field_validator("treasury_auctions")
    @classmethod
    def check_terms(cls, v: list[str]) -> list[str]:
        out = []
        for t in v:
            t = str(t).strip().upper()
            if not re.fullmatch(r"\d{1,2}Y", t):
                raise ValueError(f"国债期限写成 10Y / 20Y / 30Y 这样的格式：{t!r}")
            out.append(t)
        return out


class EventsCfg(_Base):
    enabled: bool = True
    # 相关公司：只收集它们的事件（财报日等），不做盘中监控。代码 → 显示名称
    related: dict[str, str] = Field(default_factory=dict)
    collect_times: list[str] = Field(default_factory=lambda: ["06:40", "18:30"])  # 本地时间，每天刷新事件日历
    lookahead_days: int = Field(default=100, ge=14, le=400)  # 收集范围；对话的 upcoming_events 也以此为上限
    sources: EventSourcesCfg = Field(default_factory=EventSourcesCfg)
    insider_sale_min_usd: float = 1_000_000  # Form 144 拟减持金额低于此值的不收录
    digest: EventDigestCfg = Field(default_factory=EventDigestCfg)
    manual: list[ManualEvent] = Field(default_factory=list)
    macro: MacroEventsCfg = Field(default_factory=MacroEventsCfg)

    @field_validator("collect_times")
    @classmethod
    def check_times(cls, v: list[str]) -> list[str]:
        return [_check_hhmm(t) for t in v]


class Attribution(_Base):
    enabled: bool = True
    news_minutes_before: int = 90
    news_minutes_after: int = 15
    overnight_hours_for_gaps: int = 18
    use_web_search: bool = True
    max_web_searches: int = 4
    max_candidates: int = 25


# ───────────────────────── 数据源 ─────────────────────────


class PriceSourceCfg(_Base):
    provider: Literal["yfinance", "alpaca"] = "yfinance"  # 实时分钟线的主源
    fallback: Literal["none", "yfinance", "alpaca"] = "alpaca"  # 主源失败或缺代码时补（需要对应的 key；没有就不启用）
    official_closes: bool = True  # 日线收盘价优先用 Alpaca SIP 的官方收盘（需要 Alpaca key）
    alpaca_feed: Literal["iex", "sip"] = "iex"  # Alpaca 实时分钟线用的数据源（免费账户只能 iex；sip 只给 15 分钟以前）


class Toggle(_Base):
    enabled: bool = False


class FinnhubCfg(_Base):
    enabled: bool = True


class GoogleNewsEdition(_Base):
    hl: str
    gl: str
    ceid: str


class GoogleNewsCfg(_Base):
    enabled: bool = True
    editions: dict[str, GoogleNewsEdition] = Field(
        default_factory=lambda: {"en": GoogleNewsEdition(hl="en-US", gl="US", ceid="US:en")}
    )


class NewsSourcesCfg(_Base):
    finnhub: FinnhubCfg = Field(default_factory=FinnhubCfg)
    google_news_rss: GoogleNewsCfg = Field(default_factory=GoogleNewsCfg)


class SecEdgarCfg(_Base):
    enabled: bool = True
    user_agent: str = "tickersentinel your-email@example.com"
    forms: list[str] = Field(default_factory=lambda: ["8-K", "6-K", "4", "10-Q", "10-K"])


class FilingsCfg(_Base):
    sec_edgar: SecEdgarCfg = Field(default_factory=SecEdgarCfg)
    dart: Toggle = Field(default_factory=Toggle)


class Sources(_Base):
    http_timeout_seconds: float = 12.0
    prices: PriceSourceCfg = Field(default_factory=PriceSourceCfg)
    news: NewsSourcesCfg = Field(default_factory=NewsSourcesCfg)
    filings: FilingsCfg = Field(default_factory=FilingsCfg)


# ───────────────────────── LLM / 推送 / 存储 ─────────────────────────


class LMStudioCfg(_Base):
    """用 LM Studio 自带的 lms 命令行在需要时启动本地服务、加载模型（可选）。"""

    auto_start: bool = True
    lms_path: str = "~/.lmstudio/bin/lms"
    context_length: int = 32768  # 加载时的上下文长度；简报提示词约 4–8k token，留足余量
    ttl_minutes: int = 60  # 闲置多久后由 LM Studio 自动卸载模型（释放内存）


class LLMProviderCfg(_Base):
    """一个模型供应商档案。kind 决定协议：
    - openai_compat：OpenAI 兼容的 Chat Completions 接口。本机 LM Studio / Ollama / llama.cpp / MLX，
      以及 OpenAI、DeepSeek、OpenRouter 等云端服务都用它，区别只在 base_url、model 和密钥。
    - anthropic：Claude API（官方 SDK），支持服务器端 web search。
    """

    kind: Literal["openai_compat", "anthropic"]
    model: str
    base_url: str | None = None  # openai_compat 必填；anthropic 一般不填
    api_key_env: str | None = None  # 存放密钥的环境变量名（写在 .env）；本地服务不需要
    timeout_seconds: float = 300.0
    max_tokens: int | None = None  # 单次输出上限；不填按供应商默认（本地 2000，Claude 16000）
    # openai_compat：输出上限的参数名。OpenAI 的推理模型（gpt-5 系列等）只认 max_completion_tokens；
    # LM Studio 两个都认；llama.cpp / Ollama 等较老的实现只认 max_tokens
    max_tokens_param: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    temperature: float | None = None  # 只对 openai_compat 生效（Claude Sonnet 5.5 不接受非默认采样参数）
    reasoning_effort: str | None = None  # openai_compat：LM Studio 上 "none" 关闭思考，速度快数倍
    effort: str | None = None  # anthropic：output_config.effort（low / medium / high ……）
    web_search_tool: str | None = None  # anthropic：服务器端检索工具版本，如 web_search_20260209
    extra_body: dict[str, Any] = Field(default_factory=dict)  # 原样并入请求体的其他参数
    lmstudio: LMStudioCfg | None = None  # 只对本机 LM Studio 有意义

    @model_validator(mode="after")
    def check_kind(self) -> "LLMProviderCfg":
        if self.kind == "openai_compat" and not self.base_url:
            raise ValueError("openai_compat 类型的供应商需要填写 base_url")
        return self


def _research_provider() -> LLMProviderCfg:
    """给新持仓提名联动候选用的线上模型（tsen scope add）：Claude Opus 5.5 + 联网检索。没配 ANTHROPIC_API_KEY 时自动跳过。"""
    return LLMProviderCfg(kind="anthropic", model="claude-opus-5-5", api_key_env="ANTHROPIC_API_KEY", effort="high",
                          web_search_tool="web_search_20260209")


def _default_providers() -> dict[str, LLMProviderCfg]:
    return {"local": LLMProviderCfg(kind="openai_compat", base_url="http://127.0.0.1:1234/v1",
                                    model="qwen/qwen3.8-27b", reasoning_effort="none"),
            "claude_research": _research_provider()}


class LLMTasks(_Base):
    """各项任务依次尝试的供应商（填 providers 里的名字）。前一个失败（服务没开、超时、报错）就换下一个；
    全部失败或列表为空时，该任务走降级逻辑（简报只列标题，归因只列新闻）。"""

    brief: list[str] = Field(default_factory=lambda: ["local"])
    attribution: list[str] = Field(default_factory=lambda: ["local"])
    events: list[str] = Field(default_factory=lambda: ["local"])
    chat: list[str] = Field(default_factory=lambda: ["local"])
    # tsen scope add：给新持仓提名同业、海外对应公司、大客户、检索词。默认不调用任何 API：在 Claude Code 里用订阅额度准备候选
    # （项目技能 add-holding → data/scope_candidates/<代码>.json）；想自动调用就写 [claude_research]（需要 ANTHROPIC_API_KEY）
    scope: list[str] = Field(default_factory=list)


class LLMCfg(_Base):
    providers: dict[str, LLMProviderCfg] = Field(default_factory=_default_providers)
    tasks: LLMTasks = Field(default_factory=LLMTasks)
    brief_max_chars: int = 900
    brief_max_items: int = 40  # 交给模型的新闻条数上限（本地模型生成约 17 token/秒，提示词越短越快）
    reader_profile: str = "读者熟悉计量与金融，偏好简洁、信息密度高、区分事实与推断的表达。"

    @model_validator(mode="after")
    def check_routes(self) -> "LLMCfg":
        if "claude_research" not in self.providers:  # 备用档（默认没用到）：旧配置没写也补上，改 llm.tasks.scope 就能用
            self.providers["claude_research"] = _research_provider()
        for task, names in self.tasks.model_dump().items():
            unknown = [n for n in names if n not in self.providers]
            if unknown:
                raise ValueError(f"llm.tasks.{task} 引用了未定义的供应商 {unknown}（可选：{list(self.providers)}）")
        return self


class ChatCfg(_Base):
    """在飞书 / Telegram 里给机器人发消息时的对话设置。"""

    enabled: bool = True
    history_turns: int = 5  # 带上最近几轮对话（一问一答算一轮），方便接着追问
    ack: str = "收到，思考中…"  # 收到消息先回这一句（本地模型带工具回答要 20–50 秒）；留空则不回
    max_chars: int = 600  # 回答的目标长度（手机阅读）
    max_tool_rounds: int = Field(default=4, ge=1, le=10)  # 一次提问最多几轮工具调用（每轮可并发调多个工具）；之后不再提供工具、要求直接作答


class FeishuSyncCfg(_Base):
    """把事件日历同步成飞书日程、把每条异动 + 归因写进飞书多维表格（见 README「飞书日历与多维表格」）。

    只需要 FEISHU_APP_ID / FEISHU_APP_SECRET（与 notify.provider 无关）。默认关闭：先开通权限、运行
    tsen feishu calendar-setup / bitable-setup，确认没问题再打开，常驻运行（tsen run）时就会自动同步。
    """

    calendar: bool = False  # 事件刷新后同步到飞书日历
    bitable: bool = False  # 异动入库 / 归因完成后写进多维表格；你在表格里填的 label / notes 每小时拉回 alerts 表
    calendar_name: str = "tickersentinel · 事件日历"
    # 你在这个日历上的角色。reader：看得到日程详情、不能改（日历是 events 表的镜像，改了也会被下一次同步覆盖）
    calendar_role: Literal["reader", "writer"] = "reader"
    # 同时把你加为每个日程的参与人（不发通知）：不用订阅日历，日程直接出现在你的主日历里
    invite_user: bool = False
    event_minutes: int = Field(default=30, ge=5, le=480)  # 有精确时间的事件（如 SK海力士 09:00 KST 发布业绩）显示多长
    past_days: int = Field(default=7, ge=0, le=60)  # 同步多久以前的事件；更早的日程留在日历里，不再更新
    bitable_name: str = "tickersentinel · 异动记录"


class NotifyCfg(_Base):
    provider: Literal["console", "telegram", "feishu"] = "console"
    console_log_path: str | None = "data/outbox.log"
    feishu_domain: Literal["feishu", "lark"] = "feishu"  # 飞书（open.feishu.cn）或国际版 Lark（open.larksuite.com）
    feishu_sync: FeishuSyncCfg = Field(default_factory=FeishuSyncCfg)


class StorageCfg(_Base):
    sqlite_path: str = "data/tickersentinel.sqlite3"


class Secrets(BaseModel):
    """从环境变量（.env）读取的密钥。字段名大写即环境变量名。"""

    anthropic_api_key: SecretStr | None = None
    finnhub_api_key: SecretStr | None = None
    alpaca_api_key_id: SecretStr | None = None
    alpaca_api_secret_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: SecretStr | None = None
    feishu_app_id: SecretStr | None = None
    feishu_app_secret: SecretStr | None = None
    feishu_chat_id: SecretStr | None = None
    opendart_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    sec_edgar_user_agent: SecretStr | None = None  # 不是密钥，但含个人邮箱，同样只放 .env
    bls_user_agent: SecretStr | None = None  # BLS 发布日程同样要求请求头带联系邮箱（没填就不取 CPI / 非农等日程）

    @classmethod
    def from_env(cls) -> "Secrets":
        values = {}
        for name in cls.model_fields:
            raw = (os.getenv(name.upper()) or "").strip()
            values[name] = raw or None
        return cls(**values)

    def get(self, name: str) -> str | None:
        v = getattr(self, name)
        return v.get_secret_value() if v is not None else None

    def present(self) -> dict[str, bool]:
        return {name: getattr(self, name) is not None for name in type(self).model_fields}


def _default_macro_indicators() -> list[MacroIndicator]:
    rows = [
        ("us10y", "美债10年", "^TNX", "利率", "yield", "close", None, None),
        ("us30y", "美债30年", "^TYX", "利率", "yield", "close", None, None),
        ("wti", "WTI原油", "CL=F", "大宗商品", "price", "24h", None, None),
        ("gold", "黄金", "GC=F", "大宗商品", "price", "24h", None, None),
        ("silver", "白银", "SI=F", "大宗商品", "price", "24h", None, None),
        ("spx", "标普500", "^GSPC", "股指", "level", "close", "ES=F", "标普期货"),
        ("ixic", "纳斯达克", "^IXIC", "股指", "level", "close", "NQ=F", "纳指100期货"),
        ("vix", "VIX", "^VIX", "股指", "level", "close", None, None),
        ("dxy", "美元指数", "DX-Y.NYB", "其他", "level", "24h", None, None),
        ("usdjpy", "美元/日元", "JPY=X", "其他", "level", "24h", None, None),
        ("btc", "比特币", "BTC-USD", "其他", "price", "24h", None, None),
    ]
    return [MacroIndicator(key=k, name=n, symbol=s, group=g, kind=kd, ref=r, off_hours_symbol=o, off_hours_name=on)
            for k, n, s, g, kd, r, o, on in rows]


class MacroCfg(_Base):
    enabled: bool = True
    attach_to_alerts: bool = True  # 异动推送里附带一段宏观快照（in_alert 为 true 的变量）
    timeout_seconds: float = Field(default=10.0, gt=0)  # 推送异动时最多等宏观数据多久，超时就不附
    cache_seconds: int = Field(default=60, ge=0)
    treasury_curve: bool = True  # 美国财政部官方收益率曲线（前一交易日收盘）：补 2 年期与 2s10s 利差
    indicators: list[MacroIndicator] = Field(default_factory=_default_macro_indicators)


class Settings(_Base):
    timezone: str = "America/Chicago"
    log_level: str = "INFO"
    universe: Universe
    briefs: Briefs = Field(default_factory=Briefs)
    monitor: Monitor = Field(default_factory=Monitor)
    attribution: Attribution = Field(default_factory=Attribution)
    events: EventsCfg = Field(default_factory=EventsCfg)
    macro: MacroCfg = Field(default_factory=MacroCfg)
    chat: ChatCfg = Field(default_factory=ChatCfg)
    sources: Sources = Field(default_factory=Sources)
    llm: LLMCfg = Field(default_factory=LLMCfg)
    notify: NotifyCfg = Field(default_factory=NotifyCfg)
    storage: StorageCfg = Field(default_factory=StorageCfg)
    secrets: Secrets = Field(default_factory=Secrets.from_env, exclude=True)
    _paths: tuple[Path, Path, Path] | None = PrivateAttr(default=None)  # 从哪几个文件读的（tsen scope 写回同一处）

    @property
    def paths(self) -> tuple[Path, Path, Path]:
        """(settings.yaml, assets.yaml, themes 目录)。不是从文件读的（测试里直接构造）时用默认位置。"""
        return self._paths or config_paths()

    @field_validator("timezone")
    @classmethod
    def check_timezone(cls, v: str) -> str:
        ZoneInfo(v)  # 无效时区会抛错
        return v

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def rules_for(self, symbol: str) -> PriceRules:
        """某个资产的检测参数：monitor.price 打底，套上类别默认和资产自己的警报标准（config/assets.yaml）。
        夜间篮子的成分只跑“个股特异”（相对篮子，门槛是主题的 overnight_single_pct）。"""
        r = self.universe.rules(symbol)
        upd: dict[str, Any] = {}
        if nb := self.universe.night_basket_of(symbol):
            th = self.universe.night_baskets[nb][0]
            idio = self.monitor.price.idiosyncratic
            upd["rules"] = ["idio"]
            upd["idiosyncratic"] = {"enabled": True, "z_threshold": idio.z_threshold, "min_move_pct": th.overnight_single_pct,
                                    "min_raw_move_pct": th.overnight_single_pct}
        if r.rapid_pct:
            upd["windows_minutes"] = sorted(r.rapid_pct)
            upd["min_move_pct"] = dict(r.rapid_pct)
        if r.z is not None:
            upd["z_threshold"] = r.z
        if r.day_pct is not None:
            upd["day_pct"] = list(r.day_pct)
        if not upd:
            return self.monitor.price
        return PriceRules(**{**self.monitor.price.model_dump(), **upd})

    def hours_for(self, symbol: str) -> str:
        return self.universe.rules(symbol).hours or "regular"

    def resolve_path(self, p: str | Path) -> Path:
        path = Path(p)
        return path if path.is_absolute() else PROJECT_ROOT / path


def config_paths(path: str | Path | None = None) -> tuple[Path, Path, Path]:
    """(settings.yaml, assets.yaml, themes 目录)。环境变量 TICKERSENTINEL_SETTINGS / _ASSETS / _THEMES 可以分别指定
    （可以写在 .env 里；相对路径按项目根目录算）。仓库里的 config/ 是示例配置，自己的配置放在不进 git 的 config.local/，
    在 .env 里写 TICKERSENTINEL_SETTINGS=config.local/settings.yaml 指过去（assets.yaml 和 themes/ 默认在同一个目录）。"""

    def env_path(name: str) -> Path | None:
        v = os.getenv(name)
        if not v:
            return None
        p = Path(v).expanduser()
        return p if p.is_absolute() else PROJECT_ROOT / p

    cfg_path = Path(path) if path else env_path("TICKERSENTINEL_SETTINGS") or DEFAULT_SETTINGS
    assets_path = env_path("TICKERSENTINEL_ASSETS") or cfg_path.parent / ASSETS_FILE
    themes_dir = env_path("TICKERSENTINEL_THEMES") or cfg_path.parent / THEMES_DIR
    return cfg_path, assets_path, themes_dir


def load_settings(path: str | Path | None = None, *, load_env: bool = True) -> Settings:
    """读取配置。优先级：参数 > 环境变量 TICKERSENTINEL_SETTINGS > config/settings.yaml。"""
    if load_env:
        load_dotenv(PROJECT_ROOT / ".env", override=False)
    return load_from(*config_paths(path))


def load_from(cfg_path: Path, assets_path: Path, themes_dir: Path) -> Settings:
    """按给定的三处读配置（不看环境变量）：tsen scope 预览改动时用临时目录里的副本。密钥不在这里读。"""
    if not cfg_path.exists():
        raise FileNotFoundError(f"找不到配置文件：{cfg_path}")
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if assets_path.exists():
        merge_assets(data, yaml.safe_load(assets_path.read_text(encoding="utf-8")) or {}, assets_path)
    merge_themes(data, themes_dir)
    settings = Settings.model_validate(data)
    settings._paths = (cfg_path, assets_path, themes_dir)
    return settings


def merge_themes(data: dict, themes_dir: Path) -> None:
    """config/themes/*.yaml → universe.themes（文件名就是主题名）。子目录不读。"""
    if not themes_dir.is_dir():
        return
    universe = data.setdefault("universe", {})
    themes = universe.setdefault("themes", {})
    for f in sorted(themes_dir.glob("*.yaml")):
        if f.stem in themes:
            raise ValueError(f"主题 {f.stem} 写了两处（settings.yaml 的 universe.themes 和 {f}）：请只保留 {f}")
        content = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        if not isinstance(content, dict):
            raise ValueError(f"{f} 应该是一个 YAML 字典（name / benchmark / peers …）")
        themes[f.stem] = content


def merge_assets(data: dict, assets: dict, path: Path | str = ASSETS_FILE) -> None:
    """把 assets.yaml（defaults：各类资产的默认警报标准；assets：资产清单）并进 settings 的 universe。"""
    extra = set(assets) - {"defaults", "assets"}
    if extra:
        raise ValueError(f"{path} 只认 defaults 和 assets 两段，多了：{', '.join(sorted(extra))}")
    universe = data.setdefault("universe", {})
    if assets.get("assets"):
        if universe.get("watchlist"):
            raise ValueError(f"资产清单写了两处（settings.yaml 的 universe.watchlist 和 {path}）：请只保留 {path}")
        universe["watchlist"] = assets["assets"]
    if assets.get("defaults"):
        universe["classes"] = assets["defaults"]
