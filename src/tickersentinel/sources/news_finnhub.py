"""Finnhub：公司新闻 + 财报日历（免费 key，60 次/分钟；实时新闻 WebSocket 需付费，这里用 REST 轮询）。

GET https://finnhub.io/api/v1/company-news?symbol=MU&from=YYYY-MM-DD&to=YYYY-MM-DD&token=KEY
  → [{"category","datetime"(unix秒),"headline","id","image","related","source","summary","url"}]
GET https://finnhub.io/api/v1/calendar/earnings?from=...&to=...&symbol=MU&token=KEY
  → {"earningsCalendar": [{"date","hour"(bmo/amc/dmh),"symbol","epsEstimate","revenueEstimate",...}]}
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import httpx

from tickersentinel.config import is_us_symbol
from tickersentinel.models import Event, NewsItem, earnings_key
from tickersentinel.sources.base import EventSource, NewsQuery, NewsSource, SourceError
from tickersentinel.sources.http import get_json, make_client
from tickersentinel.textutil import truncate
from tickersentinel.timeutil import UTC

log = logging.getLogger(__name__)
BASE = "https://finnhub.io/api/v1"


class FinnhubSource(NewsSource, EventSource):
    name = "finnhub"

    def __init__(self, api_key: str, timeout: float = 12.0, client: httpx.AsyncClient | None = None,
                 names: dict[str, str] | None = None):
        self._key = api_key
        self.names = names or {}
        self._client = client or make_client(timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self, q: NewsQuery) -> list[NewsItem]:
        items: list[NewsItem] = []
        # from/to 只精确到日期：多取一天，再按时间戳精确过滤
        d0 = (q.start - timedelta(days=1)).date().isoformat()
        d1 = (q.end + timedelta(days=1)).date().isoformat()
        for sym in q.symbols:
            if not is_us_symbol(sym):
                continue
            try:
                rows = await get_json(
                    self._client, f"{BASE}/company-news",
                    params={"symbol": sym, "from": d0, "to": d1, "token": self._key}, source="finnhub",
                )
            except SourceError as exc:
                log.warning("Finnhub 新闻获取失败（%s）：%s", sym, exc)
                continue
            for row in rows or []:
                item = self._parse(row, sym)
                if item and q.start <= item.published_at <= q.end:
                    items.append(item)
        return items

    @staticmethod
    def _parse(row: dict, sym: str) -> NewsItem | None:
        try:
            ts = datetime.fromtimestamp(int(row["datetime"]), UTC)
            title, url = row["headline"].strip(), row["url"].strip()
        except (KeyError, TypeError, ValueError):
            return None
        if not title or not url:
            return None
        return NewsItem(
            title=title, url=url, published_at=ts, source=row.get("source") or "",
            provider="finnhub", symbols=(sym,), summary=truncate(row.get("summary") or "", 400),
        )

    async def collect(self, symbols: list[str], start: date, end: date) -> list[Event]:
        """财报日历 → Event(kind="earnings")。Finnhub 不区分“已确认 / 估计”，一律记为估计（confirmed=False）。"""
        out: list[Event] = []
        tried, failures = 0, []
        for sym in symbols:
            if not is_us_symbol(sym):
                continue
            tried += 1
            try:
                data = await get_json(
                    self._client, f"{BASE}/calendar/earnings",
                    params={"symbol": sym, "from": start.isoformat(), "to": end.isoformat(), "token": self._key},
                    source="finnhub",
                )
            except SourceError as exc:  # 单个代码失败不影响其他代码
                log.warning("Finnhub 财报日历获取失败（%s）：%s", sym, exc)
                failures.append(str(exc))
                continue
            for row in (data or {}).get("earningsCalendar") or []:
                try:
                    day = date.fromisoformat(row["date"])
                except (KeyError, TypeError, ValueError):
                    continue
                details = {k: row.get(k) for k in ("epsEstimate", "revenueEstimate", "quarter", "year") if row.get(k) is not None}
                out.append(Event(
                    kind="earnings", symbol=row.get("symbol") or sym, day=day,
                    title=f"{self.names.get(sym, sym)} 财报", key=earnings_key(sym, day),
                    time_hint={"bmo": "盘前", "amc": "盘后", "dmh": "盘中"}.get(row.get("hour") or "", ""),
                    confirmed=False, source="finnhub", details=details,
                ))
        if tried and len(failures) == tried:  # 整个来源不可用（key 错误、限流）：交给 EventHub 记为失败
            raise SourceError(f"[finnhub] 财报日历全部 {tried} 个代码都失败，例如 {failures[0]}")
        return sorted(out, key=lambda e: e.day)
