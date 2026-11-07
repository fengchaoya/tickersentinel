"""美股夜盘（hours: 24x5）：切段、Hyperliquid 数据源、夜盘检测与跨段连续（盘后 → 夜盘 → 盘前 → 盘中不重推）。"""

import json
from datetime import date, datetime, timedelta

import httpx
import pandas as pd
import pytest

from tickersentinel.config import AlertRules
from tickersentinel.detect.clock import UsEquityClock
from tickersentinel.market_calendar import night_window, night_windows
from tickersentinel.sources.prices_overnight import HyperliquidPriceSource, OvernightPriceSource, to_frame
from tickersentinel.timeutil import ET, UTC

from conftest import isolate
from test_segments import SegPrices, make_bars, monitor_for


def et(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET).astimezone(UTC)


MON, TUE, FRI = date(2026, 10, 26), date(2026, 10, 27), date(2026, 10, 30)


# ───────── 切段 ─────────


def test_night_segments_cover_sunday_evening_to_friday_morning(calendar):
    clock = UsEquityClock(calendar, overnight=True)
    sun_night = clock.segment_at(et(date(2026, 10, 25), 21))  # 周日晚上：属于周一
    assert (sun_night.kind, sun_night.day, sun_night.key) == ("night", MON, "2026-10-26")
    assert sun_night.open == et(date(2026, 10, 25), 20) and sun_night.close == et(MON, 4)
    assert sun_night.ref_at == calendar.session(date(2026, 10, 23)).close  # 参照上周五收盘
    assert clock.segment_at(et(MON, 4)).kind == "pre"
    assert clock.segment_at(et(MON, 20)).kind == "night" and clock.segment_at(et(MON, 20)).day == TUE
    assert clock.segment_at(et(FRI, 20, 30)) is None and clock.segment_at(et(date(2026, 10, 31), 12)) is None  # 周末不盯
    assert clock.segment_at(et(date(2026, 11, 25), 21)) is None  # 感恩节前一晚：第二天不是交易日
    assert clock.segment_at(et(date(2026, 11, 26), 21)).day == date(2026, 11, 27)
    # 参照价相同的几段共用一个 key：周一盘后、周二夜盘、盘前、盘中
    post = clock.segment_at(et(MON, 17))
    reg = clock.segment_at(et(TUE, 10))
    assert post.key == reg.key == "2026-10-27" and post.ref_at == reg.ref_at
    assert [g.kind for g in clock.same_key_before(reg)] == ["post", "night", "pre"]
    assert clock.same_key_before(post) == []
    nights = clock.previous(clock.segment_at(et(TUE, 1)), 5)
    assert all(g.kind == "night" for g in nights) and len(nights) == 5
    assert UsEquityClock(calendar).segment_at(et(MON, 22)) is None  # 不开夜盘时照旧
    assert night_window(calendar, date(2026, 10, 31)) is None
    assert len(night_windows(calendar, et(FRI, 18), et(date(2026, 11, 2), 6))) == 1  # 周五晚到周一早：只有周日晚那一段


# ───────── 数据源 ─────────


def hl_transport(now_ms: int, seen: list):
    """假的 Hyperliquid：1 分钟线只给最近 10 分钟（模拟只保留最近 5000 根），更早的要用 5 分钟线。"""

    def candle(t, px, minutes=1):
        return {"t": t, "T": t + minutes * 60_000 - 1, "o": str(px - 1), "h": str(px + 1), "l": str(px - 2),
                "c": str(px), "v": "10", "n": 3, "s": "xyz:MU", "i": f"{minutes}m"}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if body["type"] == "meta":
            return httpx.Response(200, json={"universe": [{"name": "xyz:MU"}, {"name": "xyz:SMH"},
                                                          {"name": "xyz:OLD", "isDelisted": True}]})
        req = body["req"]
        lo, hi = req["startTime"], req["endTime"]
        if req["interval"] == "1m":
            first = now_ms - 10 * 60_000
            ts = range(max(lo, first) // 60_000 * 60_000, hi, 60_000)
            return httpx.Response(200, json=[candle(t, 100 + i) for i, t in enumerate(ts)])
        ts = range(lo // 300_000 * 300_000, hi, 300_000)
        return httpx.Response(200, json=[candle(t, 50 + i, minutes=5) for i, t in enumerate(ts)])

    return httpx.MockTransport(handler)


async def test_hyperliquid_source_maps_symbols_and_fills_older_minutes_from_5m():
    now = datetime(2026, 10, 28, 6, 0, tzinfo=UTC)
    seen: list = []
    src = HyperliquidPriceSource(aliases={"SOXX": "SMH"},
                                 client=httpx.AsyncClient(transport=hl_transport(int(now.timestamp() * 1000), seen)))
    assert await src.coin("mu") == "xyz:MU" and await src.coin("SOXX") == "xyz:SMH"
    assert await src.coin("OLD") is None and await src.coin("NOPE") is None  # 下架的、没上架的都不取
    bars = await src.intraday_bars(["MU", "NOPE"], start=now - timedelta(minutes=30), end=now)
    assert list(bars) == ["MU"]
    df = bars["MU"]
    assert df.index.is_monotonic_increasing and df.index[0] == now - timedelta(minutes=30) and df.index[-1] == now - timedelta(minutes=1)
    assert len(df) == 30 and (df.index.to_series().diff().dropna() == timedelta(minutes=1)).all()
    assert df["close"].iloc[-1] == 109.0  # 最近 10 分钟是 1 分钟线
    older = df[df.index < now - timedelta(minutes=10)]
    # 5 分钟线摊到每分钟：前 4 分钟记开盘价、最后一分钟记收盘价（不提前看到收盘价），成交量平均分
    assert list(older["close"].iloc[:5]) == [49.0, 49.0, 49.0, 49.0, 50.0] and older["volume"].iloc[0] == 2.0
    assert sum(1 for b in seen if b["type"] == "meta") == 1  # 合约列表有缓存
    await src.aclose()


async def test_overnight_source_keeps_only_night_sessions(calendar):
    idx = pd.date_range(et(FRI, 18), et(date(2026, 11, 2), 6), freq="1min", inclusive="left")
    night_df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx)

    class Night(SegPrices):
        async def intraday_bars(self, symbols, start, end=None, *, prepost=False):
            return {"MU": night_df[(night_df.index >= start) & (night_df.index < end)]}

    src = OvernightPriceSource(SegPrices({}), Night({}), calendar)
    out = await src.overnight_bars(["MU"], start=et(FRI, 18), end=et(date(2026, 11, 2), 6))
    got = out["MU"].index
    assert got[0] == et(date(2026, 11, 1), 20) and got[-1] == et(date(2026, 11, 2), 3, 59)  # 只有周日晚到周一 04:00
    assert len(got) == 8 * 60
    assert await src.overnight_bars(["MU"], start=et(date(2026, 10, 31), 1), end=et(date(2026, 10, 31), 23)) == {}


def test_to_frame_empty():
    assert to_frame([]).empty


# ───────── 检测：夜盘、跨段连续 ─────────


class NightSegPrices(SegPrices):
    """白天的分钟线从 intraday_bars 取（像 yfinance），夜盘的从 overnight_bars 取（像 Hyperliquid）。"""

    def __init__(self, bars, daily, calendar):
        super().__init__(bars, daily)
        self.windows = night_windows(calendar, et(date(2026, 10, 1), 0), et(date(2026, 11, 30), 0))
        self.night_calls = 0

    def _night_mask(self, idx):
        m = pd.Series(False, index=idx)
        for a, b in self.windows:
            m |= (idx >= a) & (idx < b)
        return m.to_numpy()

    async def intraday_bars(self, symbols, start, end=None, *, prepost=False):
        out = await super().intraday_bars(symbols, start, end, prepost=prepost)
        return {s: df[~self._night_mask(df.index)] for s, df in out.items()}

    async def overnight_bars(self, symbols, start, end=None):
        self.night_calls += 1
        out = await super().intraday_bars(symbols, start, end)
        return {s: df[self._night_mask(df.index)] for s, df in out.items()}


def segments_24x5(calendar, last: date, n_prior=6):
    clock = UsEquityClock(calendar, overnight=True)
    days = [*calendar.sessions_before(last, n_prior), calendar.session(last)]
    segs = [g for s in days for g in clock.segments_on(s.day)]
    nxt = calendar.next_session(last + timedelta(days=1)).day
    return sorted([*segs, clock.night_segment(nxt)], key=lambda g: g.open)


def night_monitor(settings, calendar, shocks, last=MON, sigma=0.0001):
    isolate(settings, ("MU",))
    settings.universe.classes["us_equity"] = AlertRules(hours="24x5")
    segs = segments_24x5(calendar, last)
    mu, mu_d = make_bars(segs, price=900.0, sigma=sigma, shocks=shocks)
    prices = NightSegPrices({"MU": mu}, {"MU": mu_d}, calendar)
    mon, storage, notifier = monitor_for(settings, calendar, prices, symbols=("MU",))
    return mon, storage, notifier, prices


async def test_night_session_alerts_use_overnight_prices(settings, calendar):
    mon, storage, notifier, prices = night_monitor(settings, calendar, {(("night", TUE), 200 + i): -0.007 for i in range(5)})
    assert mon.describe()[0].startswith("美股 MU：24 小时")
    t0 = et(MON, 20) + timedelta(minutes=206)  # 周一 23:26
    alerts = await mon.tick(now=t0)
    assert [(a.symbol, a.segment) for a in alerts] == [("MU", "night")] and prices.night_calls >= 1
    msg = notifier.sent[0][1]
    assert msg.title.startswith("🔻 MU【夜盘】") and "较前收" in msg.body
    assert storage.alerts_for_day("2026-10-27")  # 夜盘归到它属于的交易日（周二）


async def test_post_market_alert_is_not_repeated_overnight_or_premarket(settings, calendar):
    """周一盘后跌 6%（推“较今日收盘跌超 5%”）→ 夜里、周二盘前、盘中都还在 -6% 附近：同一个参照价、同一个 key，不再推。"""
    mon, storage, notifier, _ = night_monitor(settings, calendar, {(("post", MON), 30): -0.062})
    alerts = await mon.tick(now=et(MON, 16, 35))
    assert alerts and ("day", 5.0) in {(t.rule, t.level_pct) for t in alerts[0].triggers}
    for now in (et(MON, 21), et(TUE, 3, 30), et(TUE, 4, 30), et(TUE, 9, 50)):
        assert not [t for a in await mon.tick(now=now) for t in a.triggers if t.rule == "day"], now


async def test_night_drop_from_post_market_high_is_described(settings, calendar):
    """盘后冲高 6%，夜里回落到收盘价附近：全天累计从盘后的高点算，文案写明“从盘后高点回落”。"""
    shocks = {(("post", MON), 30): 0.06, (("night", TUE), 0): -0.01, (("night", TUE), 60): -0.05}
    mon, _, notifier, _ = night_monitor(settings, calendar, shocks)
    await mon.tick(now=et(MON, 16, 35))
    alerts = await mon.tick(now=et(MON, 20) + timedelta(minutes=62))
    day = next(t for a in alerts for t in a.triggers if t.rule == "day")
    assert day.direction < 0 and day.anchor_kind == "post" and day.anchor_pct == pytest.approx(6.2, abs=0.3)
    assert "全天：从盘后高点回落超 5 个点" in notifier.sent[-1][1].body


async def test_weekend_is_not_monitored(settings, calendar):
    mon, _, notifier, prices = night_monitor(settings, calendar, {}, last=FRI)
    calls = prices.night_calls
    for now in (et(FRI, 21), et(date(2026, 10, 31), 12), et(date(2026, 11, 1), 19, 59)):
        assert await mon.tick(now=now) == []
    assert prices.night_calls == calls and notifier.sent == []
