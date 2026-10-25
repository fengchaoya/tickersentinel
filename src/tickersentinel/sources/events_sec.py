"""SEC EDGAR 里的“提前知道”的事件：Form 144（内部人拟减持通知）、外国发行人 6-K 里的业绩发布/电话会预告。

复用 filings_sec_edgar.SecEdgarFilings 的 HTTP 客户端与限速器（同一个实例，全局守住 SEC 的 10 次/秒），
流程都是：submissions JSON（filings.recent）→ 按 filingDate 粗筛最近 lookback_days 天 → 逐份下载原文 → 纯函数解析。
submissions JSON 的 acceptanceDateTime 目前不可靠（见 filings_sec_edgar 的文件说明），这里不用它：
事件日期取自公告正文，粗筛只用 filingDate。原文按 accession 缓存在实例里（公告提交后不会再变）。

【Form 144 —— 2026-10-20 联网核实（MU、SNDK 以及当天 EDGAR 全部 144 中抽样 25 份）】
- 提交人是拟出售的内部人，但公告同时挂在发行人名下，所以发行人的 submissions JSON 里能看到 "144" / "144/A"。
- primaryDocument 是 XSL 渲染页 "xsl144X01/primary_doc.xml"（返回 HTML）；原始 XML 在同一目录下的
  "primary_doc.xml"（去掉 xsl 前缀即可，目录的 index.json 里也只列出这一份 XML；找不到时再查 index.json）。
- 根元素 edgarSubmission，命名空间 http://www.sec.gov/edgar/ownership；前缀因申报代理而异（默认命名空间、
  "ns2:"、地址里的 "com:" / "ns2:" 都见过），解析时一律按本地名匹配。用到的元素：
    headerData/submissionType                     144 或 144/A
    headerData/previousAccessionNumber            仅 144/A：被修订的那份
    issuerInfo/issuerCik                          发行人 CIK（用来排除“本公司作为出售人”的 144）
    issuerInfo/nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold   拟出售人（大小写、姓名顺序随申报代理而异）
    issuerInfo/relationshipsToIssuer/relationshipToIssuer*           Officer / Director / 10% Stockholder ……
    securitiesInformation*（每个券商一组）：brokerOrMarketmakerDetails/name、noOfUnitsSold、aggregateMarketValue（美元，
      按提交时市价估算）、noOfUnitsOutstanding、approxSaleDate（MM/DD/YYYY）、securitiesClassTitle、securitiesExchangeName
    remarks、noticeSignature/noticeDate、noticeSignature/planAdoptionDates/planAdoptionDate*（10b5-1 计划采纳日）
- 关系只有 Officer 这一级，没有具体职务（CEO / CFO 要从 Form 4 的 officerTitle 才能看到）。
- **144 只是“拟出售”通知**（规则 144 要求在下单时或之前提交），不证明真的成交、也不保证按这个数量成交；
  实际成交以随后的 Form 4 为准。所以 confirmed=True 的含义是“已正式提交通知”，不是“已经卖出”。
- 事件日期取 approxSaleDate（“大约出售日期”），读不到时用 filingDate。
- 144/A：key 沿用被修订那份的 accession（previousAccessionNumber），这样修订会覆盖原事件而不是新增一条。

【6-K 业绩预告 —— 2026-10-20 联网核实（SK海力士 CIK 2120882，2026-07～10 全部 22 份 6-K）】
- SK海力士 在电话会前约两周提交一份 6-K：“plans to hold a conference call for the 2026 3Q earnings results”，
  正文写 “Date and Time  October 28, 2026 9:00 a.m. (Seoul Time)”（0001193125-26-420934，10-14 提交；
  7 月那份是 0001193125-26-303983，7-15 提交，电话会 7-29 09:00 首尔时间）。内容就在主文档里，没有附件。
- 业绩当天的 “Preliminary Results of Operations” 6-K 也写了 “(Expected) Date / Time of Provision July 29, 2026 /
  9:00 a.m. (Seoul time)”，会被识别为同一事件（同一个 key、同一个时间），不会重复。
- 其余 6-K（澄清传闻、投资、回购、分红、半年报……）没有“业绩 + 电话会/发布”的组合或没有带时间的未来日期，忽略。
- 解析：只看封面（“Form 40-F ☐”）之后、“SIGNATURE” 之前的正文（排除签名日期）；要求同时出现“业绩”和
  “电话会/将发布”类字眼，再找“日期 + 时刻 + 时区”（时区缺失时只记日期）。首尔 09:00 = 美东前一天 20:00，
  所以 Event.day 是美东日期（10-28 09:00 KST → day = 10-27），details 里保留当地时间。
- 只看主文档：把业绩预告放在附件（Exhibit 99.1）里的发行人目前识别不到（SK海力士 不需要）。
"""

from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET_XML
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from tickersentinel.config import is_us_symbol
from tickersentinel.models import Event, earnings_key
from tickersentinel.sources.base import EventSource, SourceError
from tickersentinel.sources.filings_sec_edgar import (
    ARCHIVE_URL, MAX_FILINGS_PER_SYMBOL, SUBMISSIONS_URL, Filing, SecEdgarFilings, list_filings,
)
from tickersentinel.timeutil import ET, UTC, utcnow

log = logging.getLogger(__name__)

INDEX_JSON_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/index.json"
MAX_HTML_CHARS = 300_000  # 6-K 原文只看前 30 万字符（半年报这类大文件可达 4MB，业绩预告只有 1～2 万字符）
BODY_CHARS = 6_000  # 正文里只在前 6000 字符内找业绩预告
MAX_DAYS_AHEAD = 180  # 预告日期最多比提交日晚多少天（再远多半是误识别）


def _today_et() -> date:
    return utcnow().astimezone(ET).date()


# ───────────────────────── Form 144 ─────────────────────────

RELATIONSHIP_NAMES = {
    "director": "董事", "officer": "高管", "10% stockholder": "持股 10% 以上股东", "10% securityholder": "持股 10% 以上股东",
    "member of immediate family of any of the foregoing": "上述人员的直系亲属", "other": "其他关联人",
}
_RELATIONSHIP_ORDER = ["董事", "高管", "持股 10% 以上股东", "上述人员的直系亲属", "其他关联人"]
_XSL_PREFIX = re.compile(r"^xsl[^/]*/")


@dataclass(frozen=True)
class Form144:
    """一份 Form 144 的解析结果（多个券商时，数量与市值相加、出售日期取最早）。"""

    form: str  # "144" / "144/A"
    previous_accession: str | None
    issuer_cik: int | None
    issuer_name: str
    person: str
    relationships: tuple[str, ...]  # 原文，如 ("Director", "Officer")
    security_class: str
    brokers: tuple[str, ...]
    shares: float | None
    market_value: float | None  # 美元
    shares_outstanding: float | None
    approx_sale_date: date | None
    exchange: str
    plan_adoption_dates: tuple[date, ...]
    remarks: str
    notice_date: date | None

    @property
    def amended(self) -> bool:
        return self.form.upper().endswith("/A")

    @property
    def pct_outstanding(self) -> float | None:
        if not self.shares or not self.shares_outstanding:
            return None
        return self.shares / self.shares_outstanding * 100


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el: ET_XML.Element | None, name: str) -> ET_XML.Element | None:
    if el is None:
        return None
    return next((c for c in el if _local(c.tag) == name), None)


def _children(el: ET_XML.Element | None, name: str) -> list[ET_XML.Element]:
    return [] if el is None else [c for c in el if _local(c.tag) == name]


def _path(el: ET_XML.Element | None, *names: str) -> ET_XML.Element | None:
    for n in names:
        el = _child(el, n)
    return el


def _text(el: ET_XML.Element | None) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip() if el is not None else ""


def _number(s: str) -> float | None:
    s = s.replace(",", "").replace("$", "").strip()
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _us_date(s: str) -> date | None:
    """Form 144 的日期：MM/DD/YYYY（偶见 YYYY-MM-DD）。"""
    s = s.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_form144(content: bytes | str) -> Form144:
    """Form 144 原始 XML → Form144。不是 144 或 XML 损坏时抛 ValueError。"""
    raw = content.encode("utf-8") if isinstance(content, str) else content
    if b"<!DOCTYPE" in raw[:2000] or b"<!ENTITY" in raw:
        raise ValueError("Form 144 XML 不应包含 DOCTYPE/ENTITY")
    try:
        root = ET_XML.fromstring(raw)
    except ET_XML.ParseError as exc:
        raise ValueError(f"Form 144 XML 解析失败：{exc}") from exc
    if _local(root.tag) != "edgarSubmission":
        raise ValueError(f"根元素不是 edgarSubmission：{_local(root.tag)}")
    header, form_data = _child(root, "headerData"), _child(root, "formData")
    form = _text(_child(header, "submissionType")) or "144"
    if not form.upper().startswith("144") or form_data is None:
        raise ValueError(f"不是 Form 144：{form}")

    issuer = _child(form_data, "issuerInfo")
    infos = _children(form_data, "securitiesInformation")
    shares = [v for v in (_number(_text(_child(i, "noOfUnitsSold"))) for i in infos) if v is not None]
    values = [v for v in (_number(_text(_child(i, "aggregateMarketValue"))) for i in infos) if v is not None]
    outstanding = [v for v in (_number(_text(_child(i, "noOfUnitsOutstanding"))) for i in infos) if v is not None]
    sale_days = [d for d in (_us_date(_text(_child(i, "approxSaleDate"))) for i in infos) if d is not None]
    brokers = [_text(_path(i, "brokerOrMarketmakerDetails", "name")) for i in infos]
    signature = _child(form_data, "noticeSignature")
    plan_days = [_us_date(_text(e)) for e in _children(_child(signature, "planAdoptionDates"), "planAdoptionDate")]
    cik = _text(_child(issuer, "issuerCik"))
    return Form144(
        form=form,
        previous_accession=_text(_child(header, "previousAccessionNumber")) or None,
        issuer_cik=int(cik) if cik.isdigit() else None,
        issuer_name=_text(_child(issuer, "issuerName")),
        person=_text(_child(issuer, "nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold")),
        relationships=tuple(t for t in (_text(e) for e in _children(_child(issuer, "relationshipsToIssuer"),
                                                                       "relationshipToIssuer")) if t),
        security_class=next((t for t in (_text(_child(i, "securitiesClassTitle")) for i in infos) if t), ""),
        brokers=tuple(dict.fromkeys(b for b in brokers if b)),
        shares=sum(shares) if shares else None,
        market_value=sum(values) if values else None,
        shares_outstanding=max(outstanding) if outstanding else None,
        approx_sale_date=min(sale_days) if sale_days else None,
        exchange=next((t for t in (_text(_child(i, "securitiesExchangeName")) for i in infos) if t), ""),
        plan_adoption_dates=tuple(sorted(d for d in plan_days if d is not None)),
        remarks=_text(_child(form_data, "remarks")),
        notice_date=_us_date(_text(_child(signature, "noticeDate"))),
    )


def raw_xml_name(primary_document: str) -> str:
    """submissions JSON 的 primaryDocument（XSL 渲染页，如 "xsl144X01/primary_doc.xml"）→ 原始 XML 的文件名。"""
    return _XSL_PREFIX.sub("", primary_document.strip()) or "primary_doc.xml"


def relationship_cn(relationships: tuple[str, ...] | list[str]) -> str:
    """("Director", "Officer") → "董事兼高管"；没有对应译名的保留原文。"""
    names = list(dict.fromkeys(RELATIONSHIP_NAMES.get(r.strip().lower(), r.strip())
                               for r in relationships if r.strip()))
    names.sort(key=lambda n: _RELATIONSHIP_ORDER.index(n) if n in _RELATIONSHIP_ORDER else len(_RELATIONSHIP_ORDER))
    return "兼".join(names) if len(names) <= 2 else "、".join(names)


def display_person(name: str) -> str:
    """全大写的姓名（"DAVID GOECKELER"）转成首字母大写；其余写法（含“姓 名”顺序）原样保留。"""
    name = re.sub(r"\s+", " ", name).strip()
    return name.title() if name.isupper() else name


def _trim(x: float, digits: int) -> str:
    return f"{x:,.{digits}f}".rstrip("0").rstrip(".") if digits else f"{x:,.0f}"


def fmt_shares(n: float) -> str:
    """股数：6,328 股；约 3.4 万股；4 万股；约 1.25 亿股（四舍五入过的加“约”）。"""
    if n < 1e4:
        return f"{n:,.0f} 股"
    unit, digits, label = (1e4, 1, "万股") if n < 1e8 else (1e8, 2, "亿股")
    shown = round(n / unit, digits)
    return ("" if abs(shown * unit - n) < 0.5 else "约 ") + f"{_trim(shown, digits)} {label}"


def _whole(v: float | None) -> float | int | None:
    """整数值的 float 转成 int（入库/展示更干净）。"""
    return int(v) if v is not None and float(v).is_integer() else v


def fmt_usd(v: float) -> str:
    """美元金额：$8,500；$17.6 万；$5,144 万；$1.25 亿。"""
    if v < 1e4:
        return f"${v:,.0f}"
    if v < 1e8:
        w = v / 1e4
        return f"${_trim(w, 0 if w >= 100 else 1)} 万"
    return f"${_trim(v / 1e8, 2)} 亿"


def form144_event(symbol: str, name: str, f: Form144, filing: Filing, cik: int) -> Event:
    """Form144 → Event(kind="insider_sale")。"""
    day = f.approx_sale_date or f.notice_date or filing.filing_date
    if day is None:
        raise ValueError(f"{filing.accession} 没有可用的日期")
    acc = filing.accession.replace("-", "")
    url = ARCHIVE_URL.format(cik=cik, acc=acc, doc=filing.doc or "")
    xml_url = ARCHIVE_URL.format(cik=cik, acc=acc, doc=raw_xml_name(filing.doc))
    rel = relationship_cn(f.relationships)
    person = display_person(f.person) or "内部人"
    amount = fmt_shares(f.shares) if f.shares is not None else "数量不详"
    value = f"（约 {fmt_usd(f.market_value)}）" if f.market_value is not None else ""
    title = (f"{name} 内部人拟减持{'（修订）' if f.amended else ''}："
             f"{person}{f'（{rel}）' if rel else ''}{amount}{value}")
    pct = f.pct_outstanding
    details = {
        "person": f.person, "relationship": rel, "relationships": list(f.relationships),
        "shares": _whole(f.shares), "market_value_usd": _whole(f.market_value),
        "shares_outstanding": _whole(f.shares_outstanding),
        "pct_outstanding": round(pct, 4) if pct is not None else None,
        "approx_sale_date": f.approx_sale_date.isoformat() if f.approx_sale_date else None,
        "broker": "；".join(f.brokers), "security_class": f.security_class, "exchange": f.exchange,
        "plan_adoption_dates": [d.isoformat() for d in f.plan_adoption_dates], "remarks": f.remarks,
        "notice_date": f.notice_date.isoformat() if f.notice_date else None,
        "filed": filing.filing_date.isoformat() if filing.filing_date else None,
        "form": f.form, "accession": filing.accession, "amends": f.previous_accession, "url": url, "xml_url": xml_url,
    }
    key_accession = f.previous_accession if f.amended and f.previous_accession else filing.accession
    return Event(kind="insider_sale", symbol=symbol, day=day, title=title,
                 key=f"insider_sale:{symbol}:{key_accession}", confirmed=True, source="sec_144", url=url,
                 details={k: v for k, v in details.items() if v not in (None, "", [])})


# ───────────────────────── 6-K 业绩预告 ─────────────────────────

_MONTHS = {m: i for i, names in enumerate(
    [("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",), ("june", "jun"),
     ("july", "jul"), ("august", "aug"), ("september", "sep", "sept"), ("october", "oct"), ("november", "nov"),
     ("december", "dec")], start=1) for m in names}
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE_PATTERNS = [
    # October 28, 2026 / Oct. 28 (Wed), 2026 / Wednesday, October 28, 2026
    (re.compile(rf"\b(?P<mon>{_MONTH_RE})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s*(?:\(\w{{2,9}}\.?\)\s*)?,?\s+"
                rf"(?P<y>20\d{{2}})\b", re.I), "mdy"),
    # 28 October 2026
    (re.compile(rf"\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<mon>{_MONTH_RE})\.?,?\s+(?P<y>20\d{{2}})\b", re.I), "dmy"),
    # 2026-10-28 / 2026.10.28 / 2026/10/28
    (re.compile(r"\b(?P<y>20\d{2})\s?[-./]\s?(?P<m>\d{1,2})\s?[-./]\s?(?P<d>\d{1,2})\b"), "ymd"),
]
_TIME_RE = re.compile(
    r"\b(?P<h>\d{1,2})(?::(?P<m>[0-5]\d))?\s*(?P<ap>[ap])\.?\s?m\b\.?"  # 9:00 a.m. / 4 PM
    r"|\b(?P<h24>[01]?\d|2[0-3]):(?P<m24>[0-5]\d)(?!\s*[ap]\.?\s?m\b)",  # 09:00 / 16:30
    re.I,
)
# 时区写法 → IANA 时区。顺序有意义：“Central European” 要先于 “Central”
_TZ_TABLE: list[tuple[str, str]] = [
    (r"seoul|korea(?:n)?(?:\s+standard)?|kst", "Asia/Seoul"),
    (r"tokyo|japan(?:ese)?(?:\s+standard)?|jst", "Asia/Tokyo"),
    (r"hong\s+kong|hkt", "Asia/Hong_Kong"),
    (r"taipei|taiwan", "Asia/Taipei"),
    (r"beijing|china\s+standard", "Asia/Shanghai"),
    (r"singapore|sgt", "Asia/Singapore"),
    (r"central\s+european|cet|cest", "Europe/Berlin"),
    (r"london|bst", "Europe/London"),
    (r"eastern|new\s+york|e[sd]?t", "America/New_York"),
    (r"central|c[sd]?t", "America/Chicago"),
    (r"pacific|p[sd]?t", "America/Los_Angeles"),
    (r"gmt|utc", "UTC"),
]
_TZ_RE = re.compile(r"\b(" + "|".join(p for p, _ in _TZ_TABLE) + r")\b(?:\s+(?:standard\s+|daylight\s+)?time)?", re.I)
_TOPIC_RE = re.compile(
    r"earnings|(?:quarter|half|annual|year|fiscal|financial|business|operating|interim)[^.;]{0,30}?\bresults"
    r"|results\s+(?:of\s+operations|for\s+the)\b", re.I)
_ACTION_RE = re.compile(
    r"conference\s+call|earnings\s+call|webcast|(?:will|plans?\s+to|intends?\s+to|expects?\s+to)\s+"
    r"(?:hold|host|announce|release|report|publish|disclose)|(?:will|to)\s+be\s+(?:held|announced|released|reported|"
    r"published|disclosed)|scheduled|date\s*(?:and|/|&)\s*time", re.I)
_CALL_RE = re.compile(r"conference\s+call|earnings\s+call|webcast", re.I)
_QWORD = {"first": 1, "second": 2, "third": 3, "fourth": 4, "1st": 1, "2nd": 2, "3rd": 3, "4th": 4}
_PERIOD_PATTERNS: list[tuple[re.Pattern[str], Callable[[re.Match[str]], str]]] = [
    (re.compile(r"\b(20\d{2})\s*([1-4])Q\b", re.I), lambda m: f"{m[1]}Q{m[2]}"),  # 2026 3Q（SK海力士）
    (re.compile(r"\b(20\d{2})\s*Q([1-4])\b", re.I), lambda m: f"{m[1]}Q{m[2]}"),  # 2026 Q3
    (re.compile(r"\b([1-4])Q\s*(?:of\s+)?(?:FY\s*)?'?(20\d{2}|\d{2})\b", re.I),  # 3Q 2026 / 3Q26
     lambda m: f"{_year(m[2])}Q{m[1]}"),
    # Q3 2026 / Q3 FY26
    (re.compile(r"\bQ([1-4])\s*(?:of\s+)?(?:FY\s*|fiscal\s+(?:year\s+)?)?'?(20\d{2}|\d{2})\b", re.I),
     lambda m: f"{_year(m[2])}Q{m[1]}"),
    (re.compile(r"\b(first|second|third|fourth|1st|2nd|3rd|4th)[\s-]+quarter\s+(?:of\s+)?(?:(?:the\s+)?(?:fiscal|FY)\s*"
                r"(?:year\s+)?)?(20\d{2})\b", re.I), lambda m: f"{m[2]}Q{_QWORD[m[1].lower()]}"),
    (re.compile(r"\b(first|second)\s+half\s+(?:of\s+)?(?:fiscal\s+(?:year\s+)?)?(20\d{2})\b", re.I),
     lambda m: f"{m[2]}H{1 if m[1].lower() == 'first' else 2}"),
    (re.compile(r"\b(?:fiscal\s+(?:year\s+)?|FY\s*)(20\d{2})\b", re.I), lambda m: f"FY{m[1]}"),
]
_QUARTER_CN = {"1": "一", "2": "二", "3": "三", "4": "四"}


def _year(s: str) -> str:
    return s if len(s) == 4 else f"20{s}"


def html_to_text(content: bytes | str) -> str:
    """HTML/SGML 原文 → 单行纯文本（去标签、反转义、空白合并）。"""
    if isinstance(content, bytes):
        try:
            content = content.decode("utf-8")
        except UnicodeDecodeError:
            content = content.decode("latin-1")
    t = re.sub(r"(?is)<(script|style)\b.*?</\1\s*>", " ", content[:MAX_HTML_CHARS])
    t = re.sub(r"(?s)<!--.*?-->", " ", t)
    t = re.sub(r"<[^>]{0,2000}>", " ", t)
    t = html.unescape(t).replace("\xa0", " ").replace("\u200b", "")
    return re.sub(r"\s+", " ", t).strip()


def body_text(text: str) -> str:
    """去掉 6-K 封面（最后一个 “Form 40-F” 勾选框之前）与签名栏（“SIGNATURE” 之后），只留正文开头。"""
    cover = [m.end() for m in re.finditer(r"Form 40-F", text[:5000])]
    start = cover[-1] if cover else 0
    start += len(re.match(r"\s*[☐☒✓✔xX□■]?", text[start:]).group(0))  # type: ignore[union-attr]
    sig = re.search(r"\bSIGNATURES?\b", text[start:])
    end = start + sig.start() if sig else len(text)
    return text[start:end][:BODY_CHARS].strip()


def parse_period(text: str) -> str | None:
    """正文里第一次出现的报告期 → "2026Q3" / "2026H1" / "FY2026"。"""
    found: list[tuple[int, str]] = []
    for pat, fmt in _PERIOD_PATTERNS:
        m = pat.search(text)
        if m:
            found.append((m.start(), fmt(m)))
    return min(found)[1] if found else None


def period_cn(period: str | None) -> str:
    """ "2026Q3" → "2026 年三季度"；"2026H1" → "2026 年上半年"；"FY2026" → "2026 财年"。"""
    if not period:
        return ""
    if m := re.fullmatch(r"(\d{4})Q([1-4])", period):
        return f"{m[1]} 年{_QUARTER_CN[m[2]]}季度"
    if m := re.fullmatch(r"(\d{4})H([12])", period):
        return f"{m[1]} 年{'上' if m[2] == '1' else '下'}半年"
    if m := re.fullmatch(r"FY(\d{4})", period):
        return f"{m[1]} 财年"
    return period


def _zone(token: str) -> ZoneInfo | None:
    token = re.sub(r"\s+", " ", token.strip())
    for pat, zone in _TZ_TABLE:
        if re.fullmatch(pat, token, re.I):
            return ZoneInfo(zone)
    return None


def _find_zone(window: str) -> ZoneInfo | None:
    m = _TZ_RE.search(window)
    return _zone(m.group(1)) if m else None


def _match_date(m: re.Match[str], kind: str) -> date | None:
    try:
        month = int(m["m"]) if kind == "ymd" else _MONTHS[m["mon"].lower()]
        return date(int(m["y"]), month, int(m["d"]))
    except (ValueError, KeyError):
        return None


def _match_time(m: re.Match[str]) -> tuple[int, int] | None:
    if m["h"] is not None:
        h, mi = int(m["h"]), int(m["m"] or 0)
        if not 1 <= h <= 12:
            return None
        return (h % 12 + (12 if m["ap"].lower() == "p" else 0), mi)
    return int(m["h24"]), int(m["m24"])


@dataclass(frozen=True)
class EarningsAnnouncement:
    """6-K 里预告的业绩发布/电话会。local 为当地时间（带时区）；没有时刻或时区时为 None，只有 local_day。"""

    local_day: date
    local: datetime | None
    local_time: str  # 展示用，如 "2026-10-28 09:00 KST"；只有日期时为 "2026-10-28"
    period: str | None
    conference_call: bool

    @property
    def at(self) -> datetime | None:
        return self.local.astimezone(UTC) if self.local else None


def find_datetimes(text: str) -> list[tuple[date, tuple[int, int] | None, ZoneInfo | None]]:
    """按出现顺序列出文本里的日期，以及紧挨着它的时刻（前后都找）和时区。"""
    out: list[tuple[date, tuple[int, int] | None, ZoneInfo | None]] = []
    matches = sorted(((m, kind) for pat, kind in _DATE_PATTERNS for m in pat.finditer(text)),
                     key=lambda x: x[0].start())
    for m, kind in matches:
        day = _match_date(m, kind)
        if day is None:
            continue
        hm, zone = None, None
        # 时刻的位置，按优先级：紧跟日期（“October 28, 2026 9:00 a.m.”）→ 紧挨在日期前（“9:00 a.m. KST on October 28,
        # 2026”）→ 同一句里稍后（“… on October 28, 2026 and host a call at 4:30 p.m.”）。中间不能隔着数字或句号
        after = text[m.end():m.end() + 80]
        t_after = _TIME_RE.search(after)
        gap_after = after[:t_after.start()] if t_after else ""
        ok_after = t_after is not None and not re.search(r"\d|[.;:](?:\s|$)", gap_after) and len(gap_after) <= 60
        before = text[max(0, m.start() - 40):m.start()]
        t_before = None
        for t in _TIME_RE.finditer(before):
            t_before = t
        gap_before = before[t_before.end():] if t_before else ""
        ok_before = t_before is not None and len(gap_before) <= 30 and not re.search(r"\d|[.;](?:\s|$)", gap_before)
        if t_after is not None and ok_after and (len(gap_after) <= 15 or not ok_before):
            hm = _match_time(t_after)
            zone = _find_zone(after[t_after.end():t_after.end() + 40]) or _find_zone(gap_after)
        elif t_before is not None and ok_before:
            hm = _match_time(t_before)
            zone = _find_zone(gap_before)
        if zone is None:
            zone = _find_zone(text[m.end():m.end() + 40])
        out.append((day, hm, zone))
    return out


def parse_earnings_announcement(text: str, filed: date | None = None) -> EarningsAnnouncement | None:
    """6-K 正文（html_to_text 之后的纯文本）→ 业绩发布/电话会预告；不是这类公告时返回 None。

    filed：提交日期。预告的日期必须不早于提交日（过去的电话会不算预告），也不能晚太多。
    """
    body = body_text(text)
    if not (_TOPIC_RE.search(body) and _ACTION_RE.search(body)):
        return None

    def plausible(d: date) -> bool:
        return filed is None or filed <= d <= filed + timedelta(days=MAX_DAYS_AHEAD)

    candidates = [c for c in find_datetimes(body) if plausible(c[0])]
    if not candidates:
        return None
    # 优先“日期 + 时刻”，否则取第一个合理的日期
    day, hm, zone = next((c for c in candidates if c[1] is not None), candidates[0])
    if zone is None and hm is not None:
        zone = _find_zone(body)  # 时区写在别处（如 “All times are Korea Standard Time”）
    period = parse_period(body)
    call = bool(_CALL_RE.search(body))
    if hm is not None and zone is not None:
        local = datetime(day.year, day.month, day.day, hm[0], hm[1], tzinfo=zone)
        return EarningsAnnouncement(day, local, f"{local:%Y-%m-%d %H:%M} {local.tzname()}", period, call)
    shown = day.isoformat() + (f" {hm[0]:02d}:{hm[1]:02d}（时区不明）" if hm else "")
    return EarningsAnnouncement(day, None, shown, period, call)


def earnings_event(symbol: str, name: str, a: EarningsAnnouncement, filing: Filing, cik: int) -> Event:
    at = a.at
    day = at.astimezone(ET).date() if at else a.local_day
    url = ARCHIVE_URL.format(cik=cik, acc=filing.accession.replace("-", ""), doc=filing.doc or "")
    p = period_cn(a.period)
    details = {"period": a.period, "local_time": a.local_time, "conference_call": a.conference_call,
               "accession": filing.accession, "filed": filing.filing_date.isoformat() if filing.filing_date else None,
               "url": url}
    return Event(kind="earnings", symbol=symbol, day=day, title=f"{name} {p}业绩发布" if p else f"{name} 业绩发布",
                 key=earnings_key(symbol, day), at=at, confirmed=True, source="sec_6k", url=url,
                 details={k: v for k, v in details.items() if v is not None})


# ───────────────────────── 数据源 ─────────────────────────

class _SecEventSource(EventSource):
    """公共流程：代码 → CIK → submissions JSON → 按 filingDate 取最近 lookback_days 天的指定表格 → 子类逐份解析。"""

    forms: set[str] = set()

    def __init__(self, sec: SecEdgarFilings, names: dict[str, str], lookback_days: int, *,
                 today: Callable[[], date] | None = None, close_sec: bool = False):
        self.sec = sec
        self.names = names
        self.lookback_days = lookback_days
        self._today = today or _today_et
        self._close_sec = close_sec

    async def aclose(self) -> None:
        """共享的 SecEdgarFilings 默认由创建它的一方关闭；close_sec=True 时由这里关闭。"""
        if self._close_sec:
            await self.sec.aclose()

    async def collect(self, symbols: list[str], start: date, end: date) -> list[Event]:
        cik_map = await self.sec._cik_map()  # 失败即整个来源不可用，SourceError 直接上抛
        cutoff = self._today() - timedelta(days=self.lookback_days)
        events: dict[str, Event] = {}
        attempted = failed = 0
        last_error: SourceError | None = None
        for sym in symbols:
            if not is_us_symbol(sym):
                continue
            cik = cik_map.get(sym.upper())
            if cik is None:
                log.info("%s：找不到 %s 的 CIK，跳过", self.name, sym)
                continue
            attempted += 1
            try:
                data = await self.sec._get_json(SUBMISSIONS_URL.format(cik=cik))
            except SourceError as exc:
                failed += 1
                last_error = exc
                log.warning("%s：获取 %s 的公告列表失败：%s", self.name, sym, exc)
                continue
            filings = [f for f in list_filings(data, self.forms) if f.filing_date is None or f.filing_date >= cutoff]
            if len(filings) > MAX_FILINGS_PER_SYMBOL:
                log.warning("%s：%s 最近 %d 天有 %d 份公告，只看最新的 %d 份",
                            self.name, sym, self.lookback_days, len(filings), MAX_FILINGS_PER_SYMBOL)
                filings = filings[:MAX_FILINGS_PER_SYMBOL]
            # 旧的在前：同一 key（修订、重复预告）以最新提交的那份为准
            for ev in await self._symbol_events(sym, cik, list(reversed(filings))):
                if start <= ev.day <= end:
                    events[ev.key] = ev
        if attempted and failed == attempted and last_error is not None:
            raise SourceError(f"[{self.name}] 所有代码的公告列表都获取失败：{last_error}", status=last_error.status)
        return sorted(events.values(), key=lambda e: (e.day, e.symbol, e.key))

    async def _symbol_events(self, sym: str, cik: int, filings: list[Filing]) -> list[Event]:
        raise NotImplementedError

    def _name(self, sym: str) -> str:
        return self.names.get(sym) or self.names.get(sym.upper()) or sym


class Form144Events(_SecEventSource):
    """Form 144（内部人拟减持通知）→ Event(kind="insider_sale")。

    只是“拟出售”通知，不代表已经成交（以随后的 Form 4 为准）；confirmed=True 表示通知已正式提交。
    min_value_usd：低于这个市值的通知不返回（市值缺失的照常返回）。
    """

    name = "sec_144"
    forms = {"144"}  # list_filings 会自动带上 144/A

    def __init__(self, sec: SecEdgarFilings, names: dict[str, str], min_value_usd: float = 0.0, lookback_days: int = 30,
                 *, today: Callable[[], date] | None = None, close_sec: bool = False):
        super().__init__(sec, names, lookback_days, today=today, close_sec=close_sec)
        self.min_value_usd = min_value_usd
        self._parsed: dict[str, Form144 | None] = {}  # accession → 解析结果（None = 不是可用的 144）

    async def _symbol_events(self, sym: str, cik: int, filings: list[Filing]) -> list[Event]:
        out: list[Event] = []
        for filing in filings:
            if filing.accession not in self._parsed:
                try:
                    self._parsed[filing.accession] = await self._fetch(cik, filing)
                except SourceError as exc:  # 限流或网络问题：这个代码先停下，下次再试（不缓存）
                    log.warning("sec_144：读取 %s 的 %s 失败，跳过其余公告：%s", sym, filing.accession, exc)
                    break
            f = self._parsed[filing.accession]
            if f is None:
                continue
            if f.issuer_cik is not None and f.issuer_cik != cik:
                continue  # 本公司作为出售人提交的、别家公司的 144
            if self.min_value_usd and f.market_value is not None and f.market_value < self.min_value_usd:
                continue
            try:
                out.append(form144_event(sym, self._name(sym), f, filing, cik))
            except ValueError as exc:
                log.warning("sec_144：%s", exc)
        return out

    async def _fetch(self, cik: int, filing: Filing) -> Form144 | None:
        """下载并解析原始 XML。文件不存在或解析失败返回 None（可以缓存）；限流、网络问题抛 SourceError。"""
        acc = filing.accession.replace("-", "")
        guess = raw_xml_name(filing.doc)
        docs = [guess]
        while docs:
            doc = docs.pop(0)
            try:
                resp = await self.sec._get(ARCHIVE_URL.format(cik=cik, acc=acc, doc=doc))
            except SourceError as exc:
                if exc.status != 404:
                    raise
                if doc == guess:  # 猜的文件名不对：查目录清单
                    docs = [n for n in await self._xml_names(cik, acc) if n != guess][:2]
                continue
            try:
                return parse_form144(resp.content)
            except ValueError as exc:
                log.warning("sec_144：%s 解析失败：%s", filing.accession, exc)
                return None
        log.warning("sec_144：%s 找不到原始 XML", filing.accession)
        return None

    async def _xml_names(self, cik: int, acc: str) -> list[str]:
        """公告目录（index.json）里的 .xml 文件名；目录不存在时返回空列表。"""
        try:
            listing = await self.sec._get_json(INDEX_JSON_URL.format(cik=cik, acc=acc))
        except SourceError as exc:
            if exc.status == 404:
                return []
            raise
        items = (listing.get("directory") or {}).get("item") or []
        names = [str(it.get("name") or "") for it in items if isinstance(it, dict)]
        return [n for n in names if n.endswith(".xml")]


class Earnings6KEvents(_SecEventSource):
    """外国私人发行人的 6-K 业绩发布/电话会预告 → Event(kind="earnings", confirmed=True, at=精确时间)。"""

    name = "sec_6k"
    forms = {"6-K"}

    def __init__(self, sec: SecEdgarFilings, names: dict[str, str], lookback_days: int = 45,
                 *, today: Callable[[], date] | None = None, close_sec: bool = False):
        super().__init__(sec, names, lookback_days, today=today, close_sec=close_sec)
        self._parsed: dict[str, EarningsAnnouncement | None] = {}  # accession → 识别结果（None = 不是业绩预告）

    async def _symbol_events(self, sym: str, cik: int, filings: list[Filing]) -> list[Event]:
        out: list[Event] = []
        for filing in filings:
            if filing.accession not in self._parsed:
                if not filing.doc or not filing.doc.lower().endswith((".htm", ".html", ".txt")):
                    self._parsed[filing.accession] = None
                    continue
                url = ARCHIVE_URL.format(cik=cik, acc=filing.accession.replace("-", ""), doc=filing.doc)
                try:
                    resp = await self.sec._get(url)
                except SourceError as exc:
                    if exc.status == 404:
                        self._parsed[filing.accession] = None
                        continue
                    log.warning("sec_6k：读取 %s 的 %s 失败，跳过其余公告：%s", sym, filing.accession, exc)
                    break
                text = html_to_text(resp.content)
                self._parsed[filing.accession] = parse_earnings_announcement(text, filing.filing_date)
            a = self._parsed[filing.accession]
            if a is not None:
                out.append(earnings_event(sym, self._name(sym), a, filing, cik))
        return out
