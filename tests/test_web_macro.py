"""宏观页的数据层（web/macro_data.py）与接口（web/pages/macro.py）：窗口、期货替换、较起点的变化、z 值排行、
收益率曲线、小走势线、补抓。全部离线（合成的分钟线写进内存库）。"""

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("fastapi")

from tickersentinel.barstore import BarStore  # noqa: E402
from tickersentinel.market_calendar import MarketCalendar  # noqa: E402
from tickersentinel.models import Alert, Trigger  # noqa: E402
from tickersentinel.storage import Storage  # noqa: E402
from tickersentinel.timeutil import UTC  # noqa: E402
from tickersentinel.web import macro_data as md  # noqa: E402
from tickersentinel.web.alerts_chart import shift  # noqa: E402

from web_support import client, make_ctx, write_headers  # noqa: E402

NOW = datetime(2026, 10, 28, 18, 0, tzinfo=UTC)  # 周三 14:00 美东（盘中）


def walk(start: datetime, end: datetime, base: float, sigma: float, seed: int, *, yield_: bool = False) -> pd.DataFrame:
    idx = pd.date_range(start, end, freq="1min", tz=UTC, inclusive="left")
    rng = np.random.default_rng(seed)
    steps = rng.normal(0, sigma, len(idx))
    close = base + np.cumsum(steps) if yield_ else base * np.exp(np.cumsum(steps))
    return pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "volume": 1.0}, index=idx)


def seeded_store(*, shock_cl: float = 0.0) -> BarStore:
    """15 天的 ^TNX（收益率）、CL=F、GC=F 分钟线；最后 60 分钟 CL=F 可以注入一次冲击。"""
    st = BarStore(":memory:")
    start = NOW - timedelta(days=15)
    tnx = walk(start, NOW, 4.0, 0.0004, 1, yield_=True)
    cl = walk(start, NOW, 80.0, 0.0004, 2)
    gc = walk(start, NOW, 4000.0, 0.0003, 3)
    if shock_cl:
        last = cl.index >= pd.Timestamp(NOW - timedelta(minutes=60))
        cl.loc[last, "close"] *= np.exp(np.linspace(0, shock_cl, last.sum()))
    st.upsert("^TNX", tnx)
    st.upsert("CL=F", cl)
    st.upsert("GC=F", gc)
    return st


def test_resolve_window_presets_custom_and_alert(settings):
    tz = settings.tz
    a, b, name = md.resolve_window("24h", NOW, tz)
    assert (a, b, name) == (NOW - timedelta(hours=24), NOW, "24 小时")
    a, b, _ = md.resolve_window("today", NOW, tz)
    assert a.astimezone(tz).hour == 0 and a.astimezone(tz).date() == NOW.astimezone(tz).date()
    assert md.resolve_window("bogus", NOW, tz)[2] == "24 小时"
    t = NOW - timedelta(hours=1)
    a, b, name = md.resolve_window(None, NOW, tz, alert_ts=t)
    assert a == t - md.ALERT_BEFORE and b == NOW and name == "异动前后"  # 后 90 分钟截到现在
    a, b, _ = md.resolve_window(None, NOW, tz, start=NOW - timedelta(days=2), end=NOW - timedelta(days=1))
    assert b - a == timedelta(days=1)
    with pytest.raises(ValueError):
        md.resolve_window(None, NOW, tz, start=NOW, end=NOW - timedelta(hours=1))


def test_step_and_pick_symbol(settings):
    assert md.step_minutes(NOW - timedelta(hours=6), NOW) == 1
    assert md.step_minutes(NOW - timedelta(days=5), NOW) == 15
    assert md.step_minutes(NOW - timedelta(days=30), NOW) == 60
    cal = MarketCalendar("XNYS")
    spx = next(i for i in md.indicators(settings) if i.key == "spx")
    assert md.pick_symbol(spx, cal, NOW - timedelta(hours=2), NOW) == ("^GSPC", False)
    assert md.pick_symbol(spx, cal, NOW - timedelta(hours=24), NOW) == ("ES=F", True)
    tnx = next(i for i in md.indicators(settings) if i.key == "us10y")
    assert md.pick_symbol(tnx, cal, NOW - timedelta(hours=24), NOW) == ("^TNX", False)
    keys = [i.key for i in md.indicators(settings)]
    assert "memory.krw" in keys  # 生效主题的板块指标
    assert "ES=F" in md.all_symbols(settings) and "^GSPC" in md.all_symbols(settings)


def test_series_payload_changes_relative_to_window_start(settings):
    st = BarStore(":memory:")
    start = NOW - timedelta(hours=6)
    idx = pd.date_range(start - timedelta(hours=1), NOW, freq="1min", tz=UTC, inclusive="left")
    tnx = np.where(idx < pd.Timestamp(start), 4.00, 4.10)
    cl = np.where(idx < pd.Timestamp(start), 100.0, 102.0)
    st.upsert("^TNX", pd.DataFrame({"close": tnx}, index=idx))
    st.upsert("CL=F", pd.DataFrame({"close": cl}, index=idx))
    p = md.series_payload(st, settings, MarketCalendar("XNYS"), start, NOW)
    items = {it["key"]: it for g in p["groups"] for it in g["items"]}
    assert items["us10y"]["unit"] == "bp" and items["us10y"]["change"] == pytest.approx(10.0)
    assert items["wti"]["unit"] == "%" and items["wti"]["change"] == pytest.approx(2.0)
    assert items["gold"]["points"] == [] and items["gold"]["change"] is None  # 库里没有：照样列出来
    pts = items["wti"]["points"]
    assert pts[0][0] == shift(start, settings.tz) and pts[0][1] == pytest.approx(102.0)  # 小图画实际值
    assert items["wti"]["base"] == pytest.approx(100.0) and items["us10y"]["base"] == pytest.approx(4.0)
    assert p["step_minutes"] == 1 and p["data_until"].startswith("2026-10-28T17:59")
    assert [g["name"] for g in p["groups"]][:2] == ["利率", "大宗商品"]


def test_downsample_keeps_last_in_bucket_and_skips_gaps():
    idx = pd.date_range(NOW, periods=30, freq="1min", tz=UTC)
    s = pd.Series(np.arange(30, dtype=float), index=idx)
    s = s.drop(s.index[10:20])
    d = md.downsample(s, 5)
    assert list(d.to_numpy()) == [4.0, 9.0, 24.0, 29.0]


def test_movers_ranks_shocked_variable_first(settings):
    st = seeded_store(shock_cl=0.03)  # 原油最后一小时涨 3%
    res = md.movers(st, settings, MarketCalendar("XNYS"), NOW - timedelta(minutes=60), NOW)
    items = res["items"]
    assert items[0]["key"] == "wti" and items[0]["z"] > 4 and items[0]["change"] == pytest.approx(3.0, abs=0.3)
    others = [i for i in items if i["key"] != "wti"]
    assert all(abs(i["z"]) < 4 for i in others)
    assert {i["unit"] for i in items} == {"bp", "%"}
    assert items[0]["minutes"] == 60


def test_movers_without_history_have_no_z_and_sort_last(settings):
    st = BarStore(":memory:")
    idx = pd.date_range(NOW - timedelta(minutes=30), NOW, freq="1min", tz=UTC)
    st.upsert("CL=F", pd.DataFrame({"close": np.linspace(80, 81, len(idx))}, index=idx))
    st.upsert("GC=F", pd.DataFrame({"close": np.linspace(4000, 3990, len(idx))}, index=idx))
    items = md.movers(st, settings, None, NOW - timedelta(minutes=20), NOW)["items"]
    assert all(i["z"] is None for i in items)
    assert [i["key"] for i in items] == ["wti", "gold"]  # 没有 z 时按变化的绝对值


def test_curve_payload_and_sparkline():
    st = BarStore(":memory:")
    st.upsert_curve([(date(2026, 10, 27), {"2Y": 3.6, "10Y": 4.1}), (date(2026, 10, 28), {"2Y": 3.5, "10Y": 4.1})])
    c = md.curve_payload(st, date(2026, 10, 1))
    assert c["days"] == ["2026-10-27", "2026-10-28"]
    assert c["series"]["2s10s"] == [["2026-10-27", 50.0], ["2026-10-28", 60.0]]
    assert md.curve_payload(BarStore(":memory:"), date(2026, 1, 1)) == {"days": [], "series": {}}
    seeded = seeded_store()
    sp = md.sparkline(seeded, "CL=F", NOW)
    assert sp is not None and len(sp["points"].split()) <= 66 and sp["width"] == 64
    assert md.sparkline(seeded, "NOPE", NOW) is None


def _alert(ts):
    trig = Trigger(rule="rapid", direction=-1, move_pct=-3.1, window_minutes=30, z=-5.0)
    return Alert(symbol="MU", ts=ts, price=100.0, prev_close=103.0, triggers=[trig], window_minutes=30, move_pct=-3.1,
                 benchmark_move_pct=-1.0, beta=1.2, abnormal_move_pct=-1.9, volume_ratio=2.0, peers_move_pct={},
                 is_escalation=False, segment="regular", benchmark="SOXX")


async def test_api_series_movers_curve_and_alert_window(settings):
    storage = Storage(":memory:")
    aid = storage.record_alert(_alert(NOW - timedelta(minutes=30)), "2026-10-28")
    ctx = make_ctx(settings, storage=storage, now=NOW, bars=seeded_store(shock_cl=0.02))
    async with client(ctx) as c:
        r = await c.get("/api/macro/series?window=24h")
        assert r.status_code == 200
        js = r.json()
        assert js["name"] == "24 小时" and js["step_minutes"] == 5
        assert js["alerts"] and js["alerts"][0]["id"] == aid and js["alerts"][0]["name"]
        assert js["freshness"] == {"running": False, "collector": None, "can_refresh": False, "store": True}
        r = await c.get(f"/api/macro/series?alert={aid}")
        assert r.json()["name"] == "异动前后" and r.json()["alert_id"] == aid
        assert (await c.get("/api/macro/series?alert=999")).status_code == 404
        assert (await c.get("/api/macro/series?start=bogus")).status_code == 400
        r = await c.get("/api/macro/movers", params={"start": (NOW - timedelta(hours=1)).isoformat(), "end": NOW.isoformat()})
        assert r.json()["items"][0]["key"] == "wti"
        # 不带时区的时间按本地时区理解
        local = (NOW - timedelta(hours=1)).astimezone(settings.tz).replace(tzinfo=None).isoformat(timespec="minutes")
        r2 = await c.get("/api/macro/movers", params={"start": local})
        assert r2.json()["start"] == (NOW - timedelta(hours=1)).astimezone(settings.tz).isoformat()
        assert (await c.get("/api/macro/curve?days=30")).json() == {"days": [], "series": {}}


async def test_api_refresh_requires_token_and_collector(settings):
    class FakeCollector:
        calls = 0

        async def collect(self):
            FakeCollector.calls += 1
            return 42

        def status_json(self):
            return {"failures": 0, "backfilling": 0}

    ctx = make_ctx(settings, now=NOW, bars=BarStore(":memory:"))
    async with client(ctx) as c:
        assert (await c.post("/api/bars/refresh")).status_code == 403
        assert (await c.post("/api/bars/refresh", headers=write_headers(ctx))).status_code == 409
    ctx = make_ctx(settings, now=NOW, bars=BarStore(":memory:"), collector=FakeCollector())
    async with client(ctx) as c:
        r = await c.post("/api/bars/refresh", headers=write_headers(ctx))
        assert r.status_code == 200 and r.json()["rows"] == 42 and FakeCollector.calls == 1
    ctx = make_ctx(settings, now=NOW, bars=BarStore(":memory:"), collector=FakeCollector(), mode="preview")
    async with client(ctx) as c:
        assert (await c.post("/api/bars/refresh", headers=write_headers(ctx))).status_code == 409


async def test_api_without_store_is_503(settings):
    async with client(make_ctx(settings, now=NOW)) as c:
        assert (await c.get("/api/macro/series")).status_code == 503


# ───────── 页面 ─────────


async def test_macro_page_renders_skeleton_and_nav(settings):
    storage = Storage(":memory:")
    aid = storage.record_alert(_alert(NOW - timedelta(minutes=30)), "2026-10-28")
    ctx = make_ctx(settings, storage=storage, now=NOW, bars=seeded_store())
    async with client(ctx) as c:
        r = await c.get("/macro")
        assert r.status_code == 200
        html = r.text
        assert "谁动了" in html and 'href="/macro"' in html and ">宏观<" in html
        assert 'data-query="window=24h"' in html and "/static/js/macro.js" in html
        assert "现在补抓" not in html  # 没有采集器
        r = await c.get("/macro?window=5d")
        assert r.status_code == 200 and 'data-query="window=5d"' in r.text
        # 带异动：显示“异动前后”并链接回异动详情
        r = await c.get(f"/macro?alert={aid}")
        assert r.status_code == 200
        assert f'data-query="alert={aid}"' in r.text and f'href="/alerts/{aid}"' in r.text and "异动前后" in r.text
        # 自定义起止（本地时间，不带时区）
        r = await c.get("/macro", params={"start": "2026-10-28T09:00", "end": "2026-10-28T12:00"})
        assert r.status_code == 200 and "start=2026-10-28T09%3A00" in r.text


async def test_macro_page_bad_params_do_not_500(settings):
    ctx = make_ctx(settings, now=NOW, bars=seeded_store())
    async with client(ctx) as c:
        for q in ("?alert=999", "?alert=abc", "?start=bogus", "?end=2026-10-28T09:00",
                  "?start=2026-10-28T12:00&end=2026-10-28T09:00"):
            r = await c.get("/macro" + q)
            assert r.status_code == 200, q
            assert 'data-query="window=24h"' in r.text, q
        r = await c.get("/macro?alert=999")
        assert "没有这条异动" in r.text


async def test_macro_page_preview_is_readonly_and_store_missing(settings):
    class FakeCollector:
        async def collect(self):
            return 0

        def status_json(self):
            return {"failures": 0, "backfilling": 0}

    ctx = make_ctx(settings, now=NOW, bars=seeded_store(), collector=FakeCollector())
    async with client(ctx) as c:
        assert "现在补抓" in (await c.get("/macro")).text
    ctx = make_ctx(settings, now=NOW, bars=seeded_store(), collector=FakeCollector(), mode="preview")
    async with client(ctx) as c:
        html = (await c.get("/macro")).text
        assert "只读" in html and "现在补抓" not in html
    async with client(make_ctx(settings, now=NOW)) as c:  # 分钟线库没打开：说明怎么补，不加载脚本
        r = await c.get("/macro")
        assert r.status_code == 200 and "tsen bars --backfill" in r.text and "/static/js/macro.js" not in r.text


def test_movers_put_big_drops_first_too(settings):
    """按 |z| 排：大跌的变量和大涨的一样排在前面（截取前几条时不会被截掉）。"""
    st = seeded_store(shock_cl=0.01)
    start = NOW - timedelta(days=15)
    gc = walk(start, NOW, 4000.0, 0.0003, 3)
    last = gc.index >= pd.Timestamp(NOW - timedelta(minutes=60))
    gc.loc[last, "close"] *= np.exp(np.linspace(0, -0.03, last.sum()))
    st.upsert("GC=F", gc)
    items = md.movers(st, settings, MarketCalendar("XNYS"), NOW - timedelta(minutes=60), NOW)["items"]
    assert items[0]["key"] == "gold" and items[0]["z"] < -4


async def test_huge_alert_id_is_not_500(settings):
    ctx = make_ctx(settings, now=NOW, bars=BarStore(":memory:"))
    async with client(ctx) as c:
        assert (await c.get("/api/macro/series?alert=99999999999999999999999")).status_code == 404
        r = await c.get("/macro?alert=99999999999999999999999")
        assert r.status_code == 200 and "没有这条异动" in r.text


# ───────── 休市截到最近收盘、收益率用期货折算、空档 ─────────

SAT = datetime(2026, 10, 31, 21, 30, tzinfo=UTC)  # 周六 16:30 CDT
FRI_CLOSE = datetime(2026, 10, 30, 21, 0, tzinfo=UTC)  # 周五 16:00 CDT：期货最后一根是 20:59


def weekend_store() -> BarStore:
    """期货（ES=F、CL=F）到周五 16:00 CDT，比特币到现在（周六）。"""
    st = BarStore(":memory:")
    st.upsert("ES=F", walk(FRI_CLOSE - timedelta(days=3), FRI_CLOSE, 7600.0, 0.0003, 11))
    st.upsert("CL=F", walk(FRI_CLOSE - timedelta(days=3), FRI_CLOSE, 100.0, 0.0004, 12))
    st.upsert("BTC-USD", walk(FRI_CLOSE - timedelta(days=3), SAT, 77000.0, 0.0004, 13))
    return st


def test_market_end_anchors_presets_on_weekend(settings):
    st = weekend_store()
    anchor = md.market_end(st, settings, SAT)
    assert anchor == FRI_CLOSE  # 最后一根 20:59 的结束时刻；比特币不算
    a, b, _ = md.resolve_window("24h", SAT, settings.tz, anchor=anchor)
    assert (a, b) == (FRI_CLOSE - timedelta(hours=24), FRI_CLOSE)
    a, _, _ = md.resolve_window("today", SAT, settings.tz, anchor=anchor)
    assert a.astimezone(settings.tz).date() == FRI_CLOSE.astimezone(settings.tz).date()  # “今天”= 周五
    # 正常交易时（最后一根不到 2 小时前）不截
    assert md.market_end(st, settings, FRI_CLOSE + timedelta(minutes=30)) is None
    assert md.market_end(BarStore(":memory:"), settings, SAT) is None


async def test_api_series_on_weekend_shows_friday_and_live_shows_now(settings):
    ctx = make_ctx(settings, now=SAT, bars=weekend_store())
    async with client(ctx) as c:
        d = (await c.get("/api/macro/series?window=24h")).json()
        live = (await c.get("/api/macro/series?window=24h&live=1")).json()
        page = (await c.get("/macro?window=24h")).text
        live_page = (await c.get("/macro?window=5d&live=1")).text
    items = {i["key"]: i for g in d["groups"] for i in g["items"]}
    assert d["anchored"] == FRI_CLOSE.isoformat() and d["end"] == FRI_CLOSE.isoformat()
    assert items["wti"]["points"] and items["btc"]["points"]  # 期货和比特币画同一段
    assert live["anchored"] is None
    items = {i["key"]: i for g in live["groups"] for i in g["items"]}
    assert not items["wti"]["points"] and items["btc"]["points"]
    assert "周末休市" in page and "live=1" in page and "截到现在" in page
    assert "回到最近收盘" in live_page and "window=1m&amp;live=1" in live_page  # 切窗口时保留“截到现在”


def tsy_store(*, days: int = 4) -> tuple[BarStore, datetime]:
    """ZN=F 全天都有；^TNX 只有美东 08:20–15:00，按 −0.14 × Δln 期货价（%）走（≈ −14bp / 1%）。"""
    end = datetime(2026, 10, 30, 21, 0, tzinfo=UTC)
    zn = walk(end - timedelta(days=days), end, 112.0, 0.0002, 21)
    lp = np.log(zn["close"].to_numpy())
    rng = np.random.default_rng(22)
    y = 4.9 - 0.14 * (lp - lp[0]) * 100 + rng.normal(0, 0.0003, len(lp))
    tnx = pd.DataFrame({"close": y}, index=zn.index)
    et = tnx.index.tz_convert("America/New_York")
    mins = et.hour * 60 + et.minute
    tnx = tnx[(mins >= 8 * 60 + 20) & (mins < 15 * 60)]
    st = BarStore(":memory:")
    st.upsert("ZN=F", zn)
    st.upsert("^TNX", tnx)
    return st, end


def test_proxy_symbol_defaults_and_collect_target(settings):
    from tickersentinel.jobs.collect import collect_targets

    ind = {i.key: i for i in settings.macro.indicators}
    assert ind["us10y"].proxy_symbol == "ZN=F" and ind["us30y"].proxy_symbol == "ZB=F"
    assert ind["wti"].proxy_symbol is None
    assert ind["us10y"].model_copy(update={"night_proxy": None}).proxy_symbol is None
    targets = collect_targets(settings, MarketCalendar())
    assert "ZN=F" in targets and "ZB=F" in targets


def test_splice_fills_overnight_from_futures():
    st, end = tsy_store()
    data = st.series(["^TNX", "ZN=F"], end - timedelta(days=4), end)
    y, p = data["^TNX"]["close"], data["ZN=F"]["close"]
    beta = md.proxy_beta(y, p)
    assert beta is not None and -15.5 < beta < -12.5  # 期货涨 1% ≈ −14bp
    out, mark = md.splice(y, p, beta)
    assert mark.sum() > 0 and not mark[y.index].any()  # 真实数据原样保留
    # 期货有数据的分钟几乎都有值；只差最后一根真实数据之后、第一根之前各 PROXY_GAP（那几分钟可能只是收益率还没到）
    assert len(out) == len(p) - 2 * 10
    # 折算的值和“真实”关系一致：夜里某一刻的折算收益率 ≈ 按同一关系算出来的
    lp = np.log(p)
    t = out[mark].index[len(out[mark]) // 2]
    expect = 4.9 - 0.14 * (lp[t] - lp.iloc[0]) * 100
    assert abs(out[t] - expect) < 0.01
    # 盘中只缺一两分钟的不补
    holey = y.drop(y.index[100:102])
    _, m2 = md.splice(holey, p, beta)
    assert not m2[(m2.index >= y.index[99]) & (m2.index <= y.index[102])].any()
    # 期货和收益率没关系：不折算
    noise = pd.Series(100 * np.exp(np.random.default_rng(5).normal(0, 1e-3, len(p)).cumsum()), index=p.index)
    assert md.proxy_beta(y, noise) is None


def test_series_payload_fill_and_movers_with_proxy(settings):
    st, end = tsy_store()
    start = end - timedelta(hours=24)
    d = md.series_payload(st, settings, None, start, end)
    tnx = next(i for g in d["groups"] for i in g["items"] if i["key"] == "us10y")
    assert tnx["points"] and tnx["fill"] and tnx["proxy"] == "ZN=F" and tnx["beta_bp"] < 0
    real_t = {p[0] for p in tnx["points"]}
    fill_t = [p[0] for p in tnx["fill"]]
    assert set(fill_t) - real_t  # 有折算的点
    assert any(t in real_t for t in fill_t)  # 虚线带上相邻的真实点，和实线接得上
    assert tnx["value_implied"]  # 15:00 以后到 16:00 是折算的
    mv = {i["key"]: i for i in md.movers(st, settings, None, start, end)["items"]}
    assert mv["us10y"]["proxy"] == "ZN=F" and mv["us10y"]["z"] is not None


def test_sparkline_ends_at_symbols_last_bar_and_uses_proxy():
    st = weekend_store()
    assert md.sparkline(st, "CL=F", SAT) is not None  # 周六：画周五收盘前 24 小时，不是空的
    assert md.sparkline(st, "CL=F", SAT + timedelta(days=8)) is None  # 太旧不画
    ts, end = tsy_store()
    assert md.sparkline(ts, "^TNX", end) is not None
    assert md.sparkline(ts, "^TNX", end, proxy="ZN=F") is not None


def test_session_only_series_are_marked_compress(settings):
    """VIX（美东 03:15–16:15）只在交易时段有数据 → compress；期货几乎 24 小时 → 不拼接；收益率有期货补夜间 → 不拼接。"""
    end = datetime(2026, 10, 30, 20, 0, tzinfo=UTC)
    start = end - timedelta(days=12)
    st, _ = tsy_store(days=12)
    st.upsert("CL=F", walk(start, end, 100.0, 0.0004, 31))
    vix = walk(start, end, 19.0, 0.0008, 32)
    et = vix.index.tz_convert("America/New_York")
    mins = et.hour * 60 + et.minute
    st.upsert("^VIX", vix[(et.weekday < 5) & (mins >= 3 * 60 + 15) & (mins < 16 * 60 + 15)])
    assert md.hours_per_day(st.series(["^VIX"], start, end)["^VIX"]["close"]) == pytest.approx(13.0)
    d = md.series_payload(st, settings, None, end - timedelta(days=5), end)
    items = {i["key"]: i for g in d["groups"] for i in g["items"]}
    assert items["vix"]["compress"] and items["vix"]["hours_per_day"] == pytest.approx(13.0)
    assert not items["wti"]["compress"] and items["wti"]["hours_per_day"] > 20
    assert not items["us10y"]["compress"]  # 夜里有 ZN=F 折算
    assert items["vix"]["points"][-1][1] == pytest.approx(items["vix"]["value"])  # 点是实际值
    assert md.hours_per_day(pd.Series(dtype=float)) is None
    # 窗口落在一个交易时段里（没有空档）：不拼接，照常和其他图对齐
    d = md.series_payload(st, settings, None, datetime(2026, 10, 29, 14, 0, tzinfo=UTC),
                          datetime(2026, 10, 29, 18, 0, tzinfo=UTC))
    vix = next(i for g in d["groups"] for i in g["items"] if i["key"] == "vix")
    assert vix["points"] and not vix["compress"]


def test_short_history_is_not_classified_session_only():
    """刚开始补历史、只有半天数据的期货：不判断（None），按 24 小时的画。"""
    idx = pd.date_range(datetime(2026, 10, 30, 4, 0, tzinfo=UTC), periods=180, freq="1min", tz=UTC)
    assert md.hours_per_day(pd.Series(100.0, index=idx)) is None
    idx = pd.date_range(datetime(2026, 10, 28, 4, 0, tzinfo=UTC), datetime(2026, 10, 30, 4, 0, tzinfo=UTC), freq="1min")
    assert md.hours_per_day(pd.Series(100.0, index=idx)) is None  # 头尾去掉后只剩 1 天


# ───────── 持仓组、对比图、盘外换 ETF ─────────


def test_holdings_group_in_series_and_movers(settings):
    st = seeded_store(shock_cl=0.0)
    st.upsert("MU", walk(NOW - timedelta(days=15), NOW, 100.0, 0.0006, 41))
    inds = md.indicators(settings)
    hold = [i for i in inds if i.holding]
    assert [i.symbol for i in hold] == [i.symbol for i in settings.universe.holdings]
    assert all(i.key == f"hold.{i.symbol}" and i.group == md.HOLDING_GROUP for i in hold)
    assert not any(i.holding for i in md.indicators(settings, holdings=False))
    d = md.series_payload(st, settings, None, NOW - timedelta(hours=24), NOW)
    items = {i["key"]: i for g in d["groups"] for i in g["items"]}
    mu = items["hold.MU"]
    assert mu["holding"] and mu["group"] == "持仓" and mu["points"] and mu["sigma_day"] and mu["sigma_day"] > 0
    assert items["hold.SNDK"]["points"] == []  # 库里没有：照样列出来
    assert items["wti"]["sigma_day"] > 0 and items["us10y"]["sigma_day"] > 0  # 对比图“几倍日波动”的分母
    assert [g["name"] for g in d["groups"]][-1] == "持仓"
    mv = {i["key"]: i for i in md.movers(st, settings, None, NOW - timedelta(hours=1), NOW)["items"]}
    assert mv["hold.MU"]["holding"] and not mv["wti"]["holding"]


def test_market_end_includes_holdings_after_hours(settings):
    """周五：期货美东 17:00 收，个股盘后到 20:00 → 截到 20:00。"""
    st = weekend_store()
    fri_8pm = datetime(2026, 10, 31, 0, 0, tzinfo=UTC)  # 周五 20:00 美东
    st.upsert("MU", walk(FRI_CLOSE - timedelta(days=1), fri_8pm, 100.0, 0.0005, 42))
    assert md.market_end(st, settings, SAT) == fri_8pm
    d = md.series_payload(st, settings, None, *md.resolve_window("24h", SAT, settings.tz, anchor=fri_8pm)[:2])
    items = {i["key"]: i for g in d["groups"] for i in g["items"]}
    assert items["hold.MU"]["points"] and items["wti"]["points"]


def test_off_hours_etf_gets_etf_tag(settings):
    from tickersentinel.config import MacroIndicator

    settings.macro.indicators.append(MacroIndicator(key="sox", name="费城半导体", symbol="^SOX", group="股指", kind="level",
                                                    off_hours_symbol="SOXX", off_hours_name="半导体ETF（SOXX）"))
    st = BarStore(":memory:")
    st.upsert("SOXX", walk(NOW - timedelta(days=3), NOW, 300.0, 0.0005, 43))
    d = md.series_payload(st, settings, MarketCalendar("XNYS"), NOW - timedelta(days=2), NOW)
    sox = next(i for g in d["groups"] for i in g["items"] if i["key"] == "sox")
    assert sox["futures"] and sox["sub_tag"] == "ETF" and sox["symbol"] == "SOXX" and sox["name"] == "半导体ETF（SOXX）"
    assert md.sub_tag("ES=F") == "期货"


async def test_macro_page_has_compare_controls(settings):
    ctx = make_ctx(settings, now=NOW, bars=seeded_store())
    async with client(ctx) as c:
        html = (await c.get("/macro")).text
    assert 'data-layout="compare"' in html and 'id="cmp-picks"' in html and 'data-norm="sigma"' in html
    assert "叠在一起" not in html
