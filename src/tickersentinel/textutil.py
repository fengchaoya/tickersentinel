"""文本工具：URL 规范化、标题规范化与相似度（用于新闻去重）。不依赖项目其他模块。"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_KEYS = {"guccounter", "guce_referrer", "guce_referrer_sig", "ncid", "cmpid", "fbclid", "gclid", "ocid", "src", "ref", "taid", "yptr", "mod", "oc"}
_TRACKING_PREFIXES = ("utm_", "mc_", "guce_")

# 中日韩字符（含韩文音节），这类文本没有空格分词，改用字符二元组
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")
# 标题结尾的 “ - Reuters” / “ | CNBC” 之类媒体名
_SOURCE_SUFFIX = re.compile(r"\s+[-–—|]\s+[^-–—|]{2,40}$")


def canonical_url(url: str) -> str:
    """去掉追踪参数、www、末尾斜杠和锚点，用于判断是否同一篇文章。"""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query)
        if k.lower() not in _TRACKING_KEYS and not k.lower().startswith(_TRACKING_PREFIXES)
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(((parts.scheme or "https").lower(), host, path, urlencode(sorted(query)), ""))


def strip_source_suffix(title: str, source: str | None = None) -> str:
    title = title.strip()
    if source and title.endswith(f" - {source}"):
        return title[: -len(source) - 3].strip()
    return title


def normalize_title(title: str) -> str:
    t = unicodedata.normalize("NFKC", title).lower().strip()
    t = _SOURCE_SUFFIX.sub("", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _tokens(norm_title: str) -> set[str]:
    out: set[str] = set()
    for word in norm_title.split():
        if _CJK.search(word):
            chars = [c for c in word if not c.isspace()]
            out.update(a + b for a, b in zip(chars, chars[1:]))
            if len(chars) == 1:
                out.add(chars[0])
        elif len(word) > 1:
            out.add(word)
    return out


def title_similarity(a: str, b: str) -> float:
    """两条（已规范化或原始）标题的 Jaccard 相似度，0~1。"""
    ta, tb = _tokens(normalize_title(a)), _tokens(normalize_title(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def truncate(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"
