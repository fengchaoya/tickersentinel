"""全天候监控：交易时段切段（美股盘前 / 盘中 / 盘后、CME 期货、海外交易所）、按资产的警报标准（config/assets.yaml）、
盘前盘后的错价过滤、参照价（盘后较今日收盘）、冷却 key。全部离线。"""

from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd
import pytest

from tickersentinel.agents.attribution import Attributor
from tickersentinel.agents.render import render_alert
from tickersentinel.config import AlertRules, Instrument, LLMTasks, load_settings, merge_assets
from tickersentinel.detect.clock import CmeClock, ExchangeClock, UsEquityClock, clock_key
from tickersentinel.detect.baseline import despike
from tickersentinel.jobs.monitor import IntradayMonitor, build_groups
from tickersentinel.jobs.replay import run_replay
from tickersentinel.llm.client import LLMClient
from tickersentinel.news.hub import NewsHub
from tickersentinel.notify.console import ConsoleNotifier
from tickersentinel.sources.base import PriceSource
from tickersentinel.storage import Storage
from tickersentinel.timeutil import ET, UTC

from conftest import FakeNewsSource, isolate


def et(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=ET).astimezone(UTC)


# ───────── 切段 ─────────


def test_us_equity_segments_keys_and_references(calendar):
    clock = UsEquityClock(calendar)
    pre, reg, post = clock.segments_on(date(2026, 10, 9))
    assert (pre.kind, reg.kind, post.kind) == ("pre", "regular", "post")
    assert pre.open == et(date(2026, 10, 9), 4) and pre.close == reg.open == et(date(2026, 10, 9), 9, 30)
    assert post.open == reg.close and post.close == et(date(2026, 10, 9), 20)
    assert pre.key == reg.key == "2026-10-09" and post.key == "2026-10-09:post"  # 盘前盘中同一个昨收、同一个冷却 key
    assert pre.ref_at == reg.ref_at == calendar.session(date(2026, 10, 8)).close and post.ref_at == reg.close
    assert clock.segment_at(et(date(2026, 10, 9), 3, 59)) is None and clock.segment_at(et(date(2026, 10, 9), 20)) is None
    assert clock.segment_at(et(date(2026, 10, 9), 17)).kind == "post"
    assert clock.segment_at(et(date(2026, 10, 10), 10)) is None  # 周六
    # 感恩节次日 13:00 收盘：盘后从 13:00 开始
    early = clock.segments_on(date(2026, 11, 27))
    assert early[1].close == et(date(2026, 11, 27), 13) and early[2].open == early[1].close
    assert [g.day for g in clock.previous(post, 3)] == [date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)]
    assert all(g.kind == "post" for g in clock.previous(post, 3))
    assert UsEquityClock(calendar, extended=False).segments_on(date(2026, 10, 9)) == [reg]


def test_cme_and_exchange_clocks():
    cme = CmeClock()
    sunday_night = et(date(2026, 10, 11), 19)
    seg = cme.segment_at(sunday_night)
    assert seg.day == date(2026, 10, 12) and seg.key == "cme:2026-10-12"  # 周日晚开盘的那段算周一的
    assert seg.ref_at == et(date(2026, 10, 9), 17)  # 周一的参照：上周五 17:00
    assert cme.segment_at(et(date(2026, 10, 9), 17, 30)) is None  # 每天 17:00–18:00 休息
    assert cme.segment_at(et(date(2026, 10, 10), 12)) is None  # 周六
    assert [g.day.weekday() for g in cme.previous(seg, 3)] == [2, 3, 4]  # 跳过周末
    krx = ExchangeClock("XKRX")
    s = krx.segment_at(et(date(2026, 10, 19), 21))  # 美东 21:00 = 首尔 10:00
    assert s is not None and s.key.startswith("XKRX:") and s.day == date(2026, 10, 20)
    assert clock_key("foreign_equity", "000660.KS") == "XKRX" and clock_key("future", "GC=F") == "cme"
    assert clock_key("us_equity", "MU") == "us" and clock_key("crypto", "BTC-USD") == "utc"


# ───────── 资产清单与警报标准 ─────────


def test_assets_file_rules_and_merge_errors(settings):
    u = settings.universe
    assert u.symbols == ["MU", "SNDK", "SKHY"] and "000660.KS" in [i.symbol for i in u.monitored]
    assert u.equity_symbols == ["MU", "SNDK", "SKHY"] and u.underlying_map["000660.KS"] == "SKHY"
    u.watchlist.append(Instrument(symbol="GC=F", name="黄金", asset_class="future",
                                  alert=AlertRules(rapid_pct={5: 0.5, 15: 0.8}, day_pct=[1.5, 2.5])))
    gold = settings.rules_for("GC=F")
    assert gold.windows_minutes == [5, 15] and gold.min_move_pct == {5: 0.5, 15: 0.8} and gold.day_pct == [1.5, 2.5]
    assert gold.z_threshold == settings.universe.rules("GC=F").z  # 类别默认
    assert settings.rules_for("MU").min_move_pct == {5: 1.5, 15: 2.5, 30: 3.0, 60: 3.0} and settings.hours_for("MU") == "extended"
    assert settings.rules_for("MU").day_pct == [5, 8, 12]
    assert AlertRules(levels_pct=[4, 7]).day_pct == [4, 7]  # 旧名照样能读（备份、旧配置）
    assert "GC=F" not in u.equity_symbols  # 期货没有财报和公告
    with pytest.raises(ValueError, match="写了两处"):
        merge_assets({"universe": {"watchlist": [{"symbol": "X", "name": "x"}]}}, {"assets": [{"symbol": "Y", "name": "y"}]})
    with pytest.raises(ValueError, match="只认 defaults 和 assets"):
        merge_assets({}, {"asset": []})
    with pytest.raises(ValueError):
        AlertRules(rapid_pct={5: -1})


# ───────── 检测 ─────────


class SegPrices(PriceSource):
    """按段生成分钟线：每段内随机游走，段与段之间价格连续；shocks[(段 key 或 kind+day, 第几分钟)] = 对数收益。"""

    name = "segfake"

    def __init__(self, bars: dict[str, pd.DataFrame], daily: dict[str, pd.DataFrame] | None = None):
        self.bars, self.daily = bars, daily or {}
        self.calls = []

    async def intraday_bars(self, symbols, start, end=None, *, prepost=False):
        self.calls.append((tuple(symbols), prepost))
        out = {}
        for s in symbols:
            df = self.bars.get(s)
            if df is None:
                continue
            mask = df.index >= start
            if end is not None:
                mask &= df.index < end
            out[s] = df[mask]
        return out

    async def daily_bars(self, symbols, lookback_days):
        return {s: self.daily[s] for s in symbols if s in self.daily}


def make_bars(segments, price=100.0, sigma=0.0003, shocks=None, seed=3, spikes=None):
    rng = np.random.default_rng(seed)
    frames, closes = [], {}
    for seg in segments:
        n = seg.minutes
        r = rng.normal(0, sigma, n)
        for (key, k), v in (shocks or {}).items():
            if key == (seg.kind, seg.day):
                r[k] += v
        px = price * np.exp(np.cumsum(r))
        for (key, k), v in (spikes or {}).items():  # 单笔错价：只影响一根 K 线
            if key == (seg.kind, seg.day):
                px[k] *= np.exp(v)
        idx = pd.date_range(seg.open, periods=n, freq="1min")
        frames.append(pd.DataFrame({"close": px, "volume": 0.0}, index=idx))
        price = float(px[-1]) if not spikes else float(price * np.exp(np.cumsum(r))[-1])
        if seg.kind == "regular":
            closes[pd.Timestamp(seg.day)] = price
    daily = pd.DataFrame({"close": list(closes.values())}, index=pd.DatetimeIndex(list(closes)))
    return pd.concat(frames).sort_index(), daily


def us_segments(calendar, day, n_prior=6):
    clock = UsEquityClock(calendar)
    days = [*calendar.sessions_before(day, n_prior), calendar.session(day)]
    return [g for s in days for g in clock.segments_on(s.day)]


def monitor_for(settings, calendar, prices, symbols=("MU",)):
    settings.attribution.enabled = False
    settings.macro.enabled = False
    settings.llm.tasks = LLMTasks(brief=[], attribution=[], events=[], chat=[])
    isolate(settings, symbols)
    storage, notifier = Storage(":memory:"), ConsoleNotifier(echo=False)
    mon = IntradayMonitor(settings, calendar, prices, storage, notifier,
                          Attributor(settings, NewsHub([FakeNewsSource([])], settings.universe), LLMClient(settings.llm)))
    return mon, storage, notifier


async def test_premarket_gap_then_no_repeat_at_open(settings, calendar):
    day = date(2026, 10, 9)
    segs = us_segments(calendar, day)
    mu, mu_d = make_bars(segs, shocks={(("pre", day), 30): 0.06})  # 盘前 04:30 跳涨 6%
    soxx, soxx_d = make_bars(segs, price=250, seed=4)
    mon, storage, notifier = monitor_for(settings, calendar, SegPrices({"MU": mu, "SOXX": soxx}, {"MU": mu_d, "SOXX": soxx_d}))
    alerts = await mon.tick(now=et(day, 4, 40))
    assert [(a.symbol, a.segment) for a in alerts] == [("MU", "pre")]
    assert ("day", 5.0) in {(t.rule, t.level_pct) for t in alerts[0].triggers}  # 盘前跳空：急涨 + 全天累计一起推
    msg = notifier.sent[0][1]
    assert msg.title.startswith("🔺 MU【盘前】") and "全天：" in msg.body
    assert storage.alerts_for_day("2026-10-09")[0].segment == "pre"
    # 开盘后还是 +6% 左右：盘中段带上盘前的高低点，起点和盘前是同一个，推过的 5% 这一档不再推
    assert await mon.tick(now=et(day, 9, 45)) == []
    assert ("MU", "SOXX") in [c[0] for c in mon.prices.calls] and all(c[1] for c in mon.prices.calls)  # 带盘前盘后


async def test_open_does_not_repeat_a_day_step_from_a_sparse_premarket(settings, calendar):
    """盘前成交稀疏、冲高 3% 后回落，推过全天 5% 一档：开盘后的起点（盘前高点）和盘前推送时是同一个，不再重推。"""
    day = date(2026, 10, 9)
    segs = us_segments(calendar, day)
    mu, mu_d = make_bars(segs)
    prev = float(mu_d["close"][mu_d.index < pd.Timestamp(day)].iloc[-1])
    pre = next(g for g in segs if g.kind == "pre" and g.day == day)
    reg = next(g for g in segs if g.kind == "regular" and g.day == day)
    idx = pd.date_range(pre.open, pre.close, freq="1min", inclusive="left")
    pts = {0: 1.0, 5: 1.0, 10: 1.0, 20: 1.03, 40: 1.03, 41: 1.025, 60: 1.02, 90: 0.99, 100: 0.975, 101: 0.975, 102: 0.975}
    pm = pd.DataFrame({"close": [prev * pts[k] for k in sorted(pts)], "volume": 0.0}, index=[idx[k] for k in sorted(pts)])
    rg = mu[(mu.index >= reg.open) & (mu.index < reg.close)].copy()
    rg["close"] = prev * 0.975 * rg["close"] / rg["close"].iloc[0]
    bars = pd.concat([mu[(mu.index < pre.open) | (mu.index >= reg.close)], pm, rg]).sort_index()
    mon, _, _ = monitor_for(settings, calendar, SegPrices({"MU": bars}, {"MU": mu_d}))
    pre_alerts = await mon.tick(now=et(day, 5, 50))
    assert ("day", 5.0) in {(t.rule, t.level_pct) for a in pre_alerts for t in a.triggers}
    assert not [t for a in await mon.tick(now=et(day, 9, 45)) for t in a.triggers if t.rule == "day"]


async def test_postmarket_measures_against_today_close_and_despikes(settings, calendar):
    day = date(2026, 10, 9)
    segs = us_segments(calendar, day)
    # 盘中涨 6%（不关盘后的事），盘后 16:30 一笔错价 -4%（只一根 K 线），16:50 真的跌 5%
    mu, mu_d = make_bars(segs, shocks={(("regular", day), 100): 0.06, (("post", day), 50): -0.05},
                         spikes={(("post", day), 30): -0.04})
    mon, storage, notifier = monitor_for(settings, calendar, SegPrices({"MU": mu}, {"MU": mu_d}))
    assert await mon.tick(now=et(day, 16, 32)) == []  # 一根错价过不了中位数
    alerts = await mon.tick(now=et(day, 16, 55))
    assert alerts and alerts[0].segment == "post"
    assert alerts[0].prev_close == pytest.approx(float(mu_d["close"].iloc[-1]))  # 参照 = 今日收盘
    body = notifier.sent[-1][1].body
    assert "较今日收盘" in body and "【盘后】" in notifier.sent[-1][1].title
    assert storage.alerts_for_day("2026-10-09:post")


def test_despike_ignores_single_print():
    v = pd.DataFrame({"close": [100.0, 100.1, 96.0, 100.2, 100.1], "volume": 0.0})
    assert despike(v)["close"].tolist()[2:] == [100.0, 100.1, 100.1]


async def test_regular_hours_asset_skipped_outside_session(settings, calendar):
    day = date(2026, 10, 9)
    segs = us_segments(calendar, day)
    mu, mu_d = make_bars(segs, shocks={(("pre", day), 30): 0.06})
    settings.universe.classes["us_equity"] = AlertRules(hours="regular")
    mon, _, _ = monitor_for(settings, calendar, SegPrices({"MU": mu}, {"MU": mu_d}))
    assert await mon.tick(now=et(day, 4, 40)) == []
    assert mon.describe()[0].endswith("常规时段")


async def test_gold_future_uses_its_own_thresholds(settings, calendar):
    clock = CmeClock()
    target = clock.segment_for(date(2026, 10, 9))
    segs = [*clock.previous(target, 6), target]
    gold, _ = make_bars(segs, price=4000, sigma=0.0001, shocks={(("cme", date(2026, 10, 9)), 600): 0.02})
    settings.universe.watchlist.append(Instrument(symbol="GC=F", name="黄金", asset_class="future",
                                                  alert=AlertRules(day_pct=[1.5, 2.5])))
    mon, storage, notifier = monitor_for(settings, calendar, SegPrices({"GC=F": gold}), symbols=("GC=F",))
    assert [g.key for g in build_groups(settings, calendar)] == ["cme"]
    alerts = await mon.tick(now=target.open + timedelta(minutes=605))
    assert alerts and alerts[0].segment == "cme"
    assert ("day", 1.5) in {(t.rule, t.level_pct) for t in alerts[0].triggers}  # 黄金自己的 1.5%（个股是 5%）
    assert "较上一交易时段" in notifier.sent[0][1].body and "【期货】" in notifier.sent[0][1].title
    assert storage.alerts_for_day(target.key)


async def test_night_signal_from_seoul_names_its_holding(settings, calendar):
    krx = ExchangeClock("XKRX")
    target = krx.segment_at(et(date(2026, 10, 19), 21))
    segs = [*krx.previous(target, 6), target]
    hynix, hynix_d = make_bars(segs, price=300000, sigma=0.0005, shocks={(("exchange", target.day), 60): 0.06})
    hynix_d = pd.DataFrame({"close": [float(hynix[hynix.index < g.close]["close"].iloc[-1]) for g in segs]},
                           index=pd.DatetimeIndex([pd.Timestamp(g.day) for g in segs]))
    mon, _, notifier = monitor_for(settings, calendar, SegPrices({"000660.KS": hynix}, {"000660.KS": hynix_d}),
                                   symbols=("000660.KS", "SKHY"))
    alerts = await mon.tick(now=target.open + timedelta(minutes=65))
    assert [a.symbol for a in alerts] == ["000660.KS"]
    msg = notifier.sent[0][1]
    assert "【SK海力士(首尔)】" in msg.title and "SKHY 的夜间信号" in msg.body
    assert "SKHY" in Attributor(settings, NewsHub([], settings.universe), LLMClient(settings.llm)).build_prompt(alerts[0], [])


async def test_replay_covers_all_segments(settings, calendar):
    day = date(2026, 10, 9)
    segs = us_segments(calendar, day, n_prior=8)
    mu, mu_d = make_bars(segs, shocks={(("pre", day), 30): 0.06, (("post", day), 60): -0.06})
    isolate(settings, ("MU",))
    res = await run_replay(settings, calendar, SegPrices({"MU": mu}, {"MU": mu_d}), days=2,
                           now=et(day, 21))
    assert {g.kind for g in res.segments} == {"pre", "regular", "post"}
    assert [a.segment for _, a in res.alerts] == ["pre", "post"]
    assert "盘前 1 条" in res.report(settings) and "[盘后]" in res.report(settings)


def test_render_marks_segment(settings):
    from tickersentinel.models import Alert, Trigger

    a = Alert(symbol="MU", ts=datetime(2026, 10, 9, 21, 0, tzinfo=UTC), price=100.0, prev_close=95.0,
              triggers=[Trigger("level", 1, 5.3, level_pct=5)], window_minutes=None, move_pct=5.3,
              benchmark_move_pct=None, beta=None, abnormal_move_pct=None, volume_ratio=None, segment="post")
    msg = render_alert(a, settings)
    assert msg.title.startswith("🔺 MU【盘后】") and "突破较今日收盘 +5% 关口" in msg.body and "较今日收盘 +5.3%" in msg.body
    a.segment = "regular"
    assert "【" not in render_alert(a, settings).title


def test_load_settings_reads_assets_yaml(tmp_path):
    (tmp_path / "settings.yaml").write_text("universe: {benchmark: SPY}\n", encoding="utf-8")
    (tmp_path / "assets.yaml").write_text(
        "defaults: {us_equity: {levels_pct: [4, 7]}}\nassets:\n  - {symbol: AAPL, name: 苹果}\n"
        "  - {symbol: GC=F, name: 黄金, class: future, alert: {levels_pct: [1]}}\n", encoding="utf-8")
    s = load_settings(tmp_path / "settings.yaml", load_env=False)
    assert s.universe.symbols == ["AAPL", "GC=F"] and s.rules_for("AAPL").day_pct == [4.0, 7.0]  # 旧名 levels_pct
    assert s.rules_for("GC=F").day_pct == [1.0] and s.rules_for("GC=F").min_move_pct == {5: 0.6, 15: 1.0, 30: 1.4, 60: 1.4}
