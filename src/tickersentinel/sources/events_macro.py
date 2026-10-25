"""宏观日程：美联储、美国关键经济数据、长端国债拍卖、日本央行，以及按规则推算的韩国出口、美股期权到期与休市。

所有事件 kind = "macro"，symbol = 分组代码（见 MACRO_GROUPS：FED / US / UST / BOJ / KR / MKT），标题自带说明。
来源都是官方发布的日程（不需要 key），各自一个 EventSource，单个来源失败不影响其他来源（EventHub 负责）。

【2026-10-21 联网核实】
- 美联储（fed）：
  1) federalreserve.gov/monetarypolicy/fomccalendars.htm：每年一个面板（标题 “2026 FOMC Meetings”），每次会议一行：
     月份（跨月写 “Apr/May”）+ 日期（“27-28”，带 * 表示附经济预测摘要 SEP，即点阵图）。当年和下一年的会议都列出
     （2027 年 8 次已公布）。过去年份还有 “(notation vote)” “(unscheduled)” 之类的行，跳过。
  2) federalreserve.gov/json/calendar.json（约 540 KB，UTF-8 带 BOM）：{"events": [{title, description, time,
     month "2026-10", days "28"（统计发布可能是 "1, 2, 5"）, type}]}；type 有 FOMC / Speeches / Testimony / Beige / Stat …；
     时间是美东时间（“2:00 p.m.”），只覆盖到当年 12 月。用它取会议纪要日期、主席讲话与国会证词、褐皮书。
     讲话标题形如 “Speech - Chairman Kevin Warsh”（旧记录是 “Speech -- Chair Jerome H. Powell”），副主席是 “Vice Chair …”。
  决议声明 14:00 美东（calendar.json 有就以它为准），记者会 14:30。纪要通常在会后 3 周的周三 14:00 发布，
  calendar.json 还没列出的按会后 21 天估计（标为预计，列出后变为确认）。
- BLS（bls）：www.bls.gov/schedule/news_release/bls.ics。**BLS 拒绝不带联系方式的请求**（403；浏览器的 User-Agent 也不行），
  User-Agent 里要有邮箱（和 SEC 的要求一样），所以要在 .env 填 BLS_USER_AGENT；没填就跳过这个来源。
  SUMMARY 只有发布名（“Consumer Price Index”），没有数据所属月份：按发布月推算（CPI / PPI / 非农 = 上月，JOLTS = 前两个月，
  就业成本指数 = 上季度）。遇到政府停摆打乱发布节奏时，标题里的月份可能差一个月。
- BEA（bea）：apps.bea.gov/API/signup/release_dates.json（不需要 key）：{发布名: {"release_dates": [ISO 带时区, …]}}，
  覆盖到明年，偶有重复项。GDP：1/4/7/10 月发布上季度初值，次月修正值，再次月终值；PCE 物价在
  “Personal Income and Outlays” 里（上月数据）。BEA 的 ics 订阅停在 2025 年，不用。
- Census（census）：www.census.gov/economic-indicators/calendar-listview-YYYY.html，表格：指标 | 发布日期 | 时间 | 所属期间，
  日期单元格带 sorttable_customkey="YYYYMMDDHHMM"（美东）。零售销售 = “Advance Monthly Sales for Retail and Food Services”。
- 国债拍卖（treasury）：treasurydirect.gov/TA_WS/securities/upcoming?format=json，只有已公告的拍卖（约提前一周）。
  按 originalSecurityTerm 选期限（续发的 securityTerm 是 “9-Year 10-Month”），按 type 排除 TIPS 与 FRN（它们的 securityType
  也是 Note）；拍卖日取 competitiveAuctionCloseDate（没有 auctionDate 字段），结果在 competitiveAuctionCloseTime 公布
  （长端通常 13:00 美东；还没正式公告的拍卖这一项为空，按 13:00 记）。列表里也有“已排期、尚未公告”的拍卖。
- 日本央行（boj）：www.boj.or.jp/en/mopo/mpmsche_minu/index.htm，每年一张表（caption “Table : 2026”），第一列
  “Oct. 29 (Thurs.), 30 (Fri.)”，第二列不是 “-” 表示同时发布展望报告。决议在最后一天公布，时间不固定（通常东京中午前后，
  即美东前一晚），按 12:00 JST 记，标题注明“约”。
- 按规则推算（calendar_rules，不联网）：美股休市 / 提前收盘（exchange_calendars，确定）；月度期权到期 = 每月第三个周五
  （遇休市提前到前一个交易日；3/6/9/12 月为季度到期“四巫日”，确定）；韩国出口：产业通商资源部每月 1 日 09:00 KST
  公布上月进出口（周末、节假日照常），关税厅 21 日公布前 20 天出口（遇休息日顺延到下一个工作日）——按惯例推算，标为“预计”。
"""

from __future__ import annotations

import asyncio
import calendar as pycal
import hashlib
import html
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from tickersentinel.market_calendar import MarketCalendar
from tickersentinel.models import Event
from tickersentinel.sources.base import EventSource, SourceError
from tickersentinel.sources.http import make_client, request
from tickersentinel.timeutil import ET, UTC

log = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
JST = ZoneInfo("Asia/Tokyo")

# 宏观事件的 symbol（分组代码）→ 中文名
MACRO_GROUPS = {"FED": "美联储", "US": "美国数据", "UST": "美债拍卖", "BOJ": "日本央行", "KR": "韩国数据", "MKT": "美股市场"}

FOMC_PAGE_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
FED_CALENDAR_URL = "https://www.federalreserve.gov/json/calendar.json"
BLS_ICS_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
BEA_DATES_URL = "https://apps.bea.gov/API/signup/release_dates.json"
BEA_SCHEDULE_PAGE = "https://www.bea.gov/news/schedule"
CENSUS_LIST_URL = "https://www.census.gov/economic-indicators/calendar-listview-{year}.html"
TREASURY_UPCOMING_URL = "https://www.treasurydirect.gov/TA_WS/securities/upcoming?format=json"
TREASURY_PAGE = "https://www.treasurydirect.gov/auctions/upcoming/"
BOJ_PAGE_URL = "https://www.boj.or.jp/en/mopo/mpmsche_minu/index.htm"

# 官方网站对“没有浏览器标识”的请求常返回 403（实测 BEA、Census），统一带一个常见浏览器的 User-Agent
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
CACHE_TTL = 6 * 3600  # 日程一天刷新两次，6 小时缓存保证每次刷新都会重新下载

_MONTHS = {m.lower(): i for i, m in enumerate(pycal.month_name) if m} | {
    m.lower(): i for i, m in enumerate(pycal.month_abbr) if m} | {"sept": 9}


def _month_no(text: str) -> int | None:
    return _MONTHS.get(text.strip().rstrip(".").lower())


def _clean(text: str) -> str:
    """去掉 HTML 标签与实体，合并空白。calendar.json 的 description 是转义过的 HTML（&lt;p&gt;…）。"""
    text = html.unescape(html.unescape(text or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _short(text: str, n: int = 40) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _et(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, dtime(hh, mm), tzinfo=ET).astimezone(UTC)


def _prev_month(year: int, month: int, k: int = 1) -> tuple[int, int]:
    n = year * 12 + (month - 1) - k
    return n // 12, n % 12 + 1


def macro_event(group: str, key: str, title: str, day: date | None = None, at: datetime | None = None, *,
                source: str, confirmed: bool = True, time_hint: str = "", url: str = "", **details: Any) -> Event:
    """宏观事件。有精确时间时 day 取美东日期（与公司事件同一口径，见 models.Event）。"""
    if at is not None:
        day = at.astimezone(ET).date()
    assert day is not None
    return Event(kind="macro", symbol=group, day=day, title=title, key=f"macro:{key}", at=at, time_hint=time_hint,
                 confirmed=confirmed, source=source, url=url, details={"group": group, **details})


class _CachedSource(EventSource):
    """下载 → 解析 → 缓存。更新失败时沿用旧结果；从来没成功过才抛 SourceError。宏观事件与代码无关，symbols 不用。"""

    cache_ttl = CACHE_TTL

    def __init__(self, timeout: float = 20.0, client: httpx.AsyncClient | None = None, user_agent: str = BROWSER_UA):
        self._own_client = client is None
        self._client = client or make_client(timeout, headers={"User-Agent": user_agent})
        self._cache: list[Event] | None = None
        self._cached_at = 0.0
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._own_client:
            await self._client.aclose()

    async def collect(self, symbols: list[str], start: date, end: date) -> list[Event]:
        return [e for e in await self.events() if start <= e.day <= end]

    async def events(self) -> list[Event]:
        async with self._lock:
            if self._cache is not None and time.monotonic() - self._cached_at < self.cache_ttl:
                return self._cache
            try:
                fresh = await self.fetch()
            except SourceError as exc:
                if self._cache is None:
                    raise
                log.warning("%s 日程更新失败，沿用 %.1f 小时前的数据：%s", self.name,
                            (time.monotonic() - self._cached_at) / 3600, exc)
                return self._cache
            self._cache, self._cached_at = fresh, time.monotonic()
            return fresh

    async def fetch(self) -> list[Event]:
        raise NotImplementedError

    async def _get(self, url: str) -> httpx.Response:
        return await request(self._client, url, source=self.name)


# ───────────────────────────── 美联储 ─────────────────────────────

@dataclass(frozen=True)
class FomcMeeting:
    year: int
    n: int  # 当年第几次例会（从 1 起），作为稳定的 key
    start: date
    end: date
    sep: bool  # 是否附经济预测摘要（点阵图）

    @property
    def label(self) -> str:
        if self.start.month == self.end.month:
            return f"{self.start.month} 月 {self.start.day}–{self.end.day} 日"
        return f"{self.start.month} 月 {self.start.day} 日–{self.end.month} 月 {self.end.day} 日"


def parse_fomc_page(page: str) -> list[FomcMeeting]:
    """fomccalendars.htm → 各年的例会（跳过 notation vote / unscheduled / cancelled 这类行）。"""
    heads = list(re.finditer(r"(\d{4})\s+FOMC Meetings", page))
    out: list[FomcMeeting] = []
    for i, h in enumerate(heads):
        year = int(h.group(1))
        panel = page[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(page)]
        rows = re.findall(r"fomc-meeting__month[^>]*>\s*(?:<strong>)?([^<]+?)(?:</strong>)?\s*</div>.*?"
                          r"fomc-meeting__date[^>]*>([^<]+)</div>", panel, flags=re.S)
        n = 0
        for month_text, date_text in rows:
            date_text = _clean(date_text)
            m = re.fullmatch(r"(\d{1,2})(?:\s*-\s*(\d{1,2}))?\s*(\*)?", date_text)
            months = [_month_no(x) for x in _clean(month_text).split("/")]
            if m is None or not months or None in months:
                continue  # “(notation vote)”“(unscheduled)” 等
            d1 = int(m.group(1))
            d2 = int(m.group(2)) if m.group(2) else d1
            m1, m2 = months[0], months[-1]
            try:
                start = date(year, m1, d1)
                end = date(year, m2, d2)
            except ValueError:
                continue
            n += 1
            out.append(FomcMeeting(year, n, start, end, sep=bool(m.group(3))))
    return out


def _parse_fed_time(text: str) -> dtime | None:
    t = (text or "").strip().lower()
    if t.startswith("noon"):
        return dtime(12, 0)
    m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m", t)
    if not m:
        return None
    hh = int(m.group(1)) % 12 + (12 if m.group(3) == "p" else 0)
    return dtime(hh, int(m.group(2) or 0))


@dataclass(frozen=True)
class FedCalItem:
    type: str
    title: str
    description: str
    day: date
    at: datetime | None  # UTC


def parse_fed_calendar(raw: str | bytes) -> list[FedCalItem]:
    """calendar.json → 条目（一个条目列了多天的拆成多条）。"""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8-sig")
    data = json.loads(raw.lstrip("﻿"))
    out: list[FedCalItem] = []
    for e in data.get("events") or []:
        month = str(e.get("month") or "")
        mm = re.fullmatch(r"(\d{4})-(\d{2})", month)
        if not mm:
            continue
        t = _parse_fed_time(str(e.get("time") or ""))
        for d in re.findall(r"\d{1,2}", str(e.get("days") or "")):
            try:
                day = date(int(mm.group(1)), int(mm.group(2)), int(d))
            except ValueError:
                continue
            at = datetime.combine(day, t, tzinfo=ET).astimezone(UTC) if t else None
            out.append(FedCalItem(str(e.get("type") or ""), _clean(str(e.get("title") or "")),
                                  _clean(str(e.get("description") or "")), day, at))
    return out


_CHAIR_RE = re.compile(r"^(?P<kind>[A-Za-z ]+?)\s*-{1,2}\s*(?:Chairman|Chairwoman|Chair)\s+(?P<name>.+)$")
CHAIR_VERBS = {"speech": "讲话", "discussion": "对话", "testimony": "国会作证", "remarks": "致辞", "panel": "座谈",
               "opening remarks": "致辞", "welcoming remarks": "致辞", "fireside chat": "对话"}


def _meeting_of(desc: str, release: date) -> date | None:
    """纪要的 description “Meeting of October 27-28”（或 “April 30-May 1”）→ 会议最后一天。"""
    m = re.search(r"Meeting of\s+([A-Za-z]+)\.?\s+(\d{1,2})(?:\s*-\s*(?:([A-Za-z]+)\.?\s+)?(\d{1,2}))?", desc)
    if not m:
        return None
    month = _month_no(m.group(3) or m.group(1))
    if month is None:
        return None
    day = int(m.group(4) or m.group(2))
    year = release.year - (1 if month > release.month else 0)
    try:
        return date(year, month, day)
    except ValueError:
        return None


def fed_events(meetings: list[FomcMeeting], items: list[FedCalItem], wanted: set[str]) -> list[Event]:
    """例会（官方页面）+ calendar.json 的纪要 / 主席讲话 / 证词 / 褐皮书 → 事件。wanted ⊆ {fomc, minutes, chair, beige_book}。"""
    out: list[Event] = []
    decision_time = {i.day: i.at for i in items if i.type == "FOMC" and i.title.lower().startswith("fomc meeting") and i.at}
    if "fomc" in wanted:
        for m in meetings:
            at = decision_time.get(m.end) or _et(m.end, 14)
            title = "美联储议息决议 + 记者会" + ("（含点阵图与经济预测）" if m.sep else "")
            out.append(macro_event("FED", f"fomc:{m.year}-{m.n}", title, at=at, source="fed", url=FOMC_PAGE_URL,
                                   meeting=m.label, sep=m.sep))
    if "minutes" in wanted:
        listed: dict[date, Event] = {}
        for i in items:
            if i.type == "FOMC" and "minutes" in i.title.lower():
                end = _meeting_of(i.description, i.day)
                if end is None:
                    continue
                listed[end] = macro_event("FED", f"fomc_minutes:{end.isoformat()}", f"美联储会议纪要（{end.month} 月会议）",
                                          at=i.at or _et(i.day, 14), source="fed", url=FOMC_PAGE_URL)
        out += listed.values()
        for m in meetings:  # 还没列进 calendar.json 的：按会后 21 天估计
            if m.end not in listed:
                out.append(macro_event("FED", f"fomc_minutes:{m.end.isoformat()}", f"美联储会议纪要（{m.end.month} 月会议）",
                                       at=_et(m.end + timedelta(days=21), 14), source="fed", confirmed=False,
                                       url=FOMC_PAGE_URL))
    for i in items:
        if "chair" in wanted and i.type in ("Speeches", "Testimony"):
            m = _CHAIR_RE.match(i.title)
            if m is None or "vice" in i.title.lower():
                continue
            verb = CHAIR_VERBS.get(m.group("kind").strip().lower(), "讲话")
            name = m.group("name").strip()
            title = f"美联储主席 {name} {verb}" + (f"：{_short(i.description)}" if i.description else "")
            digest = hashlib.sha1(i.title.encode()).hexdigest()[:8]
            out.append(macro_event("FED", f"fed_chair:{i.day.isoformat()}:{digest}", title, day=i.day, at=i.at,
                                   source="fed", url="https://www.federalreserve.gov/newsevents/calendar.htm"))
        elif "beige_book" in wanted and i.type == "Beige":
            out.append(macro_event("FED", f"beige:{i.day.isoformat()}", "美联储褐皮书", day=i.day, at=i.at, source="fed",
                                   url="https://www.federalreserve.gov/monetarypolicy/beige-book-default.htm"))
    return out


class FedEvents(_CachedSource):
    name = "fed"

    def __init__(self, wanted: list[str], timeout: float = 20.0, client: httpx.AsyncClient | None = None):
        super().__init__(timeout, client)
        self.wanted = set(wanted)

    async def fetch(self) -> list[Event]:
        page = await self._get(FOMC_PAGE_URL)
        meetings = parse_fomc_page(page.text)
        if not meetings:
            raise SourceError(f"[fed] 议息会议页面里没有解析出会议（页面改版？）：{FOMC_PAGE_URL}")
        try:
            items = parse_fed_calendar((await self._get(FED_CALENDAR_URL)).content)
        except (SourceError, ValueError) as exc:  # 只影响纪要的确认日期、主席讲话和褐皮书，例会照常
            log.warning("美联储日历 calendar.json 读取失败（纪要按会后 21 天估计，主席讲话暂缺）：%s", exc)
            items = []
        return fed_events(meetings, items, self.wanted)


# ───────────────────────────── 美国经济数据 ─────────────────────────────

@dataclass(frozen=True)
class DataRelease:
    key: str
    name: str  # 中文名
    lag_months: int = 1  # 数据所属期间比发布月早几个月
    quarterly: bool = False


# BLS 发布名（SUMMARY 原文，精确匹配；注意 “State Job Openings and Labor Turnover” 是另一份）
BLS_RELEASES = {
    "Consumer Price Index": DataRelease("cpi", "美国 CPI"),
    "Producer Price Index": DataRelease("ppi", "美国 PPI"),
    "Employment Situation": DataRelease("jobs", "美国非农就业与失业率"),
    "Job Openings and Labor Turnover Survey": DataRelease("jolts", "美国 JOLTS 职位空缺", lag_months=2),
    "Employment Cost Index": DataRelease("eci", "美国就业成本指数", quarterly=True),
}


def _period_label(rel: DataRelease, released: date) -> tuple[str, str]:
    """(key 用的期间, 标题用的期间)。季度数据取发布前的那个季度。"""
    if rel.quarterly:
        y, m = _prev_month(released.year, released.month, 1)
        q = (m - 1) // 3 + 1
        return f"{y}Q{q}", f"{'一二三四'[q - 1]}季度"
    y, m = _prev_month(released.year, released.month, rel.lag_months)
    return f"{y}-{m:02d}", f"{m} 月"


def parse_ics(text: str) -> list[tuple[datetime, str]]:
    """极简 iCalendar 解析：(开始时间 UTC, SUMMARY)。BLS 用 TZID=US-Eastern 的本地时间。"""
    text = re.sub(r"\r?\n[ \t]", "", text.replace("\r\n", "\n"))  # 折行
    out: list[tuple[datetime, str]] = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, flags=re.S):
        dt_m = re.search(r"^DTSTART([^:\n]*):(\d{8})(?:T(\d{4,6}))?(Z?)\s*$", block, flags=re.M)
        sm = re.search(r"^SUMMARY[^:\n]*:(.*)$", block, flags=re.M)
        if not dt_m or not sm:
            continue
        params, ymd, hms, z = dt_m.groups()
        hms = (hms or "000000").ljust(6, "0")
        naive = datetime.strptime(ymd + hms, "%Y%m%d%H%M%S")
        if z:
            at = naive.replace(tzinfo=UTC)
        else:
            tzid = re.search(r"TZID=([^;:]+)", params or "")
            tz = ET if not tzid or "eastern" in tzid.group(1).lower() or "new_york" in tzid.group(1).lower() \
                else ZoneInfo(tzid.group(1))
            at = naive.replace(tzinfo=tz).astimezone(UTC)
        summary = sm.group(1).replace("\\,", ",").replace("\\;", ";").strip()
        out.append((at, summary))
    return out


def bls_events(entries: list[tuple[datetime, str]], wanted: set[str]) -> list[Event]:
    out: list[Event] = []
    for at, summary in entries:
        rel = BLS_RELEASES.get(summary)
        if rel is None or rel.key not in wanted:
            continue
        released = at.astimezone(ET).date()
        pkey, plabel = _period_label(rel, released)
        out.append(macro_event("US", f"{rel.key}:{pkey}", f"{rel.name}（{plabel}）", at=at, source="bls",
                               url="https://www.bls.gov/schedule/news_release/", period=pkey))
    return out


class BLSEvents(_CachedSource):
    name = "bls"

    def __init__(self, wanted: list[str], user_agent: str, timeout: float = 20.0, client: httpx.AsyncClient | None = None):
        super().__init__(timeout, client, user_agent=user_agent)
        self.wanted = set(wanted)

    async def fetch(self) -> list[Event]:
        resp = await self._get(BLS_ICS_URL)
        entries = parse_ics(resp.text)
        if not entries:
            raise SourceError("[bls] 发布日程 ics 里没有条目（格式变了？）")
        return bls_events(entries, self.wanted)


GDP_STAGES = {0: "初值", 1: "修正值", 2: "终值"}


def bea_events(data: dict, wanted: set[str]) -> list[Event]:
    """release_dates.json → GDP / PCE。"""
    out: list[Event] = []
    for name, info in data.items():
        if not isinstance(info, dict):
            continue  # file_last_updated 等
        dates = sorted({datetime.fromisoformat(x).astimezone(UTC) for x in info.get("release_dates") or []})
        for at in dates:
            released = at.astimezone(ET).date()
            if name == "Gross Domestic Product" and "gdp" in wanted:
                stage = (released.month - 1) % 3  # 1/4/7/10 月 = 初值
                y, m = _prev_month(released.year, released.month, stage + 1)
                q = (m - 1) // 3 + 1
                title = f"美国 GDP（{y} 年{'一二三四'[q - 1]}季度{GDP_STAGES[stage]}）"
                out.append(macro_event("US", f"gdp:{y}Q{q}:{stage + 1}", title, at=at, source="bea", url=BEA_SCHEDULE_PAGE))
            elif name == "Personal Income and Outlays" and "pce" in wanted:
                y, m = _prev_month(released.year, released.month, 1)
                out.append(macro_event("US", f"pce:{y}-{m:02d}", f"美国 PCE 物价与个人收支（{m} 月）", at=at, source="bea",
                                       url=BEA_SCHEDULE_PAGE))
    return out


class BEAEvents(_CachedSource):
    name = "bea"

    def __init__(self, wanted: list[str], timeout: float = 20.0, client: httpx.AsyncClient | None = None):
        super().__init__(timeout, client)
        self.wanted = set(wanted)

    async def fetch(self) -> list[Event]:
        resp = await self._get(BEA_DATES_URL)
        try:
            data = resp.json()
        except ValueError as exc:
            raise SourceError(f"[bea] release_dates.json 不是 JSON：{resp.text[:200]!r}") from exc
        return bea_events(data, self.wanted)


CENSUS_RELEASES = {
    "Advance Monthly Sales for Retail and Food Services": ("retail_sales", "美国零售销售"),
    "Advance Report on Durable Goods--Manufacturers' Shipments, Inventories, and Orders": ("durable_goods", "美国耐用品订单"),
}


def parse_census_listview(page: str) -> list[tuple[str, datetime, str]]:
    """(指标名, 发布时间 UTC, 所属期间原文)。"""
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", page, flags=re.S):
        key = re.search(r'sorttable_customkey="(\d{12})"', tr)
        cells = [_clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S)]
        if not key or len(cells) < 4:
            continue
        at = datetime.strptime(key.group(1), "%Y%m%d%H%M").replace(tzinfo=ET).astimezone(UTC)
        out.append((cells[0], at, cells[3]))
    return out


def census_events(rows: list[tuple[str, datetime, str]], wanted: set[str]) -> list[Event]:
    out: dict[str, Event] = {}
    for name, at, period in rows:
        hit = next((v for k, v in CENSUS_RELEASES.items() if name.startswith(k[:40])), None)
        if hit is None or hit[0] not in wanted:
            continue
        key, cname = hit
        pm = re.fullmatch(r"([A-Za-z]+)\s+(\d{4})", period.strip())
        month = _month_no(pm.group(1)) if pm else None
        if pm and month:
            pkey, plabel = f"{pm.group(2)}-{month:02d}", f"{month} 月"
        else:
            pkey, plabel = at.astimezone(ET).date().isoformat(), period or ""
        out[f"{key}:{pkey}"] = macro_event("US", f"{key}:{pkey}", f"{cname}（{plabel}）" if plabel else cname, at=at,
                                           source="census", url="https://www.census.gov/economic-indicators/")
    return list(out.values())


class CensusEvents(_CachedSource):
    name = "census"

    def __init__(self, wanted: list[str], timeout: float = 20.0, client: httpx.AsyncClient | None = None,
                 today: date | None = None):
        super().__init__(timeout, client)
        self.wanted = set(wanted)
        self._today = today

    async def fetch(self) -> list[Event]:
        year = (self._today or datetime.now(ET).date()).year
        rows: list[tuple[str, datetime, str]] = []
        errors: list[str] = []
        for y in (year, year + 1):  # 下一年的日程通常在年底前就公布了；还没公布时 404，忽略
            try:
                rows += parse_census_listview((await self._get(CENSUS_LIST_URL.format(year=y))).text)
            except SourceError as exc:
                errors.append(str(exc))
        if not rows:
            raise SourceError("[census] 经济指标日程没有取到：" + "；".join(errors))
        return census_events(rows, self.wanted)


# ───────────────────────────── 国债拍卖 ─────────────────────────────

def treasury_events(items: list[dict], terms: list[str]) -> list[Event]:
    """upcoming 接口 → 选定期限的拍卖。terms 形如 ["10Y", "20Y", "30Y"]。"""
    wanted = {f"{t.upper().rstrip('Y')}-Year" for t in terms}
    out: list[Event] = []
    for x in items:
        term = str(x.get("originalSecurityTerm") or x.get("securityTerm") or "")
        if term not in wanted or str(x.get("inflationProtected") or "").lower() == "yes":
            continue
        if str(x.get("type") or x.get("securityType") or "") not in ("Note", "Bond"):
            continue  # 浮动利率票据（type = FRN，securityType 仍是 Note）、TIPS 等
        try:
            day = date.fromisoformat(str(x.get("auctionDate") or x.get("competitiveAuctionCloseDate") or "")[:10])
        except ValueError:
            continue
        t = _parse_fed_time(str(x.get("competitiveAuctionCloseTime") or x.get("closingTimeCompetitive") or "")) or dtime(13, 0)
        at = datetime.combine(day, t, tzinfo=ET).astimezone(UTC)
        years = term.split("-")[0]
        bits = ["续发"] if str(x.get("reopening") or "").lower() == "yes" else []
        try:
            amount = float(x.get("offeringAmount") or 0)
        except ValueError:
            amount = 0
        if amount:
            bits.append(f"{amount / 1e8:.0f} 亿美元")
        title = f"美国 {years} 年期国债拍卖" + (f"（{'，'.join(bits)}）" if bits else "")
        out.append(macro_event("UST", f"auction:{x.get('cusip') or term}:{day.isoformat()}", title, at=at,
                               source="treasury", url=TREASURY_PAGE, term=term))
    return out


class TreasuryAuctionEvents(_CachedSource):
    name = "treasury"
    cache_ttl = 3 * 3600

    def __init__(self, terms: list[str], timeout: float = 20.0, client: httpx.AsyncClient | None = None):
        super().__init__(timeout, client)
        self.terms = list(terms)

    async def fetch(self) -> list[Event]:
        resp = await self._get(TREASURY_UPCOMING_URL)
        try:
            items = resp.json()
        except ValueError as exc:
            raise SourceError(f"[treasury] 返回的不是 JSON：{resp.text[:200]!r}") from exc
        if not isinstance(items, list):
            raise SourceError("[treasury] 返回格式不对（不是列表）")
        return treasury_events(items, self.terms)


# ───────────────────────────── 日本央行 ─────────────────────────────

def parse_boj_schedule(page: str) -> list[tuple[int, int, date, bool]]:
    """(年, 当年第几次, 决议日, 是否发布展望报告)。"""
    out = []
    for cap in re.finditer(r"Table\s*:\s*(\d{4})", page):
        year = int(cap.group(1))
        body_end = page.find("</table>", cap.end())
        body = page[cap.end(): body_end if body_end > 0 else len(page)]
        n = 0
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body, flags=re.S):
            cells = [_clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S)]
            if not cells:
                continue
            first = re.sub(r"\[PDF[^\]]*\]", "", cells[0])
            month: int | None = None
            last: date | None = None
            for mon, d in re.findall(r"(?:([A-Z][a-z]+)\.?\s+)?(\d{1,2})\s*\(", first):
                if mon:
                    month = _month_no(mon)
                if month is None:
                    break
                try:
                    last = date(year, month, int(d))
                except ValueError:
                    last = None
                    break
            if last is None:
                continue
            n += 1
            outlook = len(cells) > 1 and cells[1].strip() not in ("-", "", "－")
            out.append((year, n, last, outlook))
    return out


def boj_events(rows: list[tuple[int, int, date, bool]]) -> list[Event]:
    out = []
    for year, n, day, outlook in rows:
        at = datetime.combine(day, dtime(12, 0), tzinfo=JST).astimezone(UTC)
        title = "日本央行议息决议（" + ("含展望报告；" if outlook else "") + "时间不固定，约为东京中午）"
        out.append(macro_event("BOJ", f"boj:{year}-{n}", title, at=at, source="boj", url=BOJ_PAGE_URL, outlook=outlook))
    return out


class BOJEvents(_CachedSource):
    name = "boj"

    async def fetch(self) -> list[Event]:
        rows = parse_boj_schedule((await self._get(BOJ_PAGE_URL)).text)
        if not rows:
            raise SourceError(f"[boj] 议息会议日程页面没有解析出会议（页面改版？）：{BOJ_PAGE_URL}")
        return boj_events(rows)


# ───────────────────────────── 按规则推算 ─────────────────────────────

NYSE_HOLIDAYS = {
    "New Year's Day": "元旦", "Dr. Martin Luther King Jr. Day": "马丁·路德·金纪念日", "President's Day": "总统日",
    "Good Friday": "耶稣受难日", "Memorial Day": "阵亡将士纪念日", "Juneteenth National Independence Day": "六月节",
    "July 4th": "独立日", "Labor Day": "劳动节", "Thanksgiving": "感恩节", "Christmas": "圣诞节",
}


def _months(start: date, end: date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def _safe_session(cal: MarketCalendar, day: date) -> bool | None:
    """exchange_calendars 只覆盖到约一年后，超出范围返回 None。"""
    try:
        return cal.is_session(day)
    except Exception:
        return None


def third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


def us_market_events(cal: MarketCalendar, start: date, end: date) -> list[Event]:
    """工作日休市与提前收盘。"""
    names = cal.holiday_names(start, end)
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            is_s = _safe_session(cal, d)
            if is_s is None:
                break
            if not is_s:
                name = NYSE_HOLIDAYS.get(names.get(d, ""), names.get(d) or "临时休市")
                out.append(macro_event("MKT", f"us_holiday:{d.isoformat()}", f"美股休市（{name}）", day=d,
                                       source="calendar_rules"))
            else:
                s = cal.session(d)
                if s is not None and s.early_close:
                    close_et = s.close.astimezone(ET)
                    out.append(macro_event("MKT", f"us_early_close:{d.isoformat()}",
                                           f"美股提前收盘（美东 {close_et:%H:%M} 收盘，盘后交易也提前结束）",
                                           at=s.close, source="calendar_rules"))
        d += timedelta(days=1)
    return out


def opex_events(cal: MarketCalendar, start: date, end: date) -> list[Event]:
    """美股月度期权到期（第三个周五，遇休市提前到前一个交易日）。"""
    out = []
    for y, m in _months(start, end):
        d = third_friday(y, m)
        is_s = _safe_session(cal, d)
        if is_s is None:
            break
        if not is_s:
            d = cal.previous_session(d).day
        quarterly = m in (3, 6, 9, 12)
        title = "美股季度期权到期（四巫日）" if quarterly else "美股月度期权到期"
        out.append(macro_event("MKT", f"opex:{y}-{m:02d}", title, day=d, time_hint="收盘", source="calendar_rules",
                               quarterly=quarterly))
    return out


def korea_export_events(krx: MarketCalendar, start: date, end: date) -> list[Event]:
    """韩国月度进出口（每月 1 日 09:00 KST，上月数据）与前 20 天出口（21 日，遇休息日顺延），按惯例推算。"""
    out = []
    for y, m in _months(start - timedelta(days=31), end + timedelta(days=31)):
        py, pm = _prev_month(y, m)
        at = datetime.combine(date(y, m, 1), dtime(9, 0), tzinfo=KST).astimezone(UTC)
        out.append(macro_event("KR", f"kr_exports:{py}-{pm:02d}", f"韩国 {pm} 月进出口", at=at,
                               source="calendar_rules", confirmed=False,
                               url="https://www.motie.go.kr/", note="按惯例推算：产业通商资源部每月 1 日 09:00（首尔）公布"))
        d = date(y, m, 21)
        for _ in range(10):
            is_s = _safe_session(krx, d)
            if is_s is None or is_s:
                break
            d += timedelta(days=1)
        at = datetime.combine(d, dtime(9, 0), tzinfo=KST).astimezone(UTC)
        out.append(macro_event("KR", f"kr_exports_20d:{y}-{m:02d}", f"韩国 {m} 月前 20 天出口（关税厅）", at=at,
                               source="calendar_rules", confirmed=False, url="https://www.customs.go.kr/",
                               note="按惯例推算：关税厅每月 21 日（遇休息日顺延）09:00（首尔）公布"))
    return [e for e in out if start <= e.day <= end]


class CalendarRuleEvents(EventSource):
    """不联网：按交易所日历和惯例推算的日程。"""

    name = "calendar_rules"

    def __init__(self, *, us_market: bool = True, opex: bool = True, korea_exports: bool = True,
                 calendar: MarketCalendar | None = None, krx: MarketCalendar | None = None):
        self.us_market, self.opex, self.korea_exports = us_market, opex, korea_exports
        self.calendar = calendar or MarketCalendar()
        self._krx = krx

    async def collect(self, symbols: list[str], start: date, end: date) -> list[Event]:
        out: list[Event] = []
        if self.us_market:
            out += us_market_events(self.calendar, start, end)
        if self.opex:
            out += opex_events(self.calendar, start, end)
        if self.korea_exports:
            if self._krx is None:
                self._krx = MarketCalendar("XKRX")
            out += korea_export_events(self._krx, start, end)
        return out


# ───────────────────────────── 组装 ─────────────────────────────

def build_macro_event_sources(settings) -> list[EventSource]:
    cfg = settings.events.macro
    if not cfg.enabled:
        return []
    timeout = settings.sources.http_timeout_seconds
    out: list[EventSource] = []
    if cfg.fed:
        out.append(FedEvents(cfg.fed, timeout))
    bls = [k for k in cfg.us_data if k in {r.key for r in BLS_RELEASES.values()}]
    if bls:
        ua = settings.secrets.get("bls_user_agent")
        if ua:
            out.append(BLSEvents(bls, ua, timeout))
        else:
            log.warning("未配置 BLS_USER_AGENT：跳过 CPI / PPI / 非农 / JOLTS 的发布日程（BLS 要求请求头带联系邮箱，见 README）")
    bea = [k for k in cfg.us_data if k in ("gdp", "pce")]
    if bea:
        out.append(BEAEvents(bea, timeout))
    census = [k for k in cfg.us_data if k in {v[0] for v in CENSUS_RELEASES.values()}]
    if census:
        out.append(CensusEvents(census, timeout))
    if cfg.treasury_auctions:
        out.append(TreasuryAuctionEvents(cfg.treasury_auctions, timeout))
    if cfg.boj:
        out.append(BOJEvents(timeout))
    if cfg.us_market or cfg.opex or cfg.korea_exports:
        out.append(CalendarRuleEvents(us_market=cfg.us_market, opex=cfg.opex, korea_exports=cfg.korea_exports))
    return out
