"""行情源组合：主源 + Alpaca 兜底（实时 IEX / 历史 SIP）、日线官方收盘价、SIP 的 15 分钟限制、装配。全部离线。"""

from datetime import timedelta

import httpx
import pandas as pd
import pytest

from tickersentinel.config import Secrets
from tickersentinel.models import Quote
from tickersentinel.sources import build_price_source
from tickersentinel.sources.base import PriceSource, SourceError
from tickersentinel.sources.prices_alpaca import SIP_DELAY, AlpacaPriceSource, leaves
from tickersentinel.sources.prices_routing import FallbackPriceSource, OfficialCloseSource, apply_official_closes
from tickersentinel.sources.prices_yfinance import YFinancePriceSource
from tickersentinel.timeutil import utcnow

from conftest import utc


def bars(*closes, start="2026-10-22 13:30"):
    idx = pd.date_range(pd.Timestamp(start, tz="UTC"), periods=len(closes), freq="1min")
    return pd.DataFrame({"close": list(closes), "volume": [100.0] * len(closes)}, index=idx)


def daily(**closes):
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in closes["days"]])
    df = pd.DataFrame({"close": closes["close"]}, index=idx)
    if "adj" in closes:
        df["adj_close"] = closes["adj"]
    return df


class Fake(PriceSource):
    def __init__(self, name, intraday=None, daily_=None, error=None, quotes=None):
        self.name = name
        self.intraday = intraday or {}
        self.daily_ = daily_ or {}
        self.error = error
        self.quotes = quotes or {}
        self.calls = []
        self.closed = 0

    async def intraday_bars(self, symbols, start, end=None, *, prepost=False):
        self.calls.append(("intraday", tuple(symbols), end))
        if self.error:
            raise self.error
        return {s: self.intraday[s] for s in symbols if s in self.intraday}

    async def daily_bars(self, symbols, lookback_days):
        self.calls.append(("daily", tuple(symbols)))
        if self.error:
            raise self.error
        return {s: self.daily_[s] for s in symbols if s in self.daily_}

    async def latest_quotes(self, symbols):
        self.calls.append(("quotes", tuple(symbols)))
        if self.error:
            raise self.error
        return {s: self.quotes[s] for s in symbols if s in self.quotes}

    async def aclose(self):
        self.closed += 1


async def test_fallback_fills_missing_symbols_and_uses_sip_for_old_requests():
    yf = Fake("yfinance", intraday={"MU": bars(1, 2)})
    iex = Fake("alpaca-iex", intraday={"SKHY": bars(5)})
    sip = Fake("alpaca-sip", intraday={"SKHY": bars(6)})
    src = FallbackPriceSource(yf, iex, history=sip)
    now = utcnow()
    out = await src.intraday_bars(["MU", "SKHY"], start=now - timedelta(hours=1))  # 实时：缺的 SKHY 由 IEX 补
    assert list(out["MU"]["close"]) == [1, 2] and list(out["SKHY"]["close"]) == [5]
    assert iex.calls == [("intraday", ("SKHY",), None)] and sip.calls == []
    old_end = now - SIP_DELAY - timedelta(minutes=1)  # 15 分钟以前的（基线、回放）：由 SIP 补
    out = await src.intraday_bars(["MU", "SKHY"], start=now - timedelta(days=5), end=old_end)
    assert list(out["SKHY"]["close"]) == [6] and sip.calls == [("intraday", ("SKHY",), old_end)]
    # 都有就不打扰兜底
    yf.intraday["SKHY"] = bars(9)
    n = len(iex.calls)
    await src.intraday_bars(["MU", "SKHY"], start=now - timedelta(hours=1))
    assert len(iex.calls) == n


async def test_fallback_when_primary_fails_and_error_when_both_fail():
    yf = Fake("yfinance", error=SourceError("Yahoo 429"))
    iex = Fake("alpaca-iex", intraday={"MU": bars(3)}, quotes={"MU": Quote("MU", 100.0, 98.0)})
    src = FallbackPriceSource(yf, iex)
    out = await src.intraday_bars(["MU"], start=utcnow() - timedelta(hours=1))
    assert list(out["MU"]["close"]) == [3]
    assert (await src.latest_quotes(["MU"]))["MU"].price == 100.0
    iex.error = SourceError("401")
    with pytest.raises(SourceError, match="主源与兜底都失败"):
        await src.intraday_bars(["MU"], start=utcnow() - timedelta(hours=1))
    # 主源正常、只是兜底也没有那个代码：照常返回主源有的
    yf.error, yf.intraday = None, {"MU": bars(1)}
    out = await src.intraday_bars(["MU", "XYZ"], start=utcnow() - timedelta(hours=1))
    assert list(out) == ["MU"]


def test_official_closes_replace_close_only_on_common_days():
    prim = {"MU": daily(days=["2026-10-21", "2026-10-22", "2026-10-23"], close=[913.6, 855.9, 877.4], adj=[913.6, 855.9, 877.4]),
            "WDC": daily(days=["2026-10-22"], close=[340.0])}
    off = {"MU": daily(days=["2026-10-21", "2026-10-22"], close=[913.67, 856.49]),
           "SKHY": daily(days=["2026-10-22"], close=[131.69])}
    out = apply_official_closes(prim, off)
    assert list(out["MU"]["close"]) == [913.67, 856.49, 877.4]  # 当天还没有官方收盘：保留主源
    assert list(out["MU"]["adj_close"]) == [913.6, 855.9, 877.4]  # 其他列不动
    assert list(out["WDC"]["close"]) == [340.0] and list(out["SKHY"]["close"]) == [131.69]
    assert list(prim["MU"]["close"]) == [913.6, 855.9, 877.4]  # 不改调用方的数据


async def test_official_close_source_degrades_to_primary():
    inner = Fake("yfinance", daily_={"MU": daily(days=["2026-10-22"], close=[855.9])}, intraday={"MU": bars(1)})
    sip = Fake("alpaca-sip", daily_={"MU": daily(days=["2026-10-22"], close=[856.49])})
    src = OfficialCloseSource(inner, sip)
    assert list((await src.daily_bars(["MU"], 10))["MU"]["close"]) == [856.49]
    sip.error = SourceError("down")
    assert list((await src.daily_bars(["MU"], 10))["MU"]["close"]) == [855.9]
    inner.error, sip.error = SourceError("yahoo down"), None
    assert list((await src.daily_bars(["MU"], 10))["MU"]["close"]) == [856.49]  # 主源坏了：官方日线顶上
    sip.error = SourceError("down")
    with pytest.raises(SourceError):
        await src.daily_bars(["MU"], 10)
    inner.error = None
    assert list((await src.intraday_bars(["MU"], utc(2026, 10, 22, 13)))["MU"]["close"]) == [1]  # 分钟线原样交给内层


async def test_sip_requests_never_ask_for_the_last_15_minutes():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"bars": {}, "next_page_token": None})

    src = AlpacaPriceSource("id", "secret", feed="sip", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    now = utcnow()
    await src.intraday_bars(["MU"], start=now - timedelta(days=1))
    end = pd.Timestamp(seen[0]["end"]).to_pydatetime()
    assert seen[0]["feed"] == "sip" and end <= now - SIP_DELAY + timedelta(seconds=1)
    assert await src.intraday_bars(["MU"], start=now - timedelta(minutes=5)) == {} and len(seen) == 1  # 全在 15 分钟内：不发请求
    await src.aclose()


async def test_build_price_source_wiring(settings):
    settings.secrets = Secrets()
    plain = build_price_source(settings)  # 没有 Alpaca key：只有 yfinance
    assert isinstance(plain.us, YFinancePriceSource) and plain.us is plain.foreign
    settings.secrets = Secrets(alpaca_api_key_id="id", alpaca_api_secret_key="secret")
    src = build_price_source(settings)
    assert isinstance(src.us, OfficialCloseSource) and isinstance(src.us.inner, FallbackPriceSource)
    fb = src.us.inner
    assert isinstance(fb.primary, YFinancePriceSource) and fb.fallback.feed == "iex" and fb.history.feed == "sip"
    assert src.us.official is fb.history  # 官方收盘与历史兜底共用一个 SIP 客户端
    assert len(leaves(src)) == 3  # yfinance、IEX、SIP 各关一次
    await src.aclose()
    settings.sources.prices.fallback = "none"
    settings.sources.prices.official_closes = False
    assert isinstance(build_price_source(settings).us, YFinancePriceSource)
