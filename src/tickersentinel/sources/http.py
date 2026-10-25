"""共享 HTTP 工具：统一超时、User-Agent、简单重试（429 / 5xx / 网络错误）。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from tickersentinel.sources.base import SourceError

log = logging.getLogger(__name__)

DEFAULT_UA = "Mozilla/5.0 (compatible; tickersentinel/0.1)"
_RETRY_STATUS = {429, 500, 502, 503, 504}


def make_client(timeout: float = 12.0, headers: dict[str, str] | None = None, **kw: Any) -> httpx.AsyncClient:
    h = {"User-Agent": DEFAULT_UA}
    h.update(headers or {})
    return httpx.AsyncClient(timeout=timeout, headers=h, follow_redirects=True, **kw)


async def request(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    retries: int = 2,
    backoff: float = 1.5,
    source: str = "",
) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code in _RETRY_STATUS and attempt < retries:
                await asyncio.sleep(backoff ** (attempt + 1))
                continue
            if resp.status_code >= 400:
                raise SourceError(f"[{source}] HTTP {resp.status_code}：{_redact(str(resp.url))} {resp.text[:200]!r}",
                                  status=resp.status_code)
            return resp
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt < retries:
                await asyncio.sleep(backoff ** (attempt + 1))
                continue
    raise SourceError(f"[{source}] 网络错误：{_redact(url)}（{type(last_exc).__name__}: {last_exc}）")


async def get_json(client: httpx.AsyncClient, url: str, **kw: Any) -> Any:
    resp = await request(client, url, **kw)
    try:
        return resp.json()
    except ValueError as exc:
        raise SourceError(f"[{kw.get('source', '')}] 返回的不是 JSON：{resp.text[:200]!r}") from exc


async def get_bytes(client: httpx.AsyncClient, url: str, **kw: Any) -> bytes:
    return (await request(client, url, **kw)).content


def _redact(url: str) -> str:
    """日志里隐藏 token / apikey。"""
    import re

    return re.sub(r"(token|apikey|api_key|crtfc_key)=[^&]+", r"\1=***", url)
