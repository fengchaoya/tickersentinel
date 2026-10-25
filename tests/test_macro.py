"""宏观快照：涨跌口径（较昨收 / 较美股收盘 / 24h / 近 1 小时）、财政部收益率曲线、附在异动推送里并入库、/macro 命令。全部离线。"""

import asyncio
import json
import sqlite3
from datetime import date, datetime, timedelta

import httpx
import pandas as pd
import pytest

from tickersentinel.agents.attribution import Attributor
from tickersentinel.agents.chat import ChatAgent, IncomingMessage
from tickersentinel.agents.render import render_alert, render_macro
from tickersentinel.config import LLMTasks, MacroIndicator
from tickersentinel.jobs.monitor import IntradayMonitor
from tickersentinel.llm.client import LLMClient
from tickersentinel.news.hub import NewsHub
from tickersentinel.notify.console import ConsoleNotifier
from tickersentinel.sources.base import SourceError
from tickersentinel.sources.macro import MacroSource, build_quote, parse_treasury_csv
from tickersentinel.storage import Storage
from tickersentinel.timeutil import ET, UTC

from conftest import TARGET_DAY, FakePriceSource, at, build_market

TREASURY_CSV = ('Date,"1 Mo","2 Yr","5 Yr","10 Yr","20 Yr","30 Yr"\n'
                "10/20/2026,4.10,4.79,4.94,5.12,5.47,5.42\n10/19/2026,4.11,4.77,4.94,5.15,5.53,5.48\n")


def series(points: dict[str, float]) -> pd.Series:
    """{'2026-10-08 15:59': 100, ...}（美东时间）→ UTC 索引的分钟收盘价。"""
    idx = pd.DatetimeIndex([pd.Timestamp(k, tz=ET) for k in points]).tz_convert(UTC)
    return pd.Series(list(points.values()), index=idx)


def ind(**kw) -> MacroIndicator:
    base = dict(key="x", name="X", symbol="X", group="股指", kind="level", ref="close")
    return MacroIndicator(**{**base, **kw})


def test_close_reference_is_previous_trading_day_last_bar(calendar):
    # 10-09 周五盘中：较 10-08 最后一根（16:15 的 VIX 收盘也算在内，不截在 16:00）；近 1 小时
    close = series({"2026-10-08 15:59": 20.0, "2026-10-08 16:14": 21.0, "2026-10-09 09:00": 22.0, "2026-10-09 10:00": 23.1})
    q = build_quote(ind(), close, calendar, symbol="^VIX", off_hours=False)
    assert q.change_label == "较昨收" and q.change == pytest.approx(10.0)  # 23.1 / 21.0
    assert q.change_1h == pytest.approx(5.0)  # 23.1 / 22.0
    # 收益率：变化用 bp
    y = build_quote(ind(kind="yield"), series({"2026-10-08 15:00": 5.10, "2026-10-09 10:00": 5.135}), calendar,
                    symbol="^TNX", off_hours=False)
    assert y.change == pytest.approx(3.5) and y.fmt_change(y.change) == "+4bp" and y.fmt_value() == "5.13%"
    assert y.change_1h is None  # 一小时前没在交易：不给


def test_off_hours_futures_measure_since_us_close_and_24h_mode(calendar):
    # 周五 16:00 收盘后、周六：股指期货较“美股收盘那一刻”的期货价
    fut = series({"2026-10-09 15:59": 7000.0, "2026-10-09 16:59": 7070.0})
    q = build_quote(ind(off_hours_name="标普期货"), fut, calendar, symbol="ES=F", off_hours=True)
    assert q.name == "标普期货" and q.change_label == "较美股收盘" and q.change == pytest.approx(1.0) and q.off_hours
    # 24h：周一早上的“24 小时前”落在周日，往回找到周五最后一根
    oil = series({"2026-10-09 16:59": 100.0, "2026-10-12 08:00": 101.0, "2026-10-12 09:00": 98.98})
    o = build_quote(ind(kind="price", ref="24h"), oil, calendar, symbol="CL=F", off_hours=False)
    assert o.change_label == "24h" and o.change == pytest.approx(-1.02) and o.fmt_value() == "$98.98"
    assert o.change_1h == pytest.approx(-2.0)


def test_parse_treasury_csv():
    c = parse_treasury_csv(TREASURY_CSV)
    assert c.day == date(2026, 10, 20) and c.yields == {"2Y": 4.79, "5Y": 4.94, "10Y": 5.12, "30Y": 5.42}
    assert c.spread_2s10s_bp == pytest.approx(33.0)
    assert parse_treasury_csv("") is None and parse_treasury_csv("Date\n") is None


class FakeYF:
    def __init__(self, bars=None, error=None):
        self.bars, self.error, self.asked = bars or {}, error, []

    async def intraday_bars(self, symbols, start, end=None, *, prepost=False):
        self.asked.append(list(symbols))
        if self.error:
            raise self.error
        return {s: self.bars[s] for s in symbols if s in self.bars}


def frame(points):
    return pd.DataFrame({"close": series(points)})


def treasury_http(text=TREASURY_CSV, status=200):
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, text=text)))


async def test_macro_source_snapshot_switches_to_futures_off_hours(settings, calendar):
    for th in settings.universe.themes.values():  # 这里只测基础宏观变量；板块指标另有测试
        th.indicators = []
    settings.macro.indicators = [
        ind(key="spx", name="标普500", symbol="^GSPC", off_hours_symbol="ES=F", off_hours_name="标普期货"),
        ind(key="us10y", name="美债10年", symbol="^TNX", kind="yield", group="利率"),
    ]
    yf = FakeYF({"^GSPC": frame({"2026-10-08 15:59": 100.0, "2026-10-09 10:00": 101.0}),
                 "ES=F": frame({"2026-10-09 15:59": 101.0, "2026-10-10 09:00": 100.0}),
                 "^TNX": frame({"2026-10-08 15:00": 5.0, "2026-10-09 10:00": 5.02})})
    src = MacroSource(settings, calendar, yf=yf, http=treasury_http())
    s = calendar.session(TARGET_DAY)
    snap = await src.snapshot(at(s, 60))
    assert yf.asked[-1] == ["^GSPC", "^TNX"] and snap.get("spx").change == pytest.approx(1.0)
    assert snap.curve.yields["10Y"] == 5.12 and snap.errors == {}
    assert (await src.snapshot(at(s, 61))) is snap  # 60 秒缓存
    weekend = await src.snapshot(datetime(2026, 10, 10, 14, 0, tzinfo=UTC), use_cache=False)
    assert yf.asked[-1] == ["ES=F", "^TNX"] and weekend.get("spx").name == "标普期货"
    assert "较美股收盘" in render_macro(weekend, settings).body

    partial = await MacroSource(settings, calendar, yf=FakeYF(error=SourceError("限流")),
                                http=treasury_http()).snapshot(at(s, 60))
    assert partial.quotes == [] and partial.curve is not None and "yfinance" in partial.errors  # 只剩官方曲线也给
    with pytest.raises(SourceError, match="全部取不到"):
        await MacroSource(settings, calendar, yf=FakeYF(error=SourceError("限流")),
                          http=treasury_http(status=503)).snapshot(at(s, 60))


class FakeMacro:
    def __init__(self, snap=None, delay=0.0):
        self.snap, self.delay, self.calls = snap, delay, 0

    async def snapshot(self, now=None, *, use_cache=True):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.snap


async def make_snap(settings, calendar, now):
    settings.macro.indicators = [ind(key="us10y", name="美债10年", symbol="^TNX", kind="yield", group="利率"),
                                 ind(key="gold", name="黄金", symbol="GC=F", kind="price", ref="24h", group="大宗商品",
                                     in_alert=False)]
    yf = FakeYF({"^TNX": frame({"2026-10-08 15:00": 5.0, "2026-10-09 10:00": 5.08}),
                 "GC=F": frame({"2026-10-08 10:00": 4000.0, "2026-10-09 10:00": 4040.0})})
    return await MacroSource(settings, calendar, yf=yf, http=treasury_http()).snapshot(now)


async def test_alert_carries_macro_snapshot_and_stores_it(settings, calendar):
    settings.monitor.price.day_pct = [8.0, 12.0]
    settings.llm.tasks = LLMTasks(brief=[], attribution=[], events=[], chat=[])
    m = build_market(calendar, shocks={("MU", TARGET_DAY, 120 + i): -0.006 for i in range(5)})
    now = at(m.target, 125)
    snap = await make_snap(settings, calendar, now)
    macro = FakeMacro(snap)
    storage, notifier = Storage(":memory:"), ConsoleNotifier(echo=False)
    mon = IntradayMonitor(settings, calendar, FakePriceSource(m), storage, notifier,
                          Attributor(settings, NewsHub([], settings.universe), LLMClient(settings.llm)), macro=macro)
    [alert] = await mon.tick(now=now)
    await mon.drain()
    body = notifier.sent[0][1].body
    assert "🌐 **宏观**" in body and "美债10年 5.08%（+8bp）" in body and "黄金" not in body  # in_alert=False 不附
    assert "财政部官方收盘（10-20）" in body and macro.calls == 1
    row = storage.conn.execute("SELECT macro_json FROM alerts WHERE id = ?", (alert.id,)).fetchone()
    stored = json.loads(row[0])
    assert stored["us10y"]["value"] == 5.08 and stored["us10y"]["change"] == pytest.approx(8.0)
    assert stored["treasury"]["2Y"] == 4.79
    prompt = mon.attributor.build_prompt(alert, [], snap)
    assert "【宏观背景】" in prompt and "美债10年 5.08%" in prompt


async def test_slow_macro_does_not_block_alert(settings, calendar):
    settings.monitor.price.day_pct = [8.0, 12.0]
    settings.attribution.enabled = False
    settings.macro.timeout_seconds = 0.01
    m = build_market(calendar, shocks={("MU", TARGET_DAY, 120 + i): -0.006 for i in range(5)})
    storage, notifier = Storage(":memory:"), ConsoleNotifier(echo=False)
    mon = IntradayMonitor(settings, calendar, FakePriceSource(m), storage, notifier,
                          Attributor(settings, NewsHub([], settings.universe), LLMClient(settings.llm)),
                          macro=FakeMacro(None, delay=1))
    [alert] = await mon.tick(now=at(m.target, 125))
    assert "宏观" not in notifier.sent[0][1].body and alert.macro is None  # 超时就不附，异动照发


async def test_macro_command_replies_without_llm(settings, calendar):
    now = at(calendar.session(TARGET_DAY), 60)
    snap = await make_snap(settings, calendar, now)
    settings.llm.tasks = LLMTasks(brief=[], attribution=[], events=[], chat=[])
    agent = ChatAgent(settings, LLMClient(settings.llm), Storage(":memory:"), macro=FakeMacro(snap))
    for cmd in ("/macro", "宏观", "/M"):
        reply = await agent.handle(IncomingMessage(text=cmd, chat_id="oc_x"))
        assert reply.startswith("**🌐 宏观快照") and "美债10年 5.08%｜较昨收 +8bp" in reply and "黄金 $4,040｜24h +1.0%" in reply
    off = ChatAgent(settings, LLMClient(settings.llm), Storage(":memory:"))
    assert "没有开启" in await off.handle(IncomingMessage(text="/macro", chat_id="oc_x"))


def test_render_alert_without_macro_unchanged(settings, calendar):
    from tickersentinel.models import Alert, Trigger

    a = Alert(symbol="MU", ts=datetime(2026, 10, 9, 15, 0, tzinfo=UTC), price=100.0, prev_close=103.0,
              triggers=[Trigger("level", -1, -2.9, level_pct=3)], window_minutes=None, move_pct=-2.9,
              benchmark_move_pct=None, beta=None, abnormal_move_pct=None, volume_ratio=None)
    assert "宏观" not in render_alert(a, settings).body


def test_old_database_gets_macro_column(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, symbol TEXT NOT NULL,"
                 " ts TEXT NOT NULL, session_day TEXT NOT NULL, rule TEXT NOT NULL, direction INTEGER NOT NULL, price REAL,"
                 " prev_close REAL, window_minutes INTEGER, move_pct REAL, benchmark_move_pct REAL, beta REAL,"
                 " abnormal_move_pct REAL, volume_ratio REAL, peers_json TEXT, triggers_json TEXT NOT NULL,"
                 " is_escalation INTEGER NOT NULL DEFAULT 0, message_id TEXT, attribution_text TEXT, attribution_json TEXT,"
                 " label TEXT, notes TEXT)")
    conn.commit()
    conn.close()
    st = Storage(path)
    assert "macro_json" in {r[1] for r in st.conn.execute("PRAGMA table_info(alerts)")}
    st.close()
