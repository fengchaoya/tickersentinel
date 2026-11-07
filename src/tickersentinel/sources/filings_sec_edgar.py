"""SEC EDGAR 公告（免费、无需 key，但必须在 User-Agent 里写联系方式；限速 10 次/秒）。

1) 代码 → CIK：https://www.sec.gov/files/company_tickers.json
2) 最近公告：  https://data.sec.gov/submissions/CIK##########.json
   filings.recent 下是若干等长数组：form / accessionNumber / filingDate / acceptanceDateTime /
   primaryDocument / primaryDocDescription / items（8-K 的事项编号，如 "2.02,9.01"）
3) 原文链接：  https://www.sec.gov/Archives/edgar/data/<cik>/<accession去掉横线>/<primaryDocument>
4) 受理时间：  https://www.sec.gov/Archives/edgar/data/<cik>/<accession去掉横线>/<accession>-index-headers.html
   里的 <ACCEPTANCE-DATETIME>YYYYMMDDHHMMSS，美东墙上时间（与公告索引页的 “Accepted” 一致）

SKHY 是外国私人发行人，提交 6-K（而不是 8-K）；美光、闪迪的内部人交易是 Form 4。

【受理时间的时区 —— 2026-10-11 联网核实】
submissions JSON 的 acceptanceDateTime（形如 "2026-10-09T16:21:00.000Z"）**不能直接用**。拿 109 家公司的 JSON
对照各自的 <ACCEPTANCE-DATETIME>（两轮独立抽样，49 + 60 家）：
- 最近一份公告在 2026-10-02 及以前的公司（如 SKHY、ASML、STX，共 58 家）：JSON = 真正的 UTC；
- 最近一份公告在 2026-10-05 及以后的公司（如 MU、SNDK、WDC，共 51 家；有新公告时整份文件会重新生成）：整份文件的
  所有时间（包括历史记录）都被多加了一次美东偏移，即 JSON = 美东时间 + 2×偏移（夏令时 +8h、冬令时 +10h）。
  例：MU 10-K 实际受理于 2026-10-09 08:21 ET，JSON 写成 16:21Z（真 UTC 应为 12:21Z）。
- 原先设想的“带 Z 后缀但实际是美东时间”在任何一家公司上都没有观察到。
看起来是 SEC 一侧的回归，随时可能修复，所以这里不写死任何偏移：
- 先用 JSON 时间 ±12 小时粗筛（它的误差不超过一个美东偏移），再逐份读取 <ACCEPTANCE-DATETIME> 作为受理时间
  （按 accession 缓存，受理时间不会变）；
- 读取失败时才退回 JSON 时间，偏移依次取自：本次从同一份文件对照出的值 → 不联网的判据（EDGAR 只在工作日
  06:00–22:00 ET 受理，选让整份文件越界最少的偏移）→ 这家公司上次对照出的值 → 当作真 UTC 并打日志。
这一点影响归因时“公告在异动之前还是之后”的判断。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import httpx

from tickersentinel.config import is_us_symbol
from tickersentinel.models import NewsItem
from tickersentinel.sources.base import NewsQuery, NewsSource, SourceError
from tickersentinel.sources.http import make_client, request
from tickersentinel.timeutil import ET, UTC

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
INDEX_HEADERS_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{accession}-index-headers.html"
COMPANY_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"

MIN_REQUEST_INTERVAL = 0.15  # 秒。SEC 限速 10 次/秒，超限会封 IP 约 10 分钟
JSON_TIME_SLACK = timedelta(hours=12)  # 粗筛余量：JSON 时间相对真实受理时间的误差实测不超过一个美东偏移（≤5h）
# JSON 里没有可用的时间时才按 filingDate 粗筛。filingDate 可以比受理日晚（17:30 ET 后受理记到下一个工作日，
# 遇到圣诞连休实测晚 6 天），也可以早（周五 22:00 后提交、周一 06:00 受理，日期回填到周五，实测早 3 天）。
FILING_DATE_SLACK_BEFORE = timedelta(days=5)
FILING_DATE_SLACK_AFTER = timedelta(days=10)
MAX_FILINGS_PER_SYMBOL = 40  # 单次检索、单个标的最多核对多少份公告的受理时间

_ACCEPTANCE_RE = re.compile(r"<ACCEPTANCE-DATETIME>\s*(\d{14})")

FORM_NAMES = {
    "8-K": "重大事项公告", "6-K": "外国发行人临时报告", "4": "内部人交易", "10-Q": "季报", "10-K": "年报",
    "20-F": "外国发行人年报", "SC 13D": "大股东持股(13D)", "SC 13G": "大股东持股(13G)", "424B5": "增发/发债募集说明书",
    "144": "内部人拟减持通知",
}
# 2024 年底起 EDGAR 把 SC 13D / SC 13G 改名为 SCHEDULE 13D / SCHEDULE 13G；配置里两种写法都认
_FORM_ALIASES = {"SCHEDULE 13D": "SC 13D", "SCHEDULE 13G": "SC 13G"}

# 8-K 事项编号 → 含义（9.01“财务报表与附件”几乎每份都有，不展示）
ITEMS_8K = {
    "1.01": "签订重大协议", "1.02": "终止重大协议", "1.03": "破产或接管", "1.05": "重大网络安全事件",
    "2.01": "完成资产收购或处置", "2.02": "业绩发布", "2.03": "新增重大债务", "2.05": "重组/裁员成本",
    "2.06": "重大资产减值", "3.01": "退市或不符合上市标准通知", "3.02": "未注册的股权发行",
    "3.03": "证券持有人权利变更", "4.01": "更换审计师", "4.02": "此前财报不可信赖", "5.02": "董事/高管变动",
    "5.03": "章程修订", "5.07": "股东大会表决结果", "5.08": "股东提名董事", "7.01": "Reg FD 披露", "8.01": "其他事项",
}
# primaryDocDescription 里没有信息量的套话（另外，“FORM xxx”和表格名本身也视为套话）
_GENERIC_DESC = {
    "PRIMARY DOCUMENT", "PRIMARY DOC", "OWNERSHIP DOCUMENT", "FORM 4 SUBMISSION", "CURRENT REPORT", "INDEX",
    "STATEMENT OF CHANGES IN BENEFICIAL OWNERSHIP OF SECURITIES", "REPORT OF FOREIGN PRIVATE ISSUER",
}


def base_form(form: str) -> str:
    """表格类型归一：去掉修订后缀 /A，SCHEDULE 13D/G → SC 13D/G。"""
    f = form.strip().upper()
    if f.endswith("/A"):
        f = f[:-2]
    return _FORM_ALIASES.get(f, f)


def _et_offset(ts: datetime) -> timedelta:
    """ts 时刻美东时间相对 UTC 的偏移量（夏令时 4h，冬令时 5h）。"""
    return -ts.astimezone(ET).utcoffset()  # type: ignore[operator]


def parse_header_acceptance(text: str) -> datetime | None:
    """从 index-headers 页面（或 SGML 头）里取 <ACCEPTANCE-DATETIME>，美东墙上时间 → UTC。"""
    m = _ACCEPTANCE_RE.search(text)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=ET).astimezone(UTC)
    except ValueError:
        return None


def parse_json_acceptance(s: str, extra_shifts: int = 0) -> datetime:
    """submissions JSON 的 acceptanceDateTime → UTC。

    extra_shifts = JSON 相对真 UTC 多加了几次美东偏移：0 表示就是 UTC；1 表示 2026-10 观察到的“多转换一次”；
    -1 表示“美东时间直接贴了 Z”。只在读不到 <ACCEPTANCE-DATETIME> 时作为退路使用。
    """
    ts = datetime.fromisoformat(s.replace("Z", "").split(".")[0]).replace(tzinfo=UTC)
    return ts - extra_shifts * _et_offset(ts)


def infer_extra_shifts(json_value: str, accepted: datetime) -> int:
    """对照权威受理时间，算出这份 JSON 多加了几次美东偏移（见 parse_json_acceptance）。"""
    delta = parse_json_acceptance(json_value) - accepted
    return round(delta / _et_offset(accepted))


def guess_extra_shifts(json_values: list[str], min_rows: int = 20) -> int | None:
    """不联网的判据：EDGAR 只在周一至周五 06:00–22:00 ET 受理。在已观察到的两种状态（0 = 真 UTC，1 = 多转换一次）
    之间，选让整份文件“越界”行数更少的那个。

    60 家公司 7.4 万行的实测：正确偏移下越界不到 0.1%（主要是固定在 00:15 的 EFFECT 通知），错误偏移下至少 1.3%。
    行数太少或分不出高下时返回 None（例如文件里既没有清晨也没有晚间受理的公告）。
    """
    scores: dict[int, int] = {}
    for shifts in (0, 1):
        bad = n = 0
        for v in json_values:
            try:
                et = parse_json_acceptance(v, shifts).astimezone(ET)
            except (ValueError, AttributeError):
                continue
            n += 1
            minutes = et.hour * 60 + et.minute
            if et.weekday() >= 5 or not (6 * 60 <= minutes < 22 * 60 + 10):
                bad += 1
        if n < min_rows:
            return None
        scores[shifts] = bad
    best = min(scores, key=scores.__getitem__)
    return best if sum(v == scores[best] for v in scores.values()) == 1 else None


@dataclass(frozen=True)
class Filing:
    form: str
    accession: str  # 带横线，如 0000723125-26-000023
    filing_date: date | None
    json_accepted: str
    doc: str
    desc: str
    items: str

    @property
    def amended(self) -> bool:
        return self.form.strip().upper().endswith("/A")

    def title(self, symbol: str) -> str:
        base = base_form(self.form)
        label = FORM_NAMES.get(base, base) + ("·修订" if self.amended else "")
        detail = ""
        if base == "8-K":
            names = [ITEMS_8K.get(code.strip(), f"Item {code.strip()}") for code in self.items.split(",")
                     if code.strip() and code.strip() != "9.01"]
            detail = "、".join(dict.fromkeys(names))
        if not detail:
            d = self.desc.strip()
            generic = not d or d.upper() in _GENERIC_DESC or base_form(d.upper().removeprefix("FORM ")) == base
            detail = "" if generic else d
        return f"{symbol} 提交 {self.form}（{label}）" + (f"：{detail}" if detail else "")

    def url(self, cik: int) -> str:
        if not self.doc:
            return COMPANY_URL.format(cik=cik)
        return ARCHIVE_URL.format(cik=cik, acc=self.accession.replace("-", ""), doc=self.doc)


def list_filings(data: dict, forms: set[str]) -> list[Filing]:
    """解析 submissions JSON 的 filings.recent（新的在前），只保留关心的表格类型（含各自的 /A 修订）。"""
    recent = (data.get("filings") or {}).get("recent") or {}
    n = len(recent.get("form") or [])
    wanted = {base_form(f) for f in forms}

    def col(name: str, i: int) -> str:
        values = recent.get(name) or []
        return (values[i] if i < len(values) else "") or ""

    out: list[Filing] = []
    for i in range(n):
        form = col("form", i)
        accession = col("accessionNumber", i)
        if base_form(form) not in wanted or not accession:
            continue
        try:
            filed = date.fromisoformat(col("filingDate", i))
        except ValueError:
            filed = None
        out.append(Filing(form=form, accession=accession, filing_date=filed, json_accepted=col("acceptanceDateTime", i),
                          doc=col("primaryDocument", i), desc=col("primaryDocDescription", i), items=col("items", i)))
    return out


def coarse_filter(filings: list[Filing], start: datetime, end: datetime) -> list[Filing]:
    """粗筛出受理时间可能落在 [start, end] 的公告：JSON 时间（按 UTC 读）±12 小时；JSON 时间不可用时按 filingDate。"""
    lo_day = start.astimezone(ET).date() - FILING_DATE_SLACK_BEFORE
    hi_day = end.astimezone(ET).date() + FILING_DATE_SLACK_AFTER
    out = []
    for f in filings:
        try:
            ts = parse_json_acceptance(f.json_accepted)
        except ValueError:
            keep = f.filing_date is None or lo_day <= f.filing_date <= hi_day
        else:
            keep = start - JSON_TIME_SLACK <= ts <= end + JSON_TIME_SLACK
        if keep:
            out.append(f)
    return out


class SecEdgarFilings(NewsSource):
    name = "sec_edgar"

    def __init__(self, user_agent: str, forms: list[str], timeout: float = 12.0, client: httpx.AsyncClient | None = None,
                 min_interval: float = MIN_REQUEST_INTERVAL):
        if re.search(r"example\.(com|org|net)|your-email", user_agent):
            log.warning("SEC EDGAR 要求 User-Agent 含联系方式：请在 .env 里设置 SEC_EDGAR_USER_AGENT=tickersentinel 你的邮箱")
        self.forms = set(forms)
        self._client = client or make_client(timeout, headers={"User-Agent": user_agent})
        self._min_interval = min_interval
        self._throttle = asyncio.Lock()
        self._last_request = 0.0
        self._cik: dict[str, int] | None = None
        self._accepted: dict[str, datetime] = {}  # accession → 权威受理时间（UTC）
        self._json_shifts: dict[int, int] = {}  # cik → 最近一次对照出的 JSON 偏移次数

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, **kw) -> httpx.Response:
        """所有发往 SEC 的请求都走这里：同一个实例内两次请求至少间隔 min_interval。"""
        async with self._throttle:
            wait = self._last_request + self._min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()
        return await request(self._client, url, source="sec_edgar", **kw)

    async def _get_json(self, url: str) -> dict:
        resp = await self._get(url)
        try:
            data = resp.json()
        except ValueError as exc:
            raise SourceError(f"[sec_edgar] 返回的不是 JSON：{resp.text[:200]!r}") from exc
        if not isinstance(data, dict):
            raise SourceError(f"[sec_edgar] JSON 结构不符合预期：{str(data)[:200]!r}")
        return data

    async def _cik_map(self) -> dict[str, int]:
        if self._cik is None:
            data = await self._get_json(TICKERS_URL)
            self._cik = {row["ticker"].upper(): int(row["cik_str"]) for row in data.values()}
        return self._cik

    async def _accepted_at(self, cik: int, filing: Filing) -> datetime | None:
        """联网读取权威受理时间。这份公告没有该页面/字段时返回 None；其他失败（限流、网络）抛 SourceError。"""
        url = INDEX_HEADERS_URL.format(cik=cik, acc=filing.accession.replace("-", ""), accession=filing.accession)
        try:
            resp = await self._get(url, retries=0)  # 不重试：失败后调用方会停止逐份读取，改走 JSON 退路
        except SourceError as exc:
            if exc.status != 404:
                raise
            log.info("SEC EDGAR：%s 没有 index-headers 页面（HTTP 404）", filing.accession)
            return None
        ts = parse_header_acceptance(resp.text)
        if ts is None:
            log.warning("SEC EDGAR：%s 的 index-headers 里没有 <ACCEPTANCE-DATETIME>", filing.accession)
            return None
        self._accepted[filing.accession] = ts
        return ts

    async def fetch(self, q: NewsQuery) -> list[NewsItem]:
        cik_map = await self._cik_map()
        items: list[NewsItem] = []
        headers_ok = True  # www.sec.gov 的 index-headers 一旦失败（非 404），本次检索不再逐份去读
        for sym in q.symbols:
            if not is_us_symbol(sym):
                continue
            cik = cik_map.get(sym.upper())
            if cik is None:
                log.info("SEC EDGAR 找不到 %s 的 CIK，跳过", sym)
                continue
            try:
                data = await self._get_json(SUBMISSIONS_URL.format(cik=cik))
            except SourceError as exc:
                log.warning("SEC EDGAR 获取失败（%s）：%s", sym, exc)
                continue
            candidates = coarse_filter(list_filings(data, self.forms), q.start, q.end)
            if len(candidates) > MAX_FILINGS_PER_SYMBOL:
                log.warning("SEC EDGAR：%s 在检索区间内有 %d 份候选公告，只核对最新的 %d 份",
                            sym, len(candidates), MAX_FILINGS_PER_SYMBOL)
                candidates = candidates[:MAX_FILINGS_PER_SYMBOL]

            verified_shifts: int | None = None  # 本次从这份文件对照出的偏移
            pending: list[Filing] = []
            for f in candidates:
                ts = self._accepted.get(f.accession)
                if ts is None and headers_ok:
                    try:
                        ts = await self._accepted_at(cik, f)
                    except SourceError as exc:
                        headers_ok = False
                        log.warning("SEC EDGAR 读取受理时间失败，本次检索其余公告改用 JSON 时间：%s", exc)
                if ts is None:
                    pending.append(f)
                    continue
                try:
                    verified_shifts = self._json_shifts[cik] = infer_extra_shifts(f.json_accepted, ts)
                except ValueError:
                    pass
                if q.start <= ts <= q.end:
                    items.append(self._item(sym, cik, f, ts))

            if pending:
                shifts = self._fallback_shifts(cik, data, verified_shifts)
                for f in pending:
                    try:
                        ts = parse_json_acceptance(f.json_accepted, shifts)
                    except ValueError:
                        continue
                    if q.start <= ts <= q.end:
                        items.append(self._item(sym, cik, f, ts))
        return sorted(items, key=lambda i: i.published_at, reverse=True)

    def _fallback_shifts(self, cik: int, data: dict, verified: int | None) -> int:
        """读不到权威受理时间时，JSON 时间该按几次偏移修正（优先级见文件开头）。"""
        if verified is not None:
            return verified
        recent = (data.get("filings") or {}).get("recent") or {}
        guessed = guess_extra_shifts(list(recent.get("acceptanceDateTime") or []))
        if guessed is not None:
            return guessed
        known = self._json_shifts.get(cik)
        if known is not None:
            return known
        log.warning("SEC EDGAR：CIK %s 的受理时间只能取自 JSON 且无法对照，按 UTC 处理，可能偏差 4～5 小时", cik)
        return 0

    @staticmethod
    def _item(sym: str, cik: int, f: Filing, ts: datetime) -> NewsItem:
        return NewsItem(title=f.title(sym), url=f.url(cik), published_at=ts, source="SEC EDGAR",
                        provider="sec_edgar", symbols=(sym,), kind="filing")
