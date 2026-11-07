"""事件日历页的数据整理（不含路由）：取事件、按本地日期分组、分类与配色、月历网格、列表、详情。

- 事件按读者本地时区（settings.tz）的日期归到某一天：有精确时间的按 at 换算（首尔 10-28 09:00 = 芝加哥 10-27 19:00），
  只有日期的按事件自己的日期。所以取数据时前后各多取一天，再按本地日期筛。
- 只列监控范围里的事件（events.hub.event_in_scope，和每晚汇总、简报、飞书日历一致）。
- 分类：宏观（kind = macro，按分组 FED / US / UST / BOJ / KR / MKT 配色）与公司（持仓的加粗、带左边框）。
"""

from __future__ import annotations

import calendar as pycal
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from tickersentinel.events.digest import local_day
from tickersentinel.events.hub import LAST_REFRESH_KEY, event_in_scope
from tickersentinel.feishu.calendar_sync import KIND_TAGS, SOURCE_NAMES
from tickersentinel.models import Event
from tickersentinel.sources.events_macro import MACRO_GROUPS
from tickersentinel.timeutil import ET, UTC, fmt_local, tz_abbr

CATEGORIES = {"all": "全部", "macro": "宏观", "company": "公司", "holdings": "持仓"}
VIEWS = {"month": "月历", "list": "列表"}
LIST_DAYS = (14, 30, 60, 100)
DEFAULT_LIST_DAYS = 30
CHIPS_PER_DAY = 4  # 月历每格最多显示几条，多的折叠成“+N 更多”
MAX_API_DAYS = 400
WEEKDAYS = "一二三四五六日"

# 配色（calendar.css 的 .tone-*）：宏观按分组，公司按类型
MACRO_TONES = {"FED": "fed", "US": "us", "UST": "ust", "BOJ": "boj", "KR": "kr", "MKT": "mkt"}
# 只有时段、没有精确时间的事件，排序时按美东的大致时刻
HINT_ET = {"盘前": time(8, 0), "开盘": time(9, 30), "盘中": time(12, 0), "收盘": time(16, 0), "盘后": time(16, 5)}

LEGEND = [
    ("fed", "美联储"), ("us", "美国数据"), ("ust", "美债拍卖"), ("boj", "日本央行"), ("kr", "韩国数据"), ("mkt", "美股市场"),
    ("hold", "持仓"), ("co", "相关公司"), ("insider", "拟减持"), ("lockup", "解禁"),
]

_MONTH_RE = re.compile(r"^(\d{4})-(\d{1,2})$")


# ───────────────────────── 单条事件 ─────────────────────────


@dataclass
class CalEvent:
    """页面上的一条事件（模板和 /api/events 共用）。"""

    event: Event
    local_day: date
    category: str  # macro / company
    tone: str  # 配色
    tag: str  # 徽章文字：宏观为分组名（美联储……），公司为类型（财报 / 拟减持……）
    holding: bool
    time_label: str  # 本地 HH:MM，或时段（盘前 / 盘后……），全天为空
    sort_minutes: int  # 本地当天第几分钟；全天的排最前（-1）
    company: str = ""  # 公司事件：代码 + 名称
    tooltip: str = ""

    @property
    def key(self) -> str:
        return self.event.key

    @property
    def title(self) -> str:
        return self.event.title

    @property
    def confirmed(self) -> bool:
        return self.event.confirmed

    def to_dict(self, tz: ZoneInfo) -> dict[str, Any]:
        e = self.event
        return {
            "key": e.key, "kind": e.kind, "symbol": e.symbol, "title": e.title, "local_day": self.local_day.isoformat(),
            "at": e.at.isoformat() if e.at else None,
            "at_local": e.at.astimezone(tz).isoformat() if e.at else None,
            "time_hint": e.time_hint, "time_label": self.time_label, "confirmed": e.confirmed, "source": e.source,
            "source_name": SOURCE_NAMES.get(e.source, e.source), "url": e.url, "category": self.category,
            "group": self.tag if self.category == "macro" else "", "tag": self.tag, "holding": self.holding,
            "company": self.company,
        }


def _aware(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def holding_symbols(settings) -> set[str]:
    u = settings.universe
    return {*u.symbols, *u.underlying_map}


def company_names(settings) -> dict[str, str]:
    u = settings.universe
    try:
        return u.event_names(settings.events.related)
    except Exception:  # 配置异常时只显示代码
        return {}


def classify(e: Event, settings, tz: ZoneInfo, holdings: set[str] | None = None,
             names: dict[str, str] | None = None) -> CalEvent:
    holdings = holding_symbols(settings) if holdings is None else holdings
    names = company_names(settings) if names is None else names
    at = _aware(e.at)
    if at is not e.at:
        e = replace(e, at=at)
    day = local_day(e, tz)
    details = e.details if isinstance(e.details, dict) else {}  # 旧数据 / 坏数据可能不是字典
    if e.kind == "macro":
        group = str(details.get("group") or e.symbol)
        category, tone, tag, holding, company = "macro", MACRO_TONES.get(group, "macro"), MACRO_GROUPS.get(group, "宏观"), False, ""
    else:
        holding = e.symbol in holdings
        category = "company"
        tag = KIND_TAGS.get(e.kind, "日程")
        tone = {"insider_sale": "insider", "lockup_expiry": "lockup"}.get(e.kind, "hold" if holding else "co")
        name = names.get(e.symbol, "")
        company = f"{e.symbol} {name}" if name and name != e.symbol else e.symbol
    if at is not None:
        loc = at.astimezone(tz)
        time_label, minutes = loc.strftime("%H:%M"), loc.hour * 60 + loc.minute
    elif e.time_hint:
        time_label = e.time_hint
        t = HINT_ET.get(e.time_hint.strip())
        if t is not None:
            loc = datetime.combine(e.day, t, tzinfo=ET).astimezone(tz)
            minutes = loc.hour * 60 + loc.minute
        else:
            minutes = 0
    else:
        time_label, minutes = "", -1
    when = f"{day:%m-%d} 周{WEEKDAYS[day.weekday()]}"
    if at is not None:
        when += f" {time_label} {tz_abbr(at, tz)}"
    elif time_label:
        when += f" {time_label}"
    tooltip = f"{when}｜{e.title}" + ("" if e.confirmed else "（预计）")
    return CalEvent(e, day, category, tone, tag, holding, time_label, minutes, company, tooltip)


def sort_key(c: CalEvent) -> tuple:
    return (c.local_day, c.sort_minutes, 0 if c.category == "macro" and c.tone == "mkt" else 1,
            0 if c.holding else 1, c.title, c.key)


def matches(c: CalEvent, cat: str) -> bool:
    if cat == "macro":
        return c.category == "macro"
    if cat == "company":
        return c.category == "company"
    if cat == "holdings":
        return c.category == "company" and c.holding
    return True


# ───────────────────────── 取数据 ─────────────────────────


def tz_label(tz: ZoneInfo, start: date, end: date) -> str:
    """这一屏用到的时区缩写：跨夏令时切换时写成“CDT/CST”。"""
    abbrs: list[str] = []
    for d in (start, end):
        a = tz_abbr(datetime.combine(d, time(12), tzinfo=tz), tz)
        if a and a not in abbrs:
            abbrs.append(a)
    return "/".join(abbrs)


def load_events(ctx, start: date, end: date, cat: str = "all") -> list[CalEvent]:
    """本地日期在 [start, end] 的事件（含两端），已排序。数据源前后各多取一天，再按本地日期筛。"""
    settings, tz = ctx.settings, ctx.settings.tz
    lo, hi = start - timedelta(days=1), end + timedelta(days=1)
    if ctx.events is not None:
        raw = ctx.events.upcoming(lo, hi)
    else:
        raw = [e for e in ctx.storage.events_between(lo, hi) if event_in_scope(settings, e)]
    holdings, names = holding_symbols(settings), company_names(settings)
    out = []
    for e in raw:
        try:
            c = classify(e, settings, tz, holdings, names)
        except Exception:  # 个别坏数据不影响整页
            continue
        if start <= c.local_day <= end and matches(c, cat):
            out.append(c)
    return sorted(out, key=sort_key)


def find_event(storage, key: str) -> Event | None:
    """按 key 取一条事件（不限监控范围：详情页只看库里有没有）。"""
    if not key:
        return None
    getter = getattr(storage, "event_by_key", None)
    if callable(getter):
        return getter(key)
    with storage._lock:
        row = storage.conn.execute("SELECT * FROM events WHERE key = ?", (key,)).fetchone()
    return storage._row_to_event(row) if row is not None else None


def last_refresh(ctx) -> datetime | None:
    try:
        if ctx.events is not None:
            return _aware(ctx.events.last_refresh())
        raw = ctx.storage.kv_get(LAST_REFRESH_KEY)
        return _aware(datetime.fromisoformat(raw)) if raw else None
    except (TypeError, ValueError):
        return None


# ───────────────────────── 参数 ─────────────────────────


def parse_month(raw: str | None, today: date) -> tuple[date, bool]:
    """'YYYY-MM' → 当月 1 日；格式不对返回本月和 False（页面上提示一句，不报错）。"""
    if not raw:
        return today.replace(day=1), True
    m = _MONTH_RE.match(raw.strip())
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if 2000 <= y <= 2100 and 1 <= mo <= 12:
            return date(y, mo, 1), True
    return today.replace(day=1), False


def parse_days(raw: str | None) -> int:
    try:
        v = int(raw) if raw else DEFAULT_LIST_DAYS
    except ValueError:
        return DEFAULT_LIST_DAYS
    return v if v in LIST_DAYS else DEFAULT_LIST_DAYS


def parse_cat(raw: str | None) -> str:
    return raw if raw in CATEGORIES else "all"


def parse_view(raw: str | None) -> str:
    return raw if raw in VIEWS else "month"


def add_months(d: date, n: int) -> date:
    idx = d.year * 12 + d.month - 1 + n
    return date(idx // 12, idx % 12 + 1, 1)


def relative_label(day: date, today: date) -> str:
    diff = (day - today).days
    return {0: "今天", 1: "明天", 2: "后天", -1: "昨天"}.get(diff, f"{diff} 天后" if diff > 0 else f"{-diff} 天前")


# ───────────────────────── 月历 ─────────────────────────


@dataclass
class DayCell:
    day: date
    in_month: bool
    is_today: bool
    is_past: bool
    weekend: bool
    events: list[CalEvent] = field(default_factory=list)
    holiday: str = ""  # 美股休市 / 提前收盘的提示

    @property
    def hidden(self) -> int:
        return max(0, len(self.events) - CHIPS_PER_DAY)


HOLIDAY_PREFIXES = (("macro:us_holiday:", "美股休市"), ("macro:us_early_close:", "提前收盘"))


def holiday_marks(events: list[CalEvent]) -> dict[date, str]:
    """美股休市 / 提前收盘的日子（来自 MKT 宏观事件）。休市优先于提前收盘。"""
    out: dict[date, str] = {}
    for c in events:
        for prefix, text in HOLIDAY_PREFIXES:
            if c.event.key.startswith(prefix) and out.get(c.local_day) != "美股休市":
                out[c.local_day] = text
    return out


def month_grid(month: date, today: date, events: list[CalEvent],
               holidays: dict[date, str] | None = None) -> list[list[DayCell]]:
    """周一开头的月历：4–6 周，前后补上相邻月份的日子。

    holidays：休市标记；不给时从 events 里找。按分类筛选时由调用方用全部事件算好传进来，
    这样只看“公司”“持仓”时格子里照样标出美股休市。"""
    holidays = holiday_marks(events) if holidays is None else holidays
    first = month
    last = date(month.year, month.month, pycal.monthrange(month.year, month.month)[1])
    start = first - timedelta(days=first.weekday())
    end = last + timedelta(days=6 - last.weekday())
    by_day: dict[date, list[CalEvent]] = {}
    for c in events:
        by_day.setdefault(c.local_day, []).append(c)
    weeks: list[list[DayCell]] = []
    d = start
    while d <= end:
        week = []
        for _ in range(7):
            evs = by_day.get(d, [])
            cell = DayCell(d, d.month == month.month, d == today, d < today, d.weekday() >= 5, evs,
                           holidays.get(d, ""))
            week.append(cell)
            d += timedelta(days=1)
        weeks.append(week)
    return weeks


def grid_range(month: date) -> tuple[date, date]:
    first = month
    last = date(month.year, month.month, pycal.monthrange(month.year, month.month)[1])
    return first - timedelta(days=first.weekday()), last + timedelta(days=6 - last.weekday())


# ───────────────────────── 列表 ─────────────────────────


@dataclass
class DayGroup:
    day: date
    label: str  # 今天 / 明天 / 3 天后
    events: list[CalEvent]

    @property
    def weekday(self) -> str:
        return "周" + WEEKDAYS[self.day.weekday()]


def group_by_day(events: list[CalEvent], today: date) -> list[DayGroup]:
    out: list[DayGroup] = []
    for c in events:
        if not out or out[-1].day != c.local_day:
            out.append(DayGroup(c.local_day, relative_label(c.local_day, today), []))
        out[-1].events.append(c)
    return out


# ───────────────────────── 详情 ─────────────────────────


def _money(v: Any) -> str:
    """美元金额，中文习惯：41.4 亿美元、2,605 万美元。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(x) >= 1e8:
        return f"{x / 1e8:,.1f} 亿美元"
    if abs(x) >= 1e4:
        return f"{x / 1e4:,.0f} 万美元"
    return f"{x:,.0f} 美元"


def _num(v: Any, digits: int = 2) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{x:,.{digits}f}"


def _shares(v: Any) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(x) >= 1e8:
        return f"{x / 1e8:.2f} 亿股"
    if abs(x) >= 1e4:
        return f"{x / 1e4:.1f} 万股"
    return f"{x:,.0f} 股"


PERIOD_CN = {"Q1": "一季度", "Q2": "二季度", "Q3": "三季度", "Q4": "四季度", "H1": "上半年", "H2": "下半年", "FY": "全年"}
_PERIOD_RES = (re.compile(r"^(Q[1-4]|H[12]|FY)\s*(\d{4})$"), re.compile(r"^(\d{4})\s*(Q[1-4]|H[12]|FY)$"))


def period_cn(p: str) -> str:
    """'Q3 2026' / '2026Q3' → '2026 财年三季度'（数据源给的是公司自己的财年，美光、希捷的财年和自然年不一致）；
    认不出的原样返回。"""
    t = p.strip().upper()
    m = _PERIOD_RES[0].match(t)
    if m:
        return f"{m.group(2)} 财年{PERIOD_CN[m.group(1)]}"
    m = _PERIOD_RES[1].match(t)
    if m:
        return f"{m.group(1)} 财年{PERIOD_CN[m.group(2)]}"
    return p


def detail_rows(c: CalEvent, settings) -> list[tuple[str, str]]:
    """详情里的“名称：内容”行（纯文本，模板负责转义）。"""
    e, tz = c.event, settings.tz
    d = e.details if isinstance(e.details, dict) else {}
    rows: list[tuple[str, str]] = []
    if e.at:
        rows.append(("时间", f"{fmt_local(e.at, tz, '%Y-%m-%d')} 周{WEEKDAYS[c.local_day.weekday()]} "
                             f"{fmt_local(e.at, tz, '%H:%M')} {tz_abbr(e.at, tz)}"))
        if str(tz) != str(ET):
            rows.append(("美东", f"{fmt_local(e.at, ET, '%m-%d %H:%M')} {tz_abbr(e.at, ET)}"))
        if d.get("local_time"):
            rows.append(("当地时间", str(d["local_time"])))
    else:
        rows.append(("日期", f"{e.day:%Y-%m-%d} 周{WEEKDAYS[e.day.weekday()]}"))
        if e.time_hint:
            rows.append(("时段", e.time_hint))
        elif e.kind != "insider_sale":  # 拟减持是“提交了通知”，没有时段可言
            rows.append(("时段", "全天（没有具体时间）"))
    src = SOURCE_NAMES.get(e.source, e.source or "未知")
    if e.confirmed:
        rows.append(("状态", f"已确认（来源：{src}）"))
    else:
        who = "官方公布" if e.kind == "macro" else "公司公告"
        rows.append(("状态", f"预计日期（来源：{src}），以{who}为准"))
    if c.category == "company":
        rows.append(("公司", c.company + ("（持仓）" if c.holding else "")))
    if e.kind == "earnings":
        if d.get("period"):
            rows.append(("报告期", period_cn(str(d["period"]))))
        if d.get("eps_actual") is not None:
            rows.append(("EPS 实际", _num(d["eps_actual"])))
        if d.get("eps_avg") is not None:
            eps = f"{_num(d['eps_avg'])}"
            if d.get("eps_low") is not None and d.get("eps_high") is not None:
                eps += f"（区间 {_num(d['eps_low'])} ~ {_num(d['eps_high'])}）"
            rows.append(("EPS 一致预期", eps))
        if d.get("revenue_avg") is not None:
            rev = _money(d["revenue_avg"])
            if d.get("revenue_low") is not None and d.get("revenue_high") is not None:
                rev += f"（区间 {_money(d['revenue_low'])} ~ {_money(d['revenue_high'])}）"
            rows.append(("营收一致预期", rev))
        if isinstance(d.get("day_range"), list) and d["day_range"]:
            rows.append(("估计区间", " ~ ".join(str(x) for x in d["day_range"])))
        if d.get("conference_call"):
            rows.append(("电话会", "有"))
    elif e.kind == "insider_sale":
        who = str(d.get("person") or "内部人")
        if d.get("relationship"):
            who += f"（{d['relationship']}）"
        rows.append(("申报人", who))
        if d.get("shares") is not None:
            amt = _shares(d["shares"])
            if d.get("market_value_usd") is not None:
                amt += f"，约 {_money(d['market_value_usd'])}"
            rows.append(("拟出售", amt))
        if d.get("pct_outstanding") is not None:
            try:
                rows.append(("占流通股", f"{float(d['pct_outstanding']):.3f}%"))
            except (TypeError, ValueError):
                pass
        for k, name in (("approx_sale_date", "预计出售日"), ("notice_date", "通知日期"), ("filed", "提交日期"),
                        ("broker", "经纪商"), ("form", "表格")):
            if d.get(k):
                rows.append((name, str(d[k])))
        if d.get("remarks"):
            rows.append(("说明", str(d["remarks"])))
    elif e.kind == "macro":
        if d.get("meeting"):
            rows.append(("会议", str(d["meeting"])))
        if d.get("sep"):
            rows.append(("经济预测", "含点阵图与经济预测摘要（SEP）"))
        if d.get("outlook"):
            rows.append(("展望报告", "含《经济与物价展望》"))
        if d.get("term"):
            rows.append(("期限", str(d["term"])))
        if d.get("quarterly"):
            rows.append(("类型", "季度到期（四巫日）"))
    if d.get("note"):
        rows.append(("备注", str(d["note"])))
    return rows


def safe_url(url: str) -> str:
    """只放行 http(s) 链接（详情里的“打开来源”）。"""
    u = (url or "").strip()
    return u if u.lower().startswith(("http://", "https://")) else ""

