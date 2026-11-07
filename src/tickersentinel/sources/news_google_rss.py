"""Google News RSS：免费、无需 key，覆盖英文/韩文/中文媒体。

https://news.google.com/rss/search?q=<检索词 when:12h>&hl=en-US&gl=US&ceid=US:en
条目标题形如 “Headline - Source”，链接为 Google 跳转链接（同一文章跨源去重靠标题相似度）。
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import math
from datetime import datetime

import feedparser
import httpx

from tickersentinel.config import GoogleNewsEdition
from tickersentinel.models import NewsItem
from tickersentinel.sources.base import NewsQuery, NewsSource, SourceError
from tickersentinel.sources.http import get_bytes, make_client
from tickersentinel.textutil import strip_source_suffix
from tickersentinel.timeutil import UTC, utcnow

log = logging.getLogger(__name__)
URL = "https://news.google.com/rss/search"


def when_operator(start: datetime, now: datetime | None = None) -> str:
    hours = max(1, math.ceil(((now or utcnow()) - start).total_seconds() / 3600))
    return f"when:{hours}h" if hours <= 48 else f"when:{math.ceil(hours / 24)}d"


def parse_feed(content: bytes, *, symbol: str, lang: str) -> list[NewsItem]:
    feed = feedparser.parse(content)
    items = []
    for e in feed.entries:
        tm = e.get("published_parsed") or e.get("updated_parsed")
        link = e.get("link")
        if not tm or not link:
            continue
        source = (e.get("source") or {}).get("title", "") if isinstance(e.get("source"), dict) else ""
        title = strip_source_suffix(e.get("title", ""), source)
        if not title:
            continue
        items.append(NewsItem(
            title=title,
            url=link,
            published_at=datetime.fromtimestamp(calendar.timegm(tm), UTC),
            source=source,
            provider="google_news",
            symbols=(symbol,),
            summary="",  # RSS 的 description 是 HTML 片段，信息量低，不保留
            lang=lang,
        ))
    return items


class GoogleNewsRSS(NewsSource):
    name = "google_news"

    def __init__(self, editions: dict[str, GoogleNewsEdition], timeout: float = 12.0,
                 client: httpx.AsyncClient | None = None, concurrency: int = 4):
        self.editions = editions
        self._client = client or make_client(timeout)
        self._sem = asyncio.Semaphore(concurrency)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self, q: NewsQuery) -> list[NewsItem]:
        jobs = [
            (sym, lang, term)
            for sym, by_lang in q.queries.items()
            for lang, terms in by_lang.items()
            if lang in q.langs and lang in self.editions
            for term in terms
        ]
        results = await asyncio.gather(*(self._one(sym, lang, term, q) for sym, lang, term in jobs),
                                       return_exceptions=True)
        items: list[NewsItem] = []
        failures: list[str] = []
        for (_sym, lang, term), res in zip(jobs, results):
            if isinstance(res, BaseException):
                failures.append(f"{term}/{lang}: {res}")
                continue
            items.extend(i for i in res if q.start <= i.published_at <= q.end)
        if jobs and len(failures) == len(jobs):
            raise SourceError(f"[google_news] 全部 {len(jobs)} 个检索均失败，例如 {failures[0]}")
        if failures:
            log.warning("Google News：%d/%d 个检索失败，例如 %s", len(failures), len(jobs), failures[0])
        return items

    async def _one(self, sym: str, lang: str, term: str, q: NewsQuery) -> list[NewsItem]:
        ed = self.editions[lang]
        params = {"q": f"{term} {when_operator(q.start)}", "hl": ed.hl, "gl": ed.gl, "ceid": ed.ceid}
        async with self._sem:
            content = await get_bytes(self._client, URL, params=params, source="google_news")
        try:
            return parse_feed(content, symbol=sym, lang=lang)
        except Exception as exc:  # feedparser 极少抛错，保险起见
            raise SourceError(f"[google_news] 解析失败：{exc}") from exc
