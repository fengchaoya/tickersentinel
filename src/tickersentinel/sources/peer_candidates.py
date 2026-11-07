"""发现联动时的候选来源（tsen scope、飞书 /scope 用）：Yahoo 的公司资料 / “相似股票” / 行业龙头、Finnhub 同业。

这些只用来**找候选**，最后选谁由数据决定（scope/discover.py 的残差相关、夜间偏相关）。2026-10-23 实测：
- Yahoo “相似股票”（v6/finance/recommendationsbysymbol）：LITE → COHR、AAOI、CRDO、NBIS、VRT；AMD → INTC、MU、NFLX、PLTR、TSM，
  有用的和“同一批人也在看”的热门股混在一起。
- Yahoo 行业（Ticker.info 的 industryKey → yf.Industry(...).top_companies）：LITE 属于 communication-equipment，
  龙头是 CSCO、MSI、HPE、ZBRA……，分类太粗，只当弱候选。
- Yahoo 批量报价（v7/finance/quote）：一次查多个代码的公司名，查不到的代码不返回——用来核对大模型给的海外代码是不是那家公司
  （本地模型的知识可能过时，代码也可能记错）。
- Finnhub /stock/peers（免费，有 key 时）：同一国家、同一细分行业。
都经 yfinance 内部的 YfData（它负责 Yahoo 的 cookie / crumb），同步调用放进线程；失败抛 SourceError，由调用方降级。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

from tickersentinel.sources.base import SourceError

log = logging.getLogger(__name__)

QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
SIMILAR_URL = "https://query2.finance.yahoo.com/v6/finance/recommendationsbysymbol/{symbol}"
FINNHUB_PEERS_URL = "https://finnhub.io/api/v1/stock/peers"


@dataclass
class Profile:
    symbol: str
    name: str
    exchange: str = ""
    quote_type: str = ""
    sector: str = ""
    industry: str = ""
    industry_key: str = ""


def _quotes_sync(symbols: list[str], timeout: float) -> dict[str, Profile]:
    from yfinance.data import YfData

    out: dict[str, Profile] = {}
    for i in range(0, len(symbols), 40):
        chunk = symbols[i:i + 40]
        resp = YfData().get(QUOTE_URL, params={"symbols": ",".join(chunk)}, timeout=timeout)
        if resp.status_code != 200:
            raise SourceError(f"[yahoo] quote HTTP {resp.status_code}")
        for q in (resp.json().get("quoteResponse") or {}).get("result") or []:
            sym = q.get("symbol")
            if sym:
                out[sym] = Profile(sym, q.get("longName") or q.get("shortName") or sym,
                                   exchange=q.get("fullExchangeName") or q.get("exchange") or "",
                                   quote_type=q.get("quoteType") or "")
    return out


async def yahoo_names(symbols: list[str], timeout: float = 15.0) -> dict[str, Profile]:
    """代码 → Yahoo 上的公司名（查不到的代码不在结果里）。"""
    symbols = list(dict.fromkeys(s for s in symbols if s))
    if not symbols:
        return {}
    try:
        return await asyncio.to_thread(_quotes_sync, symbols, timeout)
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError(f"[yahoo] 批量报价失败：{type(exc).__name__}: {exc}") from exc


def _profile_sync(symbol: str) -> Profile:
    import yfinance as yf

    info = yf.Ticker(symbol).info or {}
    return Profile(symbol, info.get("longName") or info.get("shortName") or symbol,
                   exchange=info.get("fullExchangeName") or info.get("exchange") or "",
                   quote_type=info.get("quoteType") or "", sector=info.get("sector") or "",
                   industry=info.get("industry") or "", industry_key=info.get("industryKey") or "")


async def yahoo_profile(symbol: str) -> Profile:
    try:
        return await asyncio.to_thread(_profile_sync, symbol)
    except Exception as exc:
        raise SourceError(f"[yahoo] {symbol} 公司资料取不到：{type(exc).__name__}: {exc}") from exc


def _similar_sync(symbol: str, timeout: float) -> list[str]:
    from yfinance.data import YfData

    resp = YfData().get(SIMILAR_URL.format(symbol=symbol), timeout=timeout)
    if resp.status_code != 200:
        raise SourceError(f"[yahoo] 相似股票 HTTP {resp.status_code}")
    res = (resp.json().get("finance") or {}).get("result") or []
    return [x["symbol"] for x in (res[0].get("recommendedSymbols") or [])] if res else []


async def yahoo_similar(symbol: str, timeout: float = 15.0) -> list[str]:
    try:
        return await asyncio.to_thread(_similar_sync, symbol, timeout)
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError(f"[yahoo] 相似股票取不到：{type(exc).__name__}: {exc}") from exc


def _industry_sync(key: str, n: int) -> list[str]:
    import yfinance as yf

    top = yf.Industry(key).top_companies
    return [str(s) for s in list(top.index)[:n]] if top is not None else []


async def yahoo_industry_top(industry_key: str, n: int = 12) -> list[str]:
    if not industry_key:
        return []
    try:
        return await asyncio.to_thread(_industry_sync, industry_key, n)
    except Exception as exc:
        raise SourceError(f"[yahoo] 行业 {industry_key} 的龙头取不到：{type(exc).__name__}: {exc}") from exc


async def finnhub_peers(symbol: str, key: str, timeout: float = 15.0,
                        client: httpx.AsyncClient | None = None) -> list[str]:
    own = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    try:
        resp = await client.get(FINNHUB_PEERS_URL, params={"symbol": symbol, "token": key})
        if resp.status_code != 200:
            raise SourceError(f"[finnhub] peers HTTP {resp.status_code}")
        data = resp.json()
        return [s for s in data if isinstance(s, str) and s != symbol] if isinstance(data, list) else []
    except httpx.HTTPError as exc:
        raise SourceError(f"[finnhub] peers 网络错误：{type(exc).__name__}") from exc
    finally:
        if own:
            await client.aclose()
