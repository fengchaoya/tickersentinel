"""新闻去重：同一篇文章（规范化 URL 相同）或同一事件的多家转载（标题相似度高）只保留一条。

公告（kind="filing"）只按 URL 去重：标题是按模板生成的（“MU 提交 4（内部人交易）”），同一天的几份 Form 4、
不同公司的业绩 8-K 标题都几乎一样，但它们是不同的公告。
"""

from __future__ import annotations

from dataclasses import replace

from tickersentinel.models import NewsItem
from tickersentinel.textutil import canonical_url, normalize_title, title_similarity

DEFAULT_THRESHOLD = 0.72


def dedupe(items: list[NewsItem], threshold: float = DEFAULT_THRESHOLD) -> list[NewsItem]:
    """保留最早发布的一条（更可能是首发来源），并合并相关代码。结果按发布时间倒序。"""
    kept: list[NewsItem] = []
    url_index: dict[str, int] = {}
    titles: list[str] = []
    for item in sorted(items, key=lambda x: x.published_at):
        cu = canonical_url(item.url)
        idx = url_index.get(cu)
        if idx is None and item.kind != "filing":
            nt = normalize_title(item.title)
            idx = next((i for i, t in enumerate(titles) if title_similarity(nt, t) >= threshold), None)
        if idx is not None:
            merged = tuple(sorted(set(kept[idx].symbols) | set(item.symbols)))
            if merged != kept[idx].symbols:
                kept[idx] = replace(kept[idx], symbols=merged)
            url_index.setdefault(cu, idx)
            continue
        kept.append(item)
        url_index[cu] = len(kept) - 1
        titles.append("" if item.kind == "filing" else normalize_title(item.title))  # 新闻也不会并进公告
    return sorted(kept, key=lambda x: x.published_at, reverse=True)
