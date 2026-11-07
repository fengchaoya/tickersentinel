"""财报日程：yfinance（美股 + 海外代码，多为估计）与 JPX 官方「決算発表予定日」（东京上市公司，已确认）。

【yfinance —— 2026-10-20 联网核实（yfinance 1.7.0）：MU / SNDK / SKHY / WDC / STX / NVDA / 005930.KS / 000660.KS / 285A.T】
Yahoo 的财报日期有三种取法，数据同源，信息量不同：
1) Ticker.calendar（quoteSummary 的 calendarEvents 模块）：{'Earnings Date': [date, ...], 'Earnings Average/Low/High',
   'Revenue Average/Low/High', ...}，只有下一次财报。yfinance 用**本机时区**把时间戳截成日期，并丢掉了原始数据里的
   isEarningsDateEstimate。两个日期表示估计区间（这次抽查 29 个代码，全都只有一个日期）。
   这里只用它取一致预期（EPS / 营收的均值、低、高；营收为财报货币，SKHY 是韩元，EPS 是每份 ADR 的美元），
   并在 3) 不可用时兜底给日期。
2) Ticker.get_earnings_dates()：抓 finance.yahoo.com/calendar/earnings 网页表格，时间精确到小时（"at 4 PM EDT"），
   但未来财报的时间就是 3) 里的占位时间，也没有“盘前/盘后/估计”标记，只适合看历史，不用。
3) POST query1.finance.yahoo.com/v1/finance/visualization（entityIdType=sp_earnings，yfinance 的 Calendars 也用它），
   一次请求可查多个代码。字段：startdatetime（UTC）、startdatetimetype（时段代码）、dateisestimate（日期是否为估计，
   与 quoteSummary 的 isEarningsDateEstimate 一致）、eventtype（EAD = 预定 / ERA = 已发布）、eventname（如
   "Q1 2027 Earnings Announcement"，财年口径）、epsestimate、epsactual、gmtOffsetMilliSeconds（上市地 UTC 偏移）。
   经 yfinance 内部的 YfData 发送（它负责 cookie / crumb）。这是主数据源；它失败时退回 1)。

时间的坑：**未来**财报的 startdatetime 全是占位值，不能当真实时间——
- 美股（10-22～11-20 抽样 1000 多条）：BMO → 12:30Z、AMC → 20:00Z、TNS（time not supplied）→ 20:00Z、
  TAS → 19:00Z，与是否确认无关。冬令时 20:00Z = 15:00 EST，若按时间推算会把“盘后”错判成“盘中”（MU 12-23 即如此）。
- 韩股 TNS → 06:00Z（15:00 KST），日股 TNS → 06:30Z（15:30 JST），即当地收盘时间。
- 只有已经发布的财报（TAS，常精确到秒，如 MU 2026-09-30 20:01:00Z）才是真实时间；但已发布的韩股也出现过
  整点 00:00:00Z（日期占位，还是恰好 09:00 KST 发布，分不清），零点一律按“只有日期”处理。
因此：at 只取“TAS、已经过去、且不在 UTC / 上市地 / 美东零点上”的时间；time_hint 取自时段代码（BMO → 盘前、
AMC → 盘后）；有 at 的美股按美东时间推算（09:30 前盘前、16:00 起盘后、其余盘中）；其他情况留空。

“已确认”的判据：dateisestimate。对照 2026-10-20 的公开信息：MU = True（尚未公布，预计 12 月）；WDC 10-22 盘后、
STX 10-27 盘后、SNDK 10-29 盘后、SKHY 10-27（美东）、铠侠 10-30 均为 False，日期也对。
但**韩国本地代码的日期错了一天**：000660.KS 给 10-27（TNS），实际是 10-28 09:00 KST（= 10-27 20:00 美东，正是
SKHY 的日期），Yahoo 似乎把美东日期套上了首尔收盘的占位时间；005930.KS 给 10-28、电话会却在 10-29，同样可疑。
所以 confirmed 只对美股代码采用 dateisestimate，海外代码一律 False，以 JPX / DART / 6-K 等官方来源为准。

【JPX —— 決算発表予定日（2026-10-20 联网核实）】
页面 https://www.jpx.co.jp/listing/event-schedules/financial-announcement/index.html 链接若干 .xlsx，文件名带日期
（kessan09_1016.xlsx 等），运行时从页面解析链接，不写死：
- kessan.xlsx：下一个营业日发布的公司（每个营业日约 17:00 JST 更新），没有 “As of”；
- kessanMM_MMDD.xlsx：在 MM 月迎来季末 / 期末的公司，表头第 3-4 行写 “As of 2026/10/15”。
  例如铠侠（3 月财年）的二季报在 9 月文件里：285A，2026-10-30，第２四半期，決算期末 2027-03-31。
表格只有一个工作表：第 5 行是表头（決算発表予定日 / コード / 会社名 / Issue Name / 決算期末 / 業種名 / Industry /
種別 / Fiscal Year/Quarter / 市場区分 / Market Segment）；日期是 Excel 序列数（1899-12-30 起算）；纯数字代码存成数字
（2753），带字母的代码是共享字符串（285A）；日期未定写 “未定_Undecided”；REIT 的種別是 “-”；表尾有几行注释。
不引入新依赖：用 zipfile + xml.etree 读 xl/sharedStrings.xml 与工作表（兼容内联字符串、注音 rPh、1904 日期系统）。
同一家公司同一期出现在多个文件里时，以较新的文件为准（没有 “As of” 的日更文件视为最新）。
文件里没有发布时间（铠侠通常在东京收盘后发布），time_hint 留空。下载结果在实例内缓存数小时。
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import time
import unicodedata
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from html import unescape
from typing import Any, Callable
from urllib.parse import urljoin
from xml.etree import ElementTree as XmlET
from zoneinfo import ZoneInfo

import httpx

from tickersentinel.config import is_us_symbol
from tickersentinel.models import Event, earnings_key
from tickersentinel.sources.base import EventSource, SourceError
from tickersentinel.sources.http import get_bytes, make_client, request
from tickersentinel.timeutil import ET, UTC, utcnow

log = logging.getLogger(__name__)

# ───────────────────────────── yfinance ─────────────────────────────

YAHOO_SCHEDULE_URL = "https://query1.finance.yahoo.com/v1/finance/visualization"
YAHOO_CALENDAR_PAGE = "https://finance.yahoo.com/calendar/earnings?symbol={symbol}"
YAHOO_FIELDS = [
    "ticker", "eventname", "startdatetime", "startdatetimetype", "dateisestimate", "eventtype",
    "epsestimate", "epsactual", "gmtOffsetMilliSeconds",
]
YAHOO_MAX_TICKERS = 25  # 每次请求最多查多少个代码（接口每次最多返回 100 行）
YAHOO_CONCURRENCY = 3  # 同时进行的 yfinance 请求数
TIMING_HINTS = {"BMO": "盘前", "AMC": "盘后"}
CALENDAR_MATCH_DAYS = 3  # Ticker.calendar 的日期与日程行相差几天以内视为同一次财报
_CALENDAR_FIELDS = {
    "Earnings Average": "eps_avg", "Earnings Low": "eps_low", "Earnings High": "eps_high",
    "Revenue Average": "revenue_avg", "Revenue Low": "revenue_low", "Revenue High": "revenue_high",
}
_LOCAL_TZ = {".KS": "Asia/Seoul", ".KQ": "Asia/Seoul", ".T": "Asia/Tokyo"}
_PERIOD_RE = re.compile(r"\b((?:Q[1-4]|H[12]|FY)\s+\d{4})\b")


def fetch_yahoo_schedule(symbols: list[str], start: date, end: date, timeout: float) -> dict:
    """（同步）visualization 接口的原始 JSON。startdatetime 是 UTC，日期边界各放宽 2 天，由调用方按当地日期精确过滤。"""
    from yfinance.data import YfData

    tickers = [{"operator": "eq", "operands": ["ticker", s]} for s in symbols]
    body = {
        "sortType": "ASC", "sortField": "startdatetime", "entityIdType": "sp_earnings",
        "includeFields": YAHOO_FIELDS, "size": 100, "offset": 0,
        "query": {"operator": "and", "operands": [
            tickers[0] if len(tickers) == 1 else {"operator": "or", "operands": tickers},
            {"operator": "gte", "operands": ["startdatetime", (start - timedelta(days=2)).isoformat()]},
            {"operator": "lte", "operands": ["startdatetime", (end + timedelta(days=2)).isoformat()]},
        ]},
    }
    resp = YfData().post(YAHOO_SCHEDULE_URL, body=body, params={"lang": "en-US", "region": "US"}, timeout=timeout)
    return resp.json()


def fetch_yahoo_calendar(symbol: str) -> dict:
    """（同步）Ticker.calendar：下一次财报的日期与一致预期。"""
    import yfinance as yf

    return dict(yf.Ticker(symbol).calendar or {})


@dataclass(frozen=True)
class YahooEarningsRow:
    """visualization 接口的一行。"""

    symbol: str
    start: datetime  # UTC；未来财报是占位时间（见模块说明）
    timing: str = ""  # BMO / AMC / TAS / TNS
    is_estimate: bool | None = None
    event_type: str = ""  # EAD（预定） / ERA（已发布）
    event_name: str = ""
    eps_estimate: float | None = None
    eps_actual: float | None = None
    utc_offset: timedelta | None = None  # 上市地在事件当时的 UTC 偏移

    @property
    def reported(self) -> bool:
        return self.event_type == "ERA" or self.eps_actual is not None


def parse_yahoo_schedule(payload: dict) -> list[YahooEarningsRow]:
    finance = (payload or {}).get("finance") or {}
    if finance.get("error"):
        raise SourceError(f"[yfinance] 财报日程接口报错：{finance['error']}")
    try:
        doc = finance["result"][0]["documents"][0]
        columns = [c.get("id") for c in doc["columns"]]
        raw_rows = doc.get("rows") or []
    except (KeyError, IndexError, TypeError) as exc:
        raise SourceError(f"[yfinance] 财报日程的格式变了：{type(exc).__name__}: {exc}") from exc
    out: list[YahooEarningsRow] = []
    for raw in raw_rows:
        rec = dict(zip(columns, raw))
        try:
            symbol = str(rec["ticker"]).strip()
            start = _parse_utc(rec["startdatetime"])
        except (KeyError, TypeError, ValueError):
            continue
        offset = _num(rec.get("gmtOffsetMilliSeconds"))
        estimate = rec.get("dateisestimate")
        out.append(YahooEarningsRow(
            symbol=symbol, start=start,
            timing=str(rec.get("startdatetimetype") or "").upper(),
            is_estimate=estimate if isinstance(estimate, bool) else None,
            event_type=str(rec.get("eventtype") or "").upper(),
            event_name=str(rec.get("eventname") or ""),
            eps_estimate=_num(rec.get("epsestimate")),
            eps_actual=_num(rec.get("epsactual")),
            utc_offset=timedelta(milliseconds=offset) if offset is not None else None,
        ))
    return out


def yahoo_real_time(row: YahooEarningsRow, now: datetime) -> datetime | None:
    """Yahoo 给的时间能否当真：只有已发布（TAS 且不晚于 now）、且不在 UTC / 上市地 / 美东零点上的才算。"""
    if row.timing != "TAS" or row.start > now:
        return None
    if not is_us_symbol(row.symbol):  # 海外代码实测是 Yahoo 的入库时间（首尔凌晨 4–5 点），不是发布时间
        return None
    for t in (row.start, _local_wall(row.symbol, row.start, row.utc_offset), row.start.astimezone(ET)):
        if (t.hour, t.minute, t.second) == (0, 0, 0):
            return None
    return row.start


def us_time_hint(at: datetime) -> str:
    t = at.astimezone(ET).time()
    if t < dtime(9, 30):
        return "盘前"
    if t >= dtime(16, 0):
        return "盘后"
    return "盘中"


def yahoo_row_to_event(
    row: YahooEarningsRow, names: dict[str, str], now: datetime, calendar: dict | None = None,
) -> Event:
    """日程行 → Event。calendar（Ticker.calendar）只给尚未发布的那一次补充一致预期 / 估计区间。"""
    at = yahoo_real_time(row, now)
    us = is_us_symbol(row.symbol)
    if at is not None:
        day = at.astimezone(ET).date()
        hint = us_time_hint(at) if us else ""
    else:
        day = _local_wall(row.symbol, row.start, row.utc_offset).date()
        hint = TIMING_HINTS.get(row.timing, "")
    confirmed = row.reported or (us and row.is_estimate is False)
    period = _PERIOD_RE.search(row.event_name)
    details: dict[str, Any] = {
        "timing": row.timing or None,
        "period": period.group(1) if period else None,
        "eps_avg": row.eps_estimate,
        "eps_actual": row.eps_actual,
        "yahoo_time": row.start.isoformat(),
    }
    if calendar and not row.reported:
        dates = calendar_dates(calendar)
        if not dates or min(abs((d - day).days) for d in dates) <= CALENDAR_MATCH_DAYS:
            details.update(calendar_consensus(calendar))
            if len(dates) > 1:
                details["day_range"] = [dates[0].isoformat(), dates[-1].isoformat()]
    return Event(
        kind="earnings", symbol=row.symbol, day=day, title=_title(row.symbol, names),
        key=earnings_key(row.symbol, day), at=at, time_hint=hint, confirmed=confirmed, source="yfinance",
        url=YAHOO_CALENDAR_PAGE.format(symbol=row.symbol),
        details={k: v for k, v in details.items() if v is not None},
    )


def calendar_dates(calendar: dict) -> list[date]:
    out: set[date] = set()
    for d in calendar.get("Earnings Date") or []:
        if isinstance(d, datetime):  # 含 pd.Timestamp
            d = d.date()
        if isinstance(d, date):
            out.add(d)
    return sorted(out)


def calendar_consensus(calendar: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    for src, dst in _CALENDAR_FIELDS.items():
        v = _num(calendar.get(src))
        if v is not None:
            out[dst] = v
    return out


def calendar_to_events(symbol: str, calendar: dict, names: dict[str, str]) -> list[Event]:
    """兜底：只有 Ticker.calendar 时，按日期生成一条估计事件（两个日期 = 估计区间，取较早的一天）。"""
    dates = calendar_dates(calendar)
    if not dates:
        return []
    day = dates[0]
    details: dict[str, Any] = calendar_consensus(calendar)
    if len(dates) > 1:
        details["day_range"] = [dates[0].isoformat(), dates[-1].isoformat()]
    return [Event(
        kind="earnings", symbol=symbol, day=day, title=_title(symbol, names), key=earnings_key(symbol, day),
        confirmed=False, source="yfinance", url=YAHOO_CALENDAR_PAGE.format(symbol=symbol), details=details,
    )]


def build_yahoo_events(
    symbols: list[str], rows: list[YahooEarningsRow], calendars: dict[str, dict], names: dict[str, str],
    now: datetime,
) -> list[Event]:
    """合并日程行与 Ticker.calendar；某个代码没有日程行时退回 calendar。同一 key 只保留信息最全的一条。"""
    by_symbol: dict[str, list[YahooEarningsRow]] = defaultdict(list)
    for r in rows:
        by_symbol[r.symbol].append(r)
    best: dict[str, Event] = {}
    for sym in symbols:
        cal = calendars.get(sym) or {}
        if by_symbol.get(sym):
            events = [yahoo_row_to_event(r, names, now, cal) for r in by_symbol[sym]]
        else:
            events = calendar_to_events(sym, cal, names)
        for ev in events:
            prev = best.get(ev.key)
            if prev is None or _rank(ev) > _rank(prev):
                best[ev.key] = ev
    return sorted(best.values(), key=lambda e: (e.day, e.symbol))


class YFinanceEarnings(EventSource):
    """yfinance 财报日程：美股与海外代码都能查；除已发布的财报外几乎没有精确时间（见模块说明）。"""

    name = "yfinance"

    def __init__(self, names: dict[str, str], timeout: float = 20.0, concurrency: int = YAHOO_CONCURRENCY):
        self.names = dict(names or {})
        self.timeout = timeout
        self._sem = asyncio.Semaphore(concurrency)

    async def _run(self, fn: Callable[..., Any], *args: Any) -> Any:
        async with self._sem:
            return await asyncio.wait_for(asyncio.to_thread(fn, *args), self.timeout)

    async def collect(self, symbols: list[str], start: date, end: date) -> list[Event]:
        syms = [s for s in dict.fromkeys(symbols) if _yahoo_supported(s)]
        if not syms:
            return []
        chunks = [syms[i:i + YAHOO_MAX_TICKERS] for i in range(0, len(syms), YAHOO_MAX_TICKERS)]
        jobs = [self._run(fetch_yahoo_schedule, c, start, end, self.timeout) for c in chunks]
        jobs += [self._run(fetch_yahoo_calendar, s) for s in syms]
        results = await asyncio.gather(*jobs, return_exceptions=True)

        rows: list[YahooEarningsRow] = []
        schedule_ok = False
        for chunk, res in zip(chunks, results[:len(chunks)]):
            try:
                if isinstance(res, BaseException):
                    raise res
                rows.extend(parse_yahoo_schedule(res))
                schedule_ok = True
            except Exception as exc:  # yfinance 内部异常类型不稳定；这一批代码退回 Ticker.calendar
                log.warning("yfinance 财报日程获取失败（%s），改用 Ticker.calendar：%s", ", ".join(chunk), _describe(exc))
        calendars: dict[str, dict] = {}
        for sym, res in zip(syms, results[len(chunks):]):
            if isinstance(res, BaseException):
                if not isinstance(res, Exception):
                    raise res
                log.warning("yfinance 财报日历获取失败（%s）：%s", sym, _describe(res))
            else:
                calendars[sym] = res if isinstance(res, dict) else {}
        if not schedule_ok and not calendars:
            raise SourceError(f"[yfinance] 财报日程与日历全部获取失败（{', '.join(syms)}）：网络或限流")
        events = build_yahoo_events(syms, rows, calendars, self.names, now=utcnow())
        return [e for e in events if start <= e.day <= end]


def _yahoo_supported(symbol: str) -> bool:
    s = symbol.upper()
    return bool(s) and not s.startswith("^") and not s.endswith("=X")


def _symbol_tz(symbol: str) -> ZoneInfo:
    for suffix, tz in _LOCAL_TZ.items():
        if symbol.upper().endswith(suffix):
            return ZoneInfo(tz)
    return ET


def _local_wall(symbol: str, ts: datetime, offset: timedelta | None) -> datetime:
    """上市地的墙上时间。优先用接口给的偏移，没有时按代码后缀推断时区。"""
    if offset is not None:
        return ts.astimezone(UTC).replace(tzinfo=None) + offset
    return ts.astimezone(_symbol_tz(symbol))


def _parse_utc(s: Any) -> datetime:
    ts = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # 排除 NaN


def _title(symbol: str, names: dict[str, str]) -> str:
    return f"{names.get(symbol, symbol)} 财报"


def _rank(ev: Event) -> tuple:
    return ("eps_actual" in ev.details, ev.confirmed, ev.at is not None)


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


# ───────────────────────────── JPX ─────────────────────────────

JPX_PAGE_URL = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/index.html"
JPX_CACHE_TTL = 4 * 3600.0  # 秒。日更文件约 17:00 JST 更新一次，缓存几小时足够
MAX_XLSX_PART = 64 * 1024 * 1024  # 解压后单个部件的上限，防止异常文件撑爆内存

_XLSX_LINK_RE = re.compile(r"""href\s*=\s*["']([^"']+?\.xlsx(?:[?#][^"']*)?)["']""", re.I)
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_DOC_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_CELL_REF_RE = re.compile(r"([A-Z]+)")
_AS_OF_RES = (
    re.compile(r"As of\s*(\d{4})/(\d{1,2})/(\d{1,2})", re.I),
    re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*現在"),
)
_DATE_TEXT_RE = re.compile(r"(\d{4})[/\-.年](\d{1,2})[/\-.月](\d{1,2})")
# 表头关键词（日文 / 英文任一出现即可）；第一行同时找到 day 与 code 的就是表头
_JPX_HEADERS: dict[str, tuple[str, ...]] = {
    "day": ("決算発表予定日", "Scheduled Date"),
    "code": ("コード", "Code"),
    "company": ("会社名",),
    "company_en": ("Issue Name",),
    "fiscal_year_end": ("決算期末", "Fiscal Year-end"),
    "period": ("種別",),
    "period_en": ("Fiscal Year/Quarter",),
    "market": ("Market Segment",),
}
_JP_PERIODS = {  # NFKC 规范化后的種別 → 中文
    "第1四半期": "第1季度", "第2四半期": "第2季度", "第3四半期": "第3季度", "第4四半期": "第4季度", "本決算": "全年",
}


@dataclass(frozen=True)
class JPXScheduleRow:
    code: str  # 4 位证券代码，如 "285A"、"6758"
    day: date | None  # 決算発表予定日（东京日期）；None = 未定
    company: str = ""
    company_en: str = ""
    fiscal_year_end: date | None = None
    period: str = ""  # 種別：第２四半期 / 本決算 / -（REIT）
    period_en: str = ""
    market: str = ""  # Market Segment：Prime / Standard / Growth
    as_of: date | None = None  # 所在文件的 “As of”；日更文件为 None


def jpx_code(symbol: str) -> str | None:
    """东京上市代码 → JPX 证券代码：285A.T → 285A；其他代码返回 None。"""
    s = symbol.strip().upper()
    if not s.endswith(".T"):
        return None
    code = s[:-2]
    return code if re.fullmatch(r"[0-9A-Z]{4}", code) else None


def find_xlsx_links(html: str, base_url: str) -> list[str]:
    out: list[str] = []
    for m in _XLSX_LINK_RE.finditer(html):
        url = urljoin(base_url, unescape(m.group(1)))
        if url not in out:
            out.append(url)
    return out


def read_xlsx(data: bytes) -> tuple[list[list[Any]], bool]:
    """读 xlsx 的第一个工作表 → (行列表, 是否 1904 日期系统)。

    单元格：字符串（共享 / 内联 / 公式结果）→ str；数字 → float；布尔 → bool；空 → None。按列号对齐，缺的列补 None。
    格式不对时抛 ValueError。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            parts = set(zf.namelist())
            workbook = XmlET.fromstring(_read_part(zf, "xl/workbook.xml"))
            pr = workbook.find(f"{_NS}workbookPr")
            date1904 = pr is not None and pr.get("date1904", "").lower() in ("1", "true")
            sheet = XmlET.fromstring(_read_part(zf, _first_sheet_path(zf, workbook, parts)))
            shared = []
            if "xl/sharedStrings.xml" in parts:
                sst = XmlET.fromstring(_read_part(zf, "xl/sharedStrings.xml"))
                shared = [_rich_text(si) for si in sst.findall(f"{_NS}si")]
    except (zipfile.BadZipFile, KeyError, XmlET.ParseError) as exc:
        raise ValueError(f"不是有效的 xlsx：{type(exc).__name__}: {exc}") from exc
    rows: list[list[Any]] = []
    for row in sheet.iter(f"{_NS}row"):
        cells: dict[int, Any] = {}
        for c in row.findall(f"{_NS}c"):
            ref = c.get("r")
            col = _col_index(ref) if ref else (max(cells) + 1 if cells else 0)
            cells[col] = _cell_value(c, shared)
        rows.append([cells.get(i) for i in range(max(cells) + 1)] if cells else [])
    return rows, date1904


def excel_serial_to_date(value: float, date1904: bool = False) -> date:
    base = date(1904, 1, 1) if date1904 else date(1899, 12, 30)
    return base + timedelta(days=int(float(value)))


def parse_jpx_schedule(data: bytes) -> tuple[date | None, list[JPXScheduleRow]]:
    """決算発表予定日 xlsx → (As of 日期, 行列表)。“未定”的行 day=None；表尾注释等没有代码的行跳过。"""
    rows, date1904 = read_xlsx(data)
    header_idx, cols = _find_header(rows)
    as_of = _find_as_of(rows[:header_idx])
    out: list[JPXScheduleRow] = []
    for row in rows[header_idx + 1:]:
        cell = {field: (row[i] if i < len(row) else None) for field, i in cols.items()}
        code = _norm_code(cell.get("code"))
        if code is None:
            continue
        out.append(JPXScheduleRow(
            code=code,
            day=_cell_date(cell.get("day"), date1904),
            company=_text(cell.get("company")),
            company_en=_text(cell.get("company_en")),
            fiscal_year_end=_cell_date(cell.get("fiscal_year_end"), date1904),
            period=_text(cell.get("period")),
            period_en=_text(cell.get("period_en")),
            market=_text(cell.get("market")),
            as_of=as_of,
        ))
    return as_of, out


def merge_jpx_files(files: list[tuple[date | None, list[JPXScheduleRow]]]) -> dict[str, list[JPXScheduleRow]]:
    """多个文件合并成 {代码: [行]}。同一代码、同一決算期末、同一種別只留最新文件里的那行（无 As of 的日更文件最新）。"""
    ordered = sorted(files, key=lambda f: (f[0] is None, f[0] or date.min))
    merged: dict[tuple, JPXScheduleRow] = {}
    for _, rows in ordered:
        for r in rows:
            merged[(r.code, r.fiscal_year_end, r.period)] = r
    out: dict[str, list[JPXScheduleRow]] = defaultdict(list)
    for r in merged.values():
        out[r.code].append(r)
    return dict(out)


def jpx_row_to_event(symbol: str, row: JPXScheduleRow, names: dict[str, str], url: str = JPX_PAGE_URL) -> Event | None:
    if row.day is None:
        return None
    period = _JP_PERIODS.get(unicodedata.normalize("NFKC", row.period), "")
    fiscal = f"{row.fiscal_year_end.year}年{row.fiscal_year_end.month}月期" if row.fiscal_year_end else ""
    label = " ".join(x for x in (fiscal, period) if x)
    name = names.get(symbol, symbol)
    details: dict[str, Any] = {
        "period": row.period or None,
        "period_en": row.period_en or None,
        "fiscal_year_end": row.fiscal_year_end.isoformat() if row.fiscal_year_end else None,
        "company": row.company or None,
        "company_en": row.company_en or None,
        "market": row.market or None,
        "as_of": row.as_of.isoformat() if row.as_of else None,
    }
    return Event(
        kind="earnings", symbol=symbol, day=row.day, title=f"{name} 财报（{label}）" if label else f"{name} 财报",
        key=earnings_key(symbol, row.day), confirmed=True, source="jpx", url=url,
        details={k: v for k, v in details.items() if v is not None},
    )


class JPXEarningsCalendar(EventSource):
    """东京证券交易所官方的決算発表予定日（只处理 .T 代码）。"""

    name = "jpx"

    def __init__(
        self, names: dict[str, str], timeout: float = 20.0, client: httpx.AsyncClient | None = None,
        cache_ttl: float = JPX_CACHE_TTL, page_url: str = JPX_PAGE_URL,
    ):
        self.names = dict(names or {})
        self.page_url = page_url
        self.cache_ttl = cache_ttl
        self._own_client = client is None
        self._client = client or make_client(timeout)
        self._cache: dict[str, list[JPXScheduleRow]] | None = None
        self._cached_at = 0.0
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    async def collect(self, symbols: list[str], start: date, end: date) -> list[Event]:
        wanted = {code: s for s in dict.fromkeys(symbols) if (code := jpx_code(s))}
        if not wanted:
            return []
        schedule = await self.schedule()
        out: list[Event] = []
        for code, sym in wanted.items():
            for row in schedule.get(code, []):
                ev = jpx_row_to_event(sym, row, self.names, self.page_url)
                if ev is not None and start <= ev.day <= end:
                    out.append(ev)
        return sorted(out, key=lambda e: (e.day, e.symbol))

    async def schedule(self) -> dict[str, list[JPXScheduleRow]]:
        """全部公司的预定日（带缓存）。更新失败时沿用旧缓存；从来没有成功过才抛 SourceError。"""
        async with self._lock:
            if self._cache is not None and time.monotonic() - self._cached_at < self.cache_ttl:
                return self._cache
            try:
                fresh = await self._download()
            except SourceError as exc:
                if self._cache is None:
                    raise
                age_h = (time.monotonic() - self._cached_at) / 3600
                log.warning("JPX 決算発表予定日更新失败，沿用 %.1f 小时前的数据：%s", age_h, exc)
                return self._cache
            self._cache, self._cached_at = fresh, time.monotonic()
            return fresh

    async def _download(self) -> dict[str, list[JPXScheduleRow]]:
        page = await request(self._client, self.page_url, source="jpx")
        links = find_xlsx_links(page.text, str(page.url))
        if not links:
            raise SourceError(f"[jpx] 页面上没有找到 .xlsx 链接（页面改版？）：{self.page_url}")
        files: list[tuple[date | None, list[JPXScheduleRow]]] = []
        for url in links:
            try:
                files.append(parse_jpx_schedule(await get_bytes(self._client, url, source="jpx")))
            except SourceError as exc:
                log.warning("JPX 文件下载失败（%s）：%s", url, exc)
            except ValueError as exc:
                log.warning("JPX 文件解析失败（%s）：%s", url, exc)
        if not files:
            raise SourceError(f"[jpx] {len(links)} 个 .xlsx 全部下载或解析失败")
        return merge_jpx_files(files)


def _read_part(zf: zipfile.ZipFile, name: str) -> bytes:
    if zf.getinfo(name).file_size > MAX_XLSX_PART:
        raise ValueError(f"xlsx 部件过大：{name}")
    return zf.read(name)


def _first_sheet_path(zf: zipfile.ZipFile, workbook: XmlET.Element, parts: set[str]) -> str:
    """workbook.xml 里第一个 <sheet> 经 workbook.xml.rels 指向的文件；解析不到时用 xl/worksheets/sheet1.xml。"""
    default = "xl/worksheets/sheet1.xml"
    sheet = workbook.find(f"{_NS}sheets/{_NS}sheet")
    rels_path = "xl/_rels/workbook.xml.rels"
    if sheet is None or rels_path not in parts:
        return default
    rid = sheet.get(f"{_DOC_REL_NS}id")
    for rel in XmlET.fromstring(_read_part(zf, rels_path)).iter(f"{_PKG_REL_NS}Relationship"):
        if rel.get("Id") == rid:
            target = rel.get("Target") or ""
            path = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
            return path if path in parts else default
    return default


def _rich_text(node: XmlET.Element) -> str:
    """<si> / <is> 的文字：直接的 <t> 加富文本 <r><t>；不含注音 <rPh>。"""
    parts: list[str] = []
    for child in node:
        if child.tag == f"{_NS}t":
            parts.append(child.text or "")
        elif child.tag == f"{_NS}r":
            t = child.find(f"{_NS}t")
            parts.append((t.text or "") if t is not None else "")
    return "".join(parts)


def _cell_value(c: XmlET.Element, shared: list[str]) -> Any:
    kind = c.get("t", "n")
    if kind == "inlineStr":
        node = c.find(f"{_NS}is")
        return _rich_text(node) if node is not None else None
    v = c.find(f"{_NS}v")
    if v is None or v.text is None:
        return None
    if kind == "s":
        i = int(v.text)
        return shared[i] if 0 <= i < len(shared) else None
    if kind in ("str", "e"):
        return v.text
    if kind == "b":
        return v.text.strip() == "1"
    try:
        return float(v.text)
    except ValueError:
        return v.text


def _col_index(ref: str) -> int:
    m = _CELL_REF_RE.match(ref.upper())
    if not m:
        raise ValueError(f"无法识别的单元格引用：{ref}")
    n = 0
    for ch in m.group(1):
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def _find_header(rows: list[list[Any]]) -> tuple[int, dict[str, int]]:
    for idx, row in enumerate(rows):
        cols: dict[str, int] = {}
        for i, v in enumerate(row):
            if not isinstance(v, str):
                continue
            for field, keys in _JPX_HEADERS.items():
                if field not in cols and any(k in v for k in keys):
                    cols[field] = i
                    break
        if "day" in cols and "code" in cols:
            return idx, cols
    raise ValueError("找不到表头（決算発表予定日 / コード）：文件格式变了？")


def _find_as_of(rows: list[list[Any]]) -> date | None:
    for row in rows:
        for v in row:
            if not isinstance(v, str):
                continue
            for pat in _AS_OF_RES:
                m = pat.search(v)
                if m:
                    try:
                        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                    except ValueError:
                        continue
    return None


def _norm_code(v: Any) -> str | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, float):
        if not v.is_integer():
            return None
        v = str(int(v))
    s = unicodedata.normalize("NFKC", str(v)).strip().upper()
    return s if re.fullmatch(r"[0-9A-Z]{4}", s) else None


def _cell_date(v: Any, date1904: bool) -> date | None:
    """Excel 序列数或 “2026/10/30” 形式的文字 → date；“未定_Undecided” 等返回 None。"""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, float):
        return excel_serial_to_date(v, date1904) if 1 <= v < 2958466 else None  # 2958466 = 10000-01-01
    m = _DATE_TEXT_RE.search(unicodedata.normalize("NFKC", str(v)))
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def _text(v: Any) -> str:
    return "" if v is None else str(v).strip()
