"""行情源的组合：主源 + 兜底，日线收盘价取官方数据。

2026-10-23 用免费 Alpaca 账户实测（MU / SNDK / SKHY / SOXX，10-20～10-23）后的取舍：
- **实时分钟线以 yfinance 为主**：盘前盘后 04:00–20:00 完整；Alpaca 免费的 IEX 只有美东约 08:00–17:00，个股还缺 5–20% 的分钟。
- **兜底用 Alpaca**：主源报错或缺某个代码时补上。查的是 15 分钟以前的数据（基线、回放）就用 SIP（全市场，04:00–20:00 完整），
  更近的只能用 IEX（免费账户的 SIP 不给最近 15 分钟）。
- **分钟线不混用两个来源**：两边价格逐分钟一致，但成交量口径完全不同（yfinance 盘前成交量全为 0，盘中 MU 是 SIP 的 2.7 倍），
  基线的成交量画像和实时成交量必须来自同一处，否则“成交量为历史同时段的几倍”没有意义。所以只在主源整体失败或缺代码时才整段改用兜底。
- **日线收盘价优先用 SIP**：SIP 日线与官方收盘价一致（IEX 日线是 IEX 最后一笔成交，差 0.01–0.08%）。只替换主源日线里 SIP 也有的
  那几天的 close，其余列（如 yfinance 的复权价 adj_close）和当天还没收盘的那一行都保留主源的；SIP 失败就照用主源。
"""

from __future__ import annotations

import asyncio
import logging

import pandas as pd

from tickersentinel.models import Quote
from tickersentinel.sources.base import PriceSource, SourceError
from tickersentinel.sources.prices_alpaca import SIP_DELAY, leaves
from tickersentinel.timeutil import utcnow

log = logging.getLogger(__name__)


def _empty(df: pd.DataFrame | None) -> bool:
    return df is None or df.empty


class FallbackPriceSource(PriceSource):
    """主源失败或缺代码时，用兜底源补。history（可选）用于 15 分钟以前的分钟线请求。"""

    def __init__(self, primary: PriceSource, fallback: PriceSource, history: PriceSource | None = None):
        self.primary = primary
        self.fallback = fallback
        self.history = history
        self.name = f"{primary.name}（兜底 {fallback.name}）"
        self._degraded: set[str] = set()  # 正在用兜底的方法：只在状态变化时打警告，不每分钟刷屏

    def children(self) -> list[PriceSource]:
        return [x for x in (self.primary, self.fallback, self.history) if x is not None]

    def _note(self, method: str, missing: list[str], why: str, used: str) -> None:
        if method not in self._degraded:
            self._degraded.add(method)
            log.warning("行情主源 %s 的 %s %s，改用 %s：%s", self.primary.name, method, why, used, "、".join(missing))
        else:
            log.debug("继续用 %s 补 %s：%s", used, method, "、".join(missing))

    def _recovered(self, method: str) -> None:
        if method in self._degraded:
            self._degraded.discard(method)
            log.info("行情主源 %s 的 %s 已恢复", self.primary.name, method)

    async def _with_fallback(self, method: str, symbols: list[str], backup: PriceSource, *args, **kw) -> dict:
        symbols = list(symbols)
        error: Exception | None = None
        try:
            out = dict(await getattr(self.primary, method)(symbols, *args, **kw))
        except Exception as exc:
            error, out = exc, {}
        if method == "latest_quotes":
            missing = [s for s in symbols if s not in out]
        else:
            missing = [s for s in symbols if _empty(out.get(s))]
        if not missing:
            self._recovered(method)
            return out
        why = f"失败（{type(error).__name__}: {error}）" if error else "没有返回"
        try:
            extra = await getattr(backup, method)(missing, *args, **kw)
        except Exception as exc:
            if error is not None:
                raise SourceError(f"行情主源与兜底都失败：{self.primary.name} {error}；{backup.name} {exc}") from exc
            log.info("兜底 %s 也没取到 %s：%s", backup.name, "、".join(missing), exc)
            return out
        got = [s for s in missing if s in extra and (method == "latest_quotes" or not _empty(extra[s]))]
        if got:
            self._note(method, got, why, backup.name)
        out.update({s: extra[s] for s in got})
        if error is not None and not out:
            raise SourceError(f"行情主源 {self.primary.name} 失败，兜底 {backup.name} 也没有数据：{error}")
        return out

    async def intraday_bars(self, symbols, start, end=None, *, prepost: bool = False):
        old = end is not None and self.history is not None and end <= utcnow() - SIP_DELAY
        backup = self.history if old else self.fallback
        return await self._with_fallback("intraday_bars", symbols, backup, start, end, prepost=prepost)

    async def daily_bars(self, symbols, lookback_days):
        return await self._with_fallback("daily_bars", symbols, self.history or self.fallback, lookback_days)

    async def latest_quotes(self, symbols) -> dict[str, Quote]:
        return await self._with_fallback("latest_quotes", symbols, self.fallback)

    async def aclose(self) -> None:
        for leaf in leaves(self):
            await leaf.aclose()


def apply_official_closes(primary: dict[str, pd.DataFrame], official: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """用官方日线的 close 替换主源日线里同一天的 close；主源没有的代码直接用官方日线。"""
    out: dict[str, pd.DataFrame] = {}
    for sym in {*primary, *official}:
        a, b = primary.get(sym), official.get(sym)
        if _empty(a):
            if not _empty(b):
                out[sym] = b
            continue
        if _empty(b) or "close" not in b:
            out[sym] = a
            continue
        a = a.copy()
        common = a.index.intersection(b.index)
        if len(common):
            a.loc[common, "close"] = b.loc[common, "close"].astype(float).to_numpy()
        out[sym] = a
    return out


class OfficialCloseSource(PriceSource):
    """日线的收盘价换成官方收盘（Alpaca SIP）；分钟线和报价原样交给内层。"""

    def __init__(self, inner: PriceSource, official: PriceSource):
        self.inner = inner
        self.official = official
        self.name = inner.name
        self._warned = False

    def children(self) -> list[PriceSource]:
        return [self.inner, self.official]

    async def intraday_bars(self, symbols, start, end=None, *, prepost: bool = False):
        return await self.inner.intraday_bars(symbols, start, end, prepost=prepost)

    async def latest_quotes(self, symbols) -> dict[str, Quote]:
        return await self.inner.latest_quotes(symbols)

    async def daily_bars(self, symbols, lookback_days):
        prim, off = await asyncio.gather(self.inner.daily_bars(symbols, lookback_days),
                                         self.official.daily_bars(symbols, lookback_days), return_exceptions=True)
        if isinstance(off, BaseException):
            if not self._warned:
                log.warning("官方日线（%s）取不到，收盘价沿用 %s：%s", self.official.name, self.inner.name, off)
                self._warned = True
            off = {}
        else:
            self._warned = False
        if isinstance(prim, BaseException):
            if not off:
                raise prim
            log.warning("日线主源失败，改用官方日线：%s", prim)
            prim = {}
        return apply_official_closes(prim, off)

    async def aclose(self) -> None:
        for leaf in leaves(self):
            await leaf.aclose()


__all__ = ["FallbackPriceSource", "OfficialCloseSource", "apply_official_closes"]
