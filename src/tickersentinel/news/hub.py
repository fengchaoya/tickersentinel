"""新闻聚合：并发抓取所有新闻/公告源 → 时间过滤 → 去重。单个源失败不影响其他源。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from tickersentinel.config import Universe
from tickersentinel.models import NewsItem
from tickersentinel.news.dedupe import dedupe
from tickersentinel.sources.base import NewsQuery, NewsSource

log = logging.getLogger(__name__)


class NewsHub:
    def __init__(self, sources: list[NewsSource], universe: Universe):
        self.sources = sources
        self.universe = universe
        self.last_errors: dict[str, str] = {}

    def build_query(self, symbols: list[str], start: datetime, end: datetime, *, include_sector: bool = True,
                    langs: tuple[str, ...] = ("en", "ko", "zh"), sector_of: list[str] | None = None) -> NewsQuery:
        """symbols 的公司新闻 + 板块新闻（这些代码所属主题的检索词，键为 SECTOR:<主题>；sector_of 指定按哪些代码的主题，
        默认就是 symbols——光模块股的异动只检索光模块的板块新闻，不检索存储芯片的）。"""
        queries: dict[str, dict[str, list[str]]] = {}
        for sym in symbols:
            try:
                queries[sym] = self.universe.get(sym).queries
            except KeyError:
                queries[sym] = {"en": [sym]}
        if include_sector:
            queries.update(self.universe.sector_news(sector_of or symbols))
        return NewsQuery(start=start, end=end, symbols=tuple(symbols), queries=queries, langs=langs)

    async def collect(self, symbols: list[str], start: datetime, end: datetime, *, include_sector: bool = True,
                      langs: tuple[str, ...] = ("en", "ko", "zh"), sector_of: list[str] | None = None) -> list[NewsItem]:
        q = self.build_query(symbols, start, end, include_sector=include_sector, langs=langs, sector_of=sector_of)
        results = await asyncio.gather(*(src.fetch(q) for src in self.sources), return_exceptions=True)
        items: list[NewsItem] = []
        self.last_errors = {}
        for src, res in zip(self.sources, results):
            if isinstance(res, BaseException):
                self.last_errors[src.name] = f"{type(res).__name__}: {res}"
                log.warning("新闻源 %s 失败：%s", src.name, res)
                continue
            items.extend(i for i in res if start <= i.published_at <= end)
        return dedupe(items)

    async def aclose(self) -> None:
        for src in self.sources:
            try:
                await src.aclose()
            except Exception:  # pragma: no cover
                pass
