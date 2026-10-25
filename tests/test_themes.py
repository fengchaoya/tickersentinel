"""主题（config/themes/）：监控范围 = 持仓 × 主题；每只股票用自己主题的基准（含同业篮子）、同业、夜间信号、相关公司、
板块新闻与板块指标；增删持仓时范围跟着变。全部离线。"""

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from tickersentinel.agents.attribution import Attributor
from tickersentinel.agents.render import render_alert, render_macro, render_macro_compact
from tickersentinel.config import Instrument, LLMTasks, MacroIndicator, is_us_symbol, load_settings
from tickersentinel.detect.basket import add_baskets, basket_daily, basket_intraday
from tickersentinel.detect.baseline import prev_close_from_daily
from tickersentinel.detect.clock import ExchangeClock, exchange_of
from tickersentinel.events.hub import EventHub
from tickersentinel.jobs.monitor import IntradayMonitor, build_groups
from tickersentinel.llm.client import LLMClient
from tickersentinel.models import Event, earnings_key
from tickersentinel.news.hub import NewsHub
from tickersentinel.notify.console import ConsoleNotifier
from tickersentinel.scope import describe_scope
from tickersentinel.sources.macro import MacroSource
from tickersentinel.storage import Storage
from tickersentinel.timeutil import ET, UTC

from conftest import TARGET_DAY, FakePriceSource, at, build_market, isolate, utc


def lite() -> Instrument:
    return Instrument(symbol="LITE", name="Lumentum", themes=["optics"], queries={"en": ["Lumentum"]})


# ───────── 范围推导 ─────────


def test_repo_scope_memory_active_optics_spare(settings):
    u = settings.universe
    assert [t.key for t in u.active_themes] == ["memory"] and "optics" in u.themes
    hynix = u.find("000660.KS")  # 主题的夜间信号自动成为只盯盘的资产
    assert hynix is not None and hynix.origin == "theme" and not hynix.holding and hynix.asset_class == "foreign_equity"
    assert hynix.related_to == ["MU", "SNDK", "SKHY"] and u.label("000660.KS") == "SK海力士(首尔)"
    assert [i.symbol for i in u.monitored] == ["MU", "SNDK", "SKHY", "000660.KS", "005930.KS", "285A.T", "night:memory:XKRX"]
    # 铠侠在东京：东京只有它一只，不成篮子，单独触发
    assert u.night_basket_of("285A.T") is None and u.benchmark_for("285A.T") is None
    assert u.find("285A.T").related_to == ["MU", "SNDK", "SKHY"] and settings.rules_for("285A.T").rules is None
    # 三星和海力士组成首尔篮子：篮子按整体涨跌触发，成分只在相对篮子单独大动时推送
    assert u.benchmark_for("MU") == "SOXX" and u.benchmark_for("000660.KS") == "night:memory:XKRX"
    assert u.benchmark_for("night:memory:XKRX") is None and u.basket_members("night:memory:XKRX") == ["000660.KS", "005930.KS"]
    assert u.peers_for("MU") == ["SNDK", "SKHY", "WDC", "005930.KS", "285A.T"]
    assert u.underlying_map == {"000660.KS": "SKHY"}  # 夜间信号不算“同一家公司”
    assert {"NVDA", "MSFT", "WDC", "000660.KS"} <= set(u.event_symbols) and "COHR" not in u.event_symbols
    assert u.focus_text() == "存储芯片（DRAM / NAND / HBM）"
    assert list(u.sector_news(["MU"])) == ["SECTOR:memory"]
    text = describe_scope(settings)
    assert "主题「存储芯片」" in text and "备用主题（没有持仓引用，不生效）：光模块（optics）" in text


def test_adding_and_removing_a_holding_moves_the_whole_theme(settings):
    u = settings.universe
    u.watchlist.append(lite())
    assert [t.key for t in u.active_themes] == ["memory", "optics"]
    assert u.benchmark_for("LITE") == "basket:optics" and u.label("basket:optics") == "光模块篮子"
    assert u.basket_members("basket:optics") == ["COHR", "AAOI", "CIEN", "FN", "GLW", "VIAV"]
    assert u.peers_for("LITE") == ["COHR", "AAOI", "CIEN", "FN", "GLW", "VIAV"]  # 不混进存储芯片的持仓和同业
    assert "LITE" not in u.peers_for("MU")
    monitored = [i.symbol for i in u.monitored]
    assert {"300308.SZ", "300502.SZ", "300394.SZ"} <= set(monitored)
    assert u.find("300308.SZ").related_to == ["LITE"] and u.find("000660.KS").related_to == ["MU", "SNDK", "SKHY"]
    assert {"COHR", "NVDA", "ANET"} <= set(u.event_symbols)
    assert list(u.sector_news(["LITE"])) == ["SECTOR:optics"] and u.focus_text(["LITE"]).startswith("数据中心光模块")
    assert {"COHR", "AAOI", "CIEN", "FN", "GLW", "VIAV"} <= set(u.intraday_symbols) and "basket:optics" not in u.intraday_symbols
    q = NewsHub([], u).build_query(["LITE"], utc(2026, 10, 9, 13), utc(2026, 10, 9, 15))
    assert set(q.queries) == {"LITE", "SECTOR:optics"}
    # 卖掉 LITE：光模块主题整体失效（不取数据、不收集事件、不推送）
    u.watchlist = [i for i in u.watchlist if i.symbol != "LITE"]
    assert [t.key for t in u.active_themes] == ["memory"]
    assert u.find("300308.SZ") is None and "COHR" not in u.event_symbols and "COHR" not in u.intraday_symbols


def test_events_outside_scope_are_hidden_until_back_in_scope(settings):
    st = Storage(":memory:")
    now = utc(2026, 10, 21, 12)
    d = date(2026, 10, 29)
    for sym in ("WDC", "COHR"):
        st.upsert_event(Event(kind="earnings", symbol=sym, day=d, title=f"{sym} 财报", key=earnings_key(sym, d),
                              source="yfinance"), now)
    hub = EventHub(settings, [], st)
    assert [e.symbol for e in hub.upcoming(d, d)] == ["WDC"]
    settings.universe.watchlist.append(lite())
    assert sorted(e.symbol for e in hub.upcoming(d, d)) == ["COHR", "WDC"]


def test_theme_files_load_and_bad_references_are_rejected(tmp_path):
    (tmp_path / "settings.yaml").write_text("universe: {benchmark: SPY}\n", encoding="utf-8")
    (tmp_path / "assets.yaml").write_text("assets:\n  - {symbol: LITE, name: Lumentum, theme: optics}\n", encoding="utf-8")
    (tmp_path / "themes").mkdir()
    (tmp_path / "themes" / "optics.yaml").write_text(
        "name: 光模块\nbenchmark: basket\npeers: [COHR, AAOI]\novernight: {\"300308.SZ\": 中际旭创}\n", encoding="utf-8")
    s = load_settings(tmp_path / "settings.yaml", load_env=False)
    u = s.universe
    assert u.benchmark_for("LITE") == "basket:optics" and u.peers_for("LITE") == ["COHR", "AAOI"]
    assert u.find("300308.SZ").name == "中际旭创"
    # 引用不存在的主题
    (tmp_path / "assets.yaml").write_text("assets:\n  - {symbol: LITE, name: Lumentum, theme: optic}\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="config/themes/ 里没有这个主题"):
        load_settings(tmp_path / "settings.yaml", load_env=False)
    # 篮子至少要 2 只美股同业（海外同业不能和美股逐分钟对齐）
    (tmp_path / "assets.yaml").write_text("assets:\n  - {symbol: LITE, name: Lumentum, theme: optics}\n", encoding="utf-8")
    (tmp_path / "themes" / "optics.yaml").write_text("name: 光模块\nbenchmark: basket\npeers: [COHR, \"300308.SZ\"]\n",
                                                    encoding="utf-8")
    with pytest.raises(ValidationError, match="凑不成篮子"):
        load_settings(tmp_path / "settings.yaml", load_env=False)


def test_legacy_universe_fields_become_the_default_theme(tmp_path):
    (tmp_path / "settings.yaml").write_text(
        "universe:\n  focus: 测试领域\n  benchmark: SOXX\n  peers: [WDC]\n  sector_queries: {en: [chips]}\n"
        "  watchlist:\n    - {symbol: MU, name: 美光}\n", encoding="utf-8")
    s = load_settings(tmp_path / "settings.yaml", load_env=False)
    u = s.universe
    assert [t.key for t in u.active_themes] == ["default"] and u.benchmark_for("MU") == "SOXX"
    assert u.peers_for("MU") == ["WDC"] and u.sector_news(["MU"]) == {"SECTOR:default": {"en": ["chips"]}}
    assert u.focus_text() == "测试领域"


def test_china_and_taiwan_otc_codes_use_existing_calendars():
    assert exchange_of("300308.SZ") == "XSHG" and exchange_of("688498.SS") == "XSHG"
    assert exchange_of("6488.TWO") == "XTAI" and exchange_of("2330.TW") == "XTAI"
    assert not is_us_symbol("6488.TWO")
    seg = ExchangeClock("XSHG").segment_at(datetime(2026, 10, 22, 2, 0, tzinfo=UTC))  # 10:00 北京时间
    assert seg is not None and seg.kind == "exchange"


# ───────── 同业篮子 ─────────


def test_basket_levels_share_the_previous_close_of_100():
    days = [pd.Timestamp("2026-10-07"), pd.Timestamp("2026-10-08")]
    daily = {"A": pd.DataFrame({"close": [100.0, 110.0]}, index=days),
             "B": pd.DataFrame({"close": [50.0, 45.0]}, index=days)}
    idx = pd.DatetimeIndex([pd.Timestamp("2026-10-09 09:30", tz=ET), pd.Timestamp("2026-10-09 09:31", tz=ET)]).tz_convert(UTC)
    intra = {"A": pd.DataFrame({"close": [121.0, 132.0]}, index=idx),  # 较昨收 +10%、+20%
             "B": pd.DataFrame({"close": [45.0]}, index=idx[:1])}  # 较昨收 0%，第二分钟没有成交（当天内前向填充）
    m = basket_intraday(intra, daily, ["A", "B"])
    assert list(m["close"].round(6)) == [105.0, 110.0]
    d = basket_daily(daily, ["A", "B"], anchor=date(2026, 10, 9))
    assert prev_close_from_daily(d, date(2026, 10, 9)) == pytest.approx(100.0)  # 和分钟线同一口径
    assert d["close"].iloc[0] == pytest.approx(100.0 / (1 + (0.10 - 0.10) / 2))  # 10-08 的平均日收益 = (+10% − 10%)/2 = 0
    intra2, daily2 = add_baskets({"basket:t": ["A", "B"]}, intraday=intra, daily=daily, anchor=date(2026, 10, 9))
    assert "basket:t" in intra2 and "basket:t" in daily2 and "basket:t" not in intra  # 不改调用方的字典


async def test_monitor_splits_each_stock_against_its_own_theme_basket(settings, calendar):
    isolate(settings, ("MU",))
    th = settings.universe.themes["memory"]
    th.benchmark, th.peers = "basket", {"SNDK": "", "WDC": ""}
    settings.attribution.enabled = False
    settings.macro.enabled = False
    settings.llm.tasks = LLMTasks(brief=[], attribution=[], events=[], chat=[])
    shocks = {("MU", TARGET_DAY, 120 + i): -0.006 for i in range(5)}
    market = build_market(calendar, shocks=shocks)
    g = next(g for g in build_groups(settings, calendar) if g.key == "us")
    assert g.benchmarks == {"MU": "basket:memory"} and g.baskets == {"basket:memory": ["SNDK", "WDC"]}
    assert set(g.fetch) == {"MU", "SNDK", "WDC"}  # 篮子本身不去数据源取
    storage, notifier = Storage(":memory:"), ConsoleNotifier(echo=False)
    mon = IntradayMonitor(settings, calendar, FakePriceSource(market), storage, notifier,
                          Attributor(settings, NewsHub([], settings.universe), LLMClient(settings.llm)))
    alerts = await mon.tick(now=at(market.target, 125))
    a = next(a for a in alerts if a.symbol == "MU")
    assert a.benchmark == "basket:memory" and set(a.peers_move_pct) == {"SNDK", "WDC", "basket:memory"}
    mean_peers = (a.peers_move_pct["SNDK"] + a.peers_move_pct["WDC"]) / 2
    assert a.benchmark_move_pct == pytest.approx(mean_peers, abs=0.02)
    assert storage.alerts_for_day(TARGET_DAY.isoformat())[0].benchmark == "basket:memory"  # 入库：事后知道当时用的哪个基准
    body = render_alert(a, settings).body
    assert "存储芯片篮子" in body and "SPY" not in body


# ───────── 板块指标与基础宏观变量分开 ─────────


class FakeYF:
    def __init__(self, bars):
        self.bars, self.asked = bars, []

    async def intraday_bars(self, symbols, start, end=None, *, prepost=False):
        self.asked.append(list(symbols))
        return {s: self.bars[s] for s in symbols if s in self.bars}


def _frame(points: dict[str, float]) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(k, tz=ET) for k in points]).tz_convert(UTC)
    return pd.DataFrame({"close": list(points.values())}, index=idx)


async def test_theme_indicators_ride_along_but_stay_separate(settings, calendar):
    settings.macro.indicators = [MacroIndicator(key="spx", name="标普500", symbol="^GSPC", group="股指", kind="level")]
    settings.macro.treasury_curve = False
    yf = FakeYF({"^GSPC": _frame({"2026-10-08 15:59": 100.0, "2026-10-09 10:00": 101.0}),
                 "KRW=X": _frame({"2026-10-08 10:00": 1300.0, "2026-10-09 10:00": 1313.0}),
                 "^KS11": _frame({"2026-10-08 01:30": 3000.0, "2026-10-09 01:30": 3030.0})})
    snap = await MacroSource(settings, calendar, yf=yf).snapshot(at(calendar.session(TARGET_DAY), 60))
    assert yf.asked[-1] == ["^GSPC", "KRW=X", "^KS11"]  # 一次下载
    assert {q.key: q.theme for q in snap.quotes} == {"spx": None, "krw": "memory", "kospi": "memory"}
    assert "memory.krw" in snap.to_json() and "krw" not in snap.to_json()
    compact = "\n".join(render_macro_compact(snap, settings))
    assert "标普500" in compact and "韩元" not in compact  # 基础宏观变量里不混板块指标
    full = render_macro(snap, settings).body
    assert "**板块指标 · 存储芯片**" in full and "美元/韩元" in full
    from tickersentinel.models import Alert, Trigger

    a = Alert(symbol="MU", ts=utc(2026, 10, 9, 15), price=100.0, prev_close=103.0,
              triggers=[Trigger("level", -1, -3.0, level_pct=3)], window_minutes=None, move_pct=-3.0,
              benchmark_move_pct=None, beta=None, abnormal_move_pct=None, volume_ratio=None)
    body = render_alert(a, settings, macro=snap).body
    assert "📌 存储芯片：" in body and "KOSPI" in body
    lite_alert = Alert(**{**a.__dict__, "symbol": "LITE", "triggers": a.triggers})
    settings.universe.watchlist.append(lite())
    assert "📌 存储芯片" not in render_alert(lite_alert, settings, macro=snap).body  # LITE 只看光模块的板块指标


def test_attribution_prompt_uses_the_alerts_own_theme(settings):
    from tickersentinel.models import Alert, Trigger

    settings.universe.watchlist.append(lite())
    a = Alert(symbol="LITE", ts=utc(2026, 10, 9, 15), price=100.0, prev_close=103.0,
              triggers=[Trigger("level", -1, -3.0, level_pct=3)], window_minutes=None, move_pct=-3.0,
              benchmark_move_pct=-1.0, beta=1.2, abnormal_move_pct=-1.8, volume_ratio=None, benchmark="basket:optics")
    prompt = Attributor(settings, NewsHub([], settings.universe), LLMClient(settings.llm)).build_prompt(a, [])
    assert "板块：光模块" in prompt and "光模块篮子" in prompt and "optical transceiver" in prompt
    assert "DRAM" not in prompt
    from tickersentinel.llm.prompts import attribution_system

    assert "光模块" in attribution_system(settings, "LITE") and "存储" not in attribution_system(settings, "LITE").split("读者持有")[0]


# ───────── 夜间信号按篮子触发 ─────────


def _krx_market(shocks: dict[str, float]):
    """首尔两只（海力士、三星）：前 6 段随机游走，目标段第 60 分钟按 shocks 跳一下。日线按首尔当地交易日。"""
    from test_segments import make_bars

    krx = ExchangeClock("XKRX")
    target = krx.segment_at(datetime(2026, 10, 20, 1, 0, tzinfo=UTC))  # 10-20 首尔 10:00
    segs = [*krx.previous(target, 6), target]
    bars, daily = {}, {}
    for i, sym in enumerate(("000660.KS", "005930.KS")):
        df, _ = make_bars(segs, price=300000 - 100000 * i, sigma=0.0005, seed=11 + i,
                          shocks={(("exchange", target.day), 60): shocks.get(sym, 0.0)})
        bars[sym] = df
        daily[sym] = pd.DataFrame({"close": [float(df[df.index < g.close]["close"].iloc[-1]) for g in segs]},
                                  index=pd.DatetimeIndex([pd.Timestamp(g.day) for g in segs]))
    return target, bars, daily


async def test_overnight_signals_fire_once_as_a_basket(settings, calendar):
    from test_segments import SegPrices, monitor_for

    target, bars, daily = _krx_market({"000660.KS": 0.06, "005930.KS": 0.06})
    mon, _, notifier = monitor_for(settings, calendar, SegPrices(bars, daily), symbols=("000660.KS", "005930.KS", "SKHY"))
    g = next(g for g in mon.groups if g.key == "XKRX")
    assert set(g.symbols) == {"000660.KS", "005930.KS", "night:memory:XKRX"}
    assert g.benchmarks["000660.KS"] == "night:memory:XKRX" and "night:memory:XKRX" not in g.fetch
    alerts = await mon.tick(now=target.open + timedelta(minutes=65))
    assert [a.symbol for a in alerts] == ["night:memory:XKRX"]  # 两只一起涨：只推篮子这一条
    title, body = notifier.sent[0][1].title, notifier.sent[0][1].body
    assert title.startswith("🔺 存储芯片夜间信号（首尔篮子）")
    assert "篮子（SK海力士(首尔)、三星电子 等权）" in body and "SKHY 的夜间信号" in body
    assert "SK海力士(首尔)" in body.split("同期")[1] and "三星电子" in body.split("同期")[1]


async def test_a_member_moving_alone_is_pushed_separately(settings, calendar):
    from test_segments import SegPrices, monitor_for

    target, bars, daily = _krx_market({"005930.KS": 0.08})  # 只有三星大涨：篮子约 +4%，三星相对篮子约 +4%
    mon, _, notifier = monitor_for(settings, calendar, SegPrices(bars, daily), symbols=("000660.KS", "005930.KS", "SKHY"))
    alerts = await mon.tick(now=target.open + timedelta(minutes=65))
    by = {a.symbol: a for a in alerts}
    assert set(by) == {"night:memory:XKRX", "005930.KS"}
    assert [t.rule for t in by["005930.KS"].triggers] == ["idio"]  # 成分只跑“个股特异”
    assert by["005930.KS"].benchmark == "night:memory:XKRX"


def test_seoul_basket_is_split_by_local_trading_day_not_us_date():
    """首尔的交易时段跨过美东午夜（20:00–02:30 美东）：按美东日期划分交易日会把一天劈成两段、换两次昨收。"""
    from zoneinfo import ZoneInfo

    kst = ZoneInfo("Asia/Seoul")
    idx = pd.date_range(pd.Timestamp("2026-10-20 09:00", tz=kst), periods=390, freq="1min").tz_convert(UTC)
    days = pd.DatetimeIndex([pd.Timestamp("2026-10-16"), pd.Timestamp("2026-10-19")])
    daily = {s: pd.DataFrame({"close": [100.0, 110.0]}, index=days) for s in ("A.KS", "B.KS")}
    intra = {s: pd.DataFrame({"close": np.full(390, 110.0)}, index=idx) for s in ("A.KS", "B.KS")}
    good = basket_intraday(intra, daily, ["A.KS", "B.KS"], tz=kst)
    assert good["close"].round(6).unique().tolist() == [100.0]  # 全天平盘 = 昨收（100）
    bad = basket_intraday(intra, daily, ["A.KS", "B.KS"])  # 按美东日期：午夜前用了前天的收盘当昨收
    assert len(bad["close"].round(6).unique()) == 2


def test_scope_view_and_rules_for_baskets(settings):
    u = settings.universe
    assert settings.rules_for("000660.KS").rules == ["idio"]
    assert settings.rules_for("000660.KS").idiosyncratic.min_move_pct == 3.0
    assert settings.rules_for("night:memory:XKRX").rules is None
    text = describe_scope(settings)
    assert "按篮子触发：存储芯片夜间信号（首尔篮子） = SK海力士(首尔)、三星电子 等权" in text
    u.themes["memory"].overnight_basket = False  # 关掉：每只单独推
    assert "night:memory:XKRX" not in [i.symbol for i in u.monitored] and settings.rules_for("000660.KS").rules is None


def _tokyo_market(shocks: dict[int, float]):
    """东京一只（铠侠）：前 6 段随机游走，目标段按 shocks（距开盘的分钟 → 跳幅）跳一下；午休 11:30–12:30 没有 K 线。"""
    from test_segments import make_bars

    tse = ExchangeClock("XTKS")
    target = tse.segment_at(datetime(2026, 10, 22, 1, 0, tzinfo=UTC))  # 10-22 东京 10:00
    assert target.break_offsets == (150, 210)  # 11:30–12:30
    segs = [*tse.previous(target, 6), target]
    df, _ = make_bars(segs, price=5000.0, sigma=0.0006, seed=21,
                      shocks={(("exchange", target.day), k): v for k, v in shocks.items()})
    lunch = np.zeros(len(df), dtype=bool)
    for g in segs:
        lunch |= (df.index >= g.break_start) & (df.index < g.break_end)
    df = df[~lunch]
    daily = pd.DataFrame({"close": [float(df[df.index < g.close]["close"].iloc[-1]) for g in segs]},
                         index=pd.DatetimeIndex([pd.Timestamp(g.day) for g in segs]))
    return target, {"285A.T": df}, {"285A.T": daily}


async def test_lunch_break_gap_is_not_a_rapid_move(settings, calendar):
    """午休前后的价差不是“5 分钟急跌”：复盘后先预热，窗口不跨午休；跳空够大时由全天累计推。"""
    from test_segments import SegPrices, monitor_for

    settings.universe.themes["memory"].overnight["285A.T"].alert = None  # 用类别默认的警报标准（全天 5/8/12%）
    target, bars, daily = _tokyo_market({210: -0.06})  # 12:30 复盘就比上午收盘低 6%
    mon, _, _ = monitor_for(settings, calendar, SegPrices(bars, daily), symbols=("285A.T", "SKHY"))
    assert settings.rules_for("285A.T").day_pct == [5.0, 8.0, 12.0]
    assert [g.symbols for g in mon.groups if g.key == "XTKS"] == [["285A.T"]]
    rules = []
    for minutes in (211, 216, 225, 239):  # 复盘后 1、6、15、29 分钟
        alerts = await mon.tick(now=target.open + timedelta(minutes=minutes))
        rules += [t.rule for a in alerts for t in a.triggers]
    assert "day" in rules and "rapid" not in rules  # 跳空由全天累计推，不当成急跌
    # 下午盘中真的急跌（13:30 跌 3%）照样能抓到
    target, bars, daily = _tokyo_market({270: -0.03})
    mon, _, _ = monitor_for(settings, calendar, SegPrices(bars, daily), symbols=("285A.T", "SKHY"))
    alerts = await mon.tick(now=target.open + timedelta(minutes=272))
    assert [t.window_minutes for a in alerts for t in a.triggers if t.rule == "rapid"] == [5]


def test_sigma_ignores_the_lunch_break():
    """σ 用的 5 分钟收益不跨午休：午休前后的价差不算，午休里前向填充的 0 也不算。"""
    from tickersentinel.detect.baseline import five_min_returns, session_frame

    tse = ExchangeClock("XTKS")
    seg = tse.segment_at(datetime(2026, 10, 22, 1, 0, tzinfo=UTC))
    idx = [t for t in pd.date_range(seg.open, seg.close, freq="1min", inclusive="left")
           if not seg.break_start <= t < seg.break_end]
    px = np.where(np.array([t >= seg.break_end for t in idx]), 95.0, 100.0)  # 上午 100，下午 95：只有午休跳空
    r = five_min_returns(session_frame(pd.DataFrame({"close": px, "volume": 1.0}, index=pd.DatetimeIndex(idx)), seg))
    assert len(r) == 29 + 35 and float(np.abs(r).max()) == 0.0  # 上午 150 分钟、下午 180 分钟，各自 5 分钟一段
