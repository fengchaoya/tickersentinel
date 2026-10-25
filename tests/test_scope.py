"""监控范围的发现与修改（tsen scope / 飞书 /scope）：打分、出方案、写入 / 撤销 / 移除。全部离线（Yahoo、大模型、行情都是假的）。"""

import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tickersentinel.config import LLMTasks, load_settings
from tickersentinel.llm.base import ChatProvider, LLMResult
from tickersentinel.llm.client import LLMClient
from tickersentinel.scope import discover
from tickersentinel.scope.discover import (
    Proposal,
    ScopeError,
    overnight_signal,
    propose,
    render_proposal,
    residual_corr,
    short_name,
    suggest_day,
    suggest_rapid,
)
from tickersentinel.scope.edit import ScopeEditError, ScopeEditor, describe_change_effects, settings_after
from tickersentinel.sources.base import PriceSource
from tickersentinel.sources.peer_candidates import Profile

from conftest import TEST_CONFIG

N = 260


def synthetic_daily(seed: int = 3) -> dict[str, pd.DataFrame]:
    """单因子市场 + 板块因子 + 隔夜消息：
    目标 NEWCO 与 PEER1/2/3 共享板块因子 f；NOISE 只跟大盘；FOREIGN（亚洲，同一日期先收盘）反映隔夜消息 o，
    NEWCO 第二天开盘跳空也反映 o；ETF 都只跟大盘（所以同业篮子比 ETF 更像板块基准）。"""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2025-10-01", periods=N)
    m, f, o = rng.normal(0, 0.01, N), rng.normal(0, 0.015, N), rng.normal(0, 0.012, N)
    out: dict[str, pd.DataFrame] = {}

    def frame(r_close, gap=None):
        close = 100 * np.exp(np.cumsum(r_close))
        if gap is None:
            opn = close * np.exp(-rng.normal(0, 0.002, N))
        else:
            prev = np.concatenate([[100.0], close[:-1]])
            opn = prev * np.exp(gap)
        return pd.DataFrame({"open": opn, "close": close, "adj_close": close}, index=days)

    gap = 0.9 * o + rng.normal(0, 0.003, N)
    intraday = m + f + rng.normal(0, 0.008, N)
    close_r = gap + intraday
    out["NEWCO"] = frame(close_r, gap=gap)
    for i, s in enumerate(["PEER1", "PEER2", "PEER3"]):
        out[s] = frame(m + f + rng.normal(0, 0.008, N))
    out["NOISE"] = frame(m + rng.normal(0, 0.012, N))
    out["BIGCUST"] = frame(m + 0.1 * f + rng.normal(0, 0.015, N))  # 大客户：日常不同涨同跌
    out["FOREIGN.KS"] = frame(o + 0.3 * f + rng.normal(0, 0.006, N))
    out["STALE.T"] = frame(rng.normal(0, 0.01, N))  # 和目标没有关系的海外股票
    for etf in discover.BENCH_CANDIDATES:
        out[etf] = frame(m + rng.normal(0, 0.003, N))
    return out


class FakePrices(PriceSource):
    name = "fake"

    def __init__(self, daily):
        self.daily = daily

    async def intraday_bars(self, symbols, start, end=None, *, prepost=False):
        return {}

    async def daily_bars(self, symbols, lookback_days):
        return {s: self.daily[s] for s in symbols if s in self.daily}


class FakeProvider(ChatProvider):
    def __init__(self, text):
        super().__init__("fake", "fake-model")
        self.text, self.calls = text, []

    async def complete(self, **kw):
        self.calls.append(kw)
        return LLMResult(text=self.text, model=self.model)


LLM_JSON = {
    "theme_key": "widgets", "theme_name": "小部件", "focus": "测试用的板块",
    "company_queries": {"en": ["NewCo Inc"], "zh": ["新公司"]},
    "us_peers": [{"symbol": "PEER1", "name": "同业一", "why": "主营相同"}, {"symbol": "PEER2", "name": "同业二", "why": "x"},
                 {"symbol": "BIGCUST", "name": "大客户", "why": "最大客户"}],
    "overseas": [{"symbol": "FOREIGN.KS", "name": "海外同行", "why": "同一产业链"},
                 {"symbol": "STALE.T", "name": "瑞萨电子", "why": "x"}, {"symbol": "FAKE.SZ", "name": "编的", "why": "x"}],
    "catalysts": [{"symbol": "MSFT", "name": "微软", "why": "云厂商"}],
    "news": {"en": ["widget prices"], "zh": ["小部件 涨价"]},
    "indicators": [{"symbol": "KRW=X", "name": "美元/韩元", "why": "x"}, {"symbol": "SOXX", "name": "ETF", "why": "不该收"}],
}


def fake_llm(settings, payload=None):
    cfg = settings.llm.model_copy(update={"tasks": LLMTasks(brief=[], attribution=[], events=[], chat=[], scope=["fake"])})
    return LLMClient(cfg, providers={"fake": FakeProvider(json.dumps(payload or LLM_JSON, ensure_ascii=False))})


@pytest.fixture
def yahoo(monkeypatch):
    names = {"NEWCO": "NewCo Holdings Inc.", "PEER1": "Peer One Corp.", "PEER2": "Peer Two", "PEER3": "Peer Three",
             "NOISE": "Noise Co", "BIGCUST": "Big Customer", "MSFT": "Microsoft", "FOREIGN.KS": "Foreign Peer",
             "STALE.T": "Systena", "KRW=X": "USD/KRW", "SOXX": "iShares Semiconductor ETF"}

    async def yahoo_names(symbols, timeout=15.0):
        return {s: Profile(s, names[s], quote_type="ETF" if s in ("SOXX",) else "EQUITY") for s in symbols if s in names}

    async def yahoo_profile(symbol):
        return Profile(symbol, names.get(symbol, symbol), industry="Widgets", industry_key="widgets")

    async def yahoo_similar(symbol, timeout=15.0):
        return ["PEER3", "NOISE"]

    async def yahoo_industry_top(key, n=12):
        return ["NOISE"]

    monkeypatch.setattr(discover, "yahoo_names", yahoo_names)
    monkeypatch.setattr(discover, "yahoo_profile", yahoo_profile)
    monkeypatch.setattr(discover, "yahoo_similar", yahoo_similar)
    monkeypatch.setattr(discover, "yahoo_industry_top", yahoo_industry_top)
    return names


# ───────── 打分 ─────────


def test_scores_separate_comovers_overnight_signals_and_noise():
    daily = synthetic_daily()
    r = discover.log_returns(daily)
    c_peer, n = residual_corr(r, "NEWCO", "PEER1", market="SPY")
    c_noise, _ = residual_corr(r, "NEWCO", "NOISE", market="SPY")
    assert c_peer > 0.5 and abs(c_noise) < 0.2 and n == 120
    pc, follow, n = overnight_signal(daily, "NEWCO", "FOREIGN.KS")
    assert pc > 0.6 and n == 160
    assert abs(overnight_signal(daily, "NEWCO", "STALE.T")[0]) < 0.2
    # 阈值按日波动换算（默认值针对 5%）：铠侠 6.2% 放宽，日波动 2.5% 的减半；和默认差不到 10% 的照用默认
    base_rapid, base_day = {5: 1.5, 15: 2.5, 30: 3.0, 60: 3.0}, [5.0, 8.0, 12.0]
    assert suggest_rapid(6.2, base_rapid) == {5: 1.9, 15: 3.0, 30: 3.5, 60: 3.5} and suggest_day(6.2, base_day) == [6.0, 10.0, 15.0]
    assert suggest_rapid(2.5, base_rapid) == {5: 0.8, 15: 1.2, 30: 1.5, 60: 1.5} and suggest_day(2.5, base_day) == [2.5, 4.0, 6.0]
    assert suggest_rapid(5.2, base_rapid) == base_rapid and suggest_day(5.2, base_day) == base_day
    assert suggest_rapid(None, {5: 1.5}) == {5: 1.5} and suggest_day(None, [3]) == [3]
    assert short_name("Lumentum Holdings Inc.") == "Lumentum" and short_name("Zhongji Innolight Co., Ltd.") == "Zhongji Innolight"


async def test_propose_new_theme_picks_each_kind_of_linkage(settings, yahoo):
    prop = await propose(settings, "newco", FakePrices(synthetic_daily()), llm=fake_llm(settings))
    assert prop.symbol == "NEWCO" and prop.new_theme and prop.theme_key == "widgets" and prop.theme_name == "小部件"
    assert {p.symbol for p in prop.chosen("peers")} == {"PEER1", "PEER2", "PEER3"}  # Yahoo 相似里的 PEER3 也靠数据入选
    assert prop.benchmark == "basket" and prop.bench_scores["basket"] - max(
        v for k, v in prop.bench_scores.items() if k != "basket") >= discover.BASKET_GAIN
    assert [p.symbol for p in prop.chosen("overnight")] == ["FOREIGN.KS"]
    cats = {p.symbol for p in prop.chosen("catalysts")}
    assert cats == {"MSFT", "BIGCUST"}  # 大模型说是同业、日常却不联动的大客户 → 相关公司
    assert "NOISE" in {p.symbol for p in prop.rejected} and "BIGCUST" not in {p.symbol for p in prop.rejected}
    assert [i["symbol"] for i in prop.indicators] == ["KRW=X"]  # 股票 / ETF 不当板块指标
    assert any("FAKE.SZ" in n for n in prop.notes)  # Yahoo 查不到的代码去掉并注明
    stale = next(p for p in prop.rejected if p.symbol == "STALE.T")
    assert stale.name == "瑞萨电子（Systena）"  # 大模型的名称和 Yahoo 登记的对不上：并排显示
    assert prop.queries == {"en": ["NewCo Inc"], "zh": ["新公司"]}
    text = render_proposal(prop, settings)
    assert "新建主题「小部件」" in text and "同业篮子" in text and "夜间信号" in text and "FOREIGN.KS" in text
    # 去掉 / 加回
    assert prop.drop("PEER1") and "PEER1" not in {p.symbol for p in prop.chosen("peers")}
    assert prop.restore("PEER1") and "PEER1" in {p.symbol for p in prop.chosen("peers")}
    again = Proposal.from_dict(json.loads(json.dumps(prop.to_dict())))  # 存进 kv 等确认、再取出来
    assert again.chosen("peers")[0].symbol == prop.chosen("peers")[0].symbol and again.benchmark == "basket"


async def test_propose_joins_an_existing_theme_when_linked(settings, yahoo):
    th = settings.universe.themes["optics"]
    th.peers = {"PEER1": "同业一", "PEER2": "同业二"}
    th.benchmark = "basket"
    prop = await propose(settings, "NEWCO", FakePrices(synthetic_daily()), llm=None)
    assert not prop.new_theme and prop.theme_key == "optics" and prop.join_score > discover.JOIN_MIN
    assert {p.symbol for p in prop.peers} == {"PEER1", "PEER2"}
    assert any("add-holding" in n and "不花 API 的钱" in n for n in prop.notes)  # 没有候选文件、也没配大模型：提示怎么准备


async def test_propose_rejects_existing_holdings_and_unknown_codes(settings, yahoo):
    with pytest.raises(ScopeError, match="已经是持仓"):
        await propose(settings, "MU", FakePrices({}))
    with pytest.raises(ScopeError, match="查不到"):
        await propose(settings, "NOPE", FakePrices({}))


# ───────── 写入 / 撤销 / 移除 ─────────


@pytest.fixture
def cfg_copy(tmp_path, monkeypatch):
    """仓库配置的副本（settings / assets / themes），数据库和备份都在 tmp 里。"""
    dst = tmp_path / "config"
    shutil.copytree(TEST_CONFIG.parent, dst)
    text = (dst / "settings.yaml").read_text(encoding="utf-8")
    text = text.replace("sqlite_path: data/tickersentinel.sqlite3", f"sqlite_path: {tmp_path / 'data' / 'db.sqlite3'}")
    (dst / "settings.yaml").write_text(text, encoding="utf-8")
    for var in ("TICKERSENTINEL_SETTINGS", "TICKERSENTINEL_ASSETS", "TICKERSENTINEL_THEMES"):
        monkeypatch.delenv(var, raising=False)
    s = load_settings(dst / "settings.yaml", load_env=False)
    return s, tmp_path


async def test_add_then_undo_restores_every_file(cfg_copy, yahoo):
    s, tmp = cfg_copy
    original = (tmp / "config" / "assets.yaml").read_text(encoding="utf-8")
    prop = await propose(s, "NEWCO", FakePrices(synthetic_daily()), llm=fake_llm(s))
    editor = ScopeEditor(s)
    change = editor.plan_add(prop)
    preview = settings_after(s, change)  # 预览不碰正式文件
    assert "NEWCO" in preview.universe.symbols and (tmp / "config" / "assets.yaml").read_text(encoding="utf-8") == original
    effects = "\n".join(describe_change_effects(s, preview))
    assert "生效的主题：+小部件" in effects and "FOREIGN.KS" in effects
    new = editor.apply(change)
    assert new.universe.benchmark_for("NEWCO") == "basket:widgets" and "FOREIGN.KS" in [i.symbol for i in new.universe.monitored]
    theme_file = tmp / "config" / "themes" / "widgets.yaml"
    assert theme_file.exists() and "由 tsen scope 在新增持仓 NEWCO 时" in theme_file.read_text(encoding="utf-8")
    assets = (tmp / "config" / "assets.yaml").read_text(encoding="utf-8")
    assert assets.startswith(original.rstrip("\n")[:200]) and "# 盯哪些资产" in assets  # 原有注释都在
    assert "由 tsen scope 加入（主题 widgets）" in assets and "symbol: NEWCO" in assets
    assert len(editor.history()) == 1
    msg, back = editor.undo()
    assert msg.startswith("已撤销") and not theme_file.exists()
    assert (tmp / "config" / "assets.yaml").read_text(encoding="utf-8") == original
    assert "NEWCO" not in back.universe.symbols and editor.history() == []
    with pytest.raises(ScopeEditError, match="没有可以撤销"):
        editor.undo()


async def test_remove_keeps_theme_file_and_other_comments(cfg_copy):
    s, tmp = cfg_copy
    editor = ScopeEditor(s)
    change = editor.plan_remove("sndk")
    assert "移除 SNDK" in change.summary and "整体停用" not in change.summary  # MU、SKHY 还在存储芯片主题里
    new = editor.apply(change)
    assert new.universe.symbols == ["MU", "SKHY"] and "memory" in new.universe.themes
    assets = (tmp / "config" / "assets.yaml").read_text(encoding="utf-8")
    assert "SNDK" not in assets and "# 夜里" in assets and "# ── 以后持有黄金" in assets
    with pytest.raises(ScopeEditError, match="夜间信号"):
        editor.plan_remove("000660.KS")
    with pytest.raises(ScopeEditError, match="不在 assets.yaml"):
        editor.plan_remove("ZZZZ")


async def test_apply_rolls_back_when_the_result_does_not_load(cfg_copy):
    s, tmp = cfg_copy
    editor = ScopeEditor(s)
    original = (tmp / "config" / "assets.yaml").read_text(encoding="utf-8")
    from tickersentinel.scope.edit import Change

    bad = Change("add", "BAD", "坏的改动", {str(tmp / "config" / "assets.yaml"): original + "\n  - {symbol: BAD, name: x, theme: nope}\n"})
    with pytest.raises(ScopeEditError, match="已还原"):
        editor.apply(bad)
    assert (tmp / "config" / "assets.yaml").read_text(encoding="utf-8") == original and editor.history() == []


# ───────── 飞书 /scope：确认后写入、即时生效、可撤销 ─────────


async def test_scope_chat_add_adjust_confirm_and_undo(cfg_copy, yahoo):
    from tickersentinel.scope.chat import ScopeChat
    from tickersentinel.storage import Storage

    s, tmp = cfg_copy
    applied = []

    async def on_change(new):
        applied.append(new)
        s.universe = new.universe  # App.apply_scope 做的事（这里只换 universe）
        return describe_change_effects(s, new)

    chat = ScopeChat(s, Storage(":memory:"), FakePrices(synthetic_daily()), fake_llm(s), on_change=on_change, replay_days=0)
    cid = "oc_test"
    assert chat.wants(cid, "/scope") and not chat.wants(cid, "确认")  # 没有待确认的改动时，“确认”是普通对话
    assert "主题「存储芯片」" in await chat.handle(cid, "/scope")
    reply = await chat.handle(cid, "/scope add newco")
    assert "新建主题「小部件」" in reply and "回复「确认」写入" in reply and "生效的主题：+小部件" in reply
    assert not (tmp / "config" / "themes" / "widgets.yaml").exists()  # 还没确认，不写
    assert chat.wants(cid, "去掉 PEER1")
    reply = await chat.handle(cid, "去掉 PEER1")
    assert "PEER1" not in reply.split("没选上")[0].split("同业：")[1].split("\n")[0]
    reply = await chat.handle(cid, "确认")
    assert reply.startswith("✅ 已写入：新增持仓 NEWCO") and "已经生效" in reply and applied
    assert "NEWCO" in s.universe.symbols and (tmp / "config" / "themes" / "widgets.yaml").exists()
    assert "PEER1" not in s.universe.themes["widgets"].peers  # 去掉的没写进去
    assert not chat.wants(cid, "确认")  # 确认过就清掉了
    reply = await chat.handle(cid, "/scope undo")
    assert "将撤销最近一次改动" in reply
    reply = await chat.handle(cid, "确认")
    assert reply.startswith("✅ 已撤销") and "NEWCO" not in s.universe.symbols
    assert not (tmp / "config" / "themes" / "widgets.yaml").exists()
    # 取消
    await chat.handle(cid, "/scope remove SNDK")
    assert (await chat.handle(cid, "取消")).startswith("好的，已放弃") and "SNDK" in s.universe.symbols


async def test_scope_chat_preview_mode_never_writes(cfg_copy, yahoo):
    from tickersentinel.scope.chat import ScopeChat
    from tickersentinel.storage import Storage

    s, tmp = cfg_copy
    chat = ScopeChat(s, Storage(":memory:"), FakePrices(synthetic_daily()), fake_llm(s), readonly=True, replay_days=0)
    reply = await chat.handle("c", "/scope add NEWCO")
    assert "预览模式不写配置" in reply and not chat.wants("c", "确认")
    assert "预览模式" in await chat.handle("c", "/scope remove SNDK")


async def test_pending_change_expires_after_30_minutes(cfg_copy):
    from datetime import datetime, timedelta

    from tickersentinel.scope.chat import PENDING_KEY, ScopeChat
    from tickersentinel.storage import Storage

    s, _ = cfg_copy
    st = Storage(":memory:")
    chat = ScopeChat(s, st, None, None, replay_days=0)
    await chat.handle("c", "/scope remove SNDK")
    raw = json.loads(st.kv_get(PENDING_KEY.format(chat_id="c")))
    raw["at"] = (datetime.fromisoformat(raw["at"]) - timedelta(minutes=31)).isoformat()
    st.kv_set(PENDING_KEY.format(chat_id="c"), json.dumps(raw))
    assert not chat.wants("c", "确认")


async def test_app_hot_reloads_scope_from_chat_and_from_edited_files(cfg_copy):
    from tickersentinel.app import App
    from tickersentinel.scope.edit import add_asset_text

    s, tmp = cfg_copy
    s.notify.provider = "console"
    s.events.macro.enabled = False
    s.sources.news.google_news_rss.enabled = False
    s.sources.filings.sec_edgar.enabled = False
    app = App(s, dry_run=True)
    assert app.chat.scope is app.scope and app.scope.readonly  # 预览不写配置
    assets = tmp / "config" / "assets.yaml"
    text = assets.read_text(encoding="utf-8")
    assets.write_text(add_asset_text(text, {"symbol": "LITE", "name": "Lumentum", "theme": "optics"}, "测试加入"), encoding="utf-8")
    await app.maybe_reload_scope()  # 命令行 / 手工改了文件：下一次检测前读到
    assert "LITE" in s.universe.symbols and app.news.universe is s.universe
    us = next(g for g in app.monitor.groups if g.key == "us")
    assert "LITE" in us.symbols and us.benchmarks["LITE"] == "basket:optics" and "COHR" in us.fetch
    msg = app.notifier.sent[-1][1]
    assert msg.title == "🔧 监控范围已更新" and "光模块" in msg.body
    await app.maybe_reload_scope()  # 没再变：不重复读、不重复提醒
    assert len(app.notifier.sent) == 1
    assets.write_text("assets: [oops\n", encoding="utf-8")  # 写坏了：提醒一次，按原来的范围继续
    await app.maybe_reload_scope()
    assert "LITE" in s.universe.symbols and "读不通" in app.notifier.sent[-1][1].body
    await app.aclose()


async def test_linkage_health_flags_weak_peers_and_suggests_new_ones(settings, monkeypatch):
    from tickersentinel.config import Instrument
    from tickersentinel.scope import health

    async def similar(symbol, timeout=15.0):
        return ["PEER2", "NOISE"]

    monkeypatch.setattr(health, "yahoo_similar", similar)
    u = settings.universe
    u.watchlist = [Instrument(symbol="NEWCO", name="NewCo", themes=["memory"])]
    th = u.themes["memory"]
    from tickersentinel.config import OvernightCfg

    th.peers, th.benchmark = {"PEER1": "", "NOISE": ""}, "SPY"
    th.overnight = {"FOREIGN.KS": OvernightCfg(name="海外同行")}
    rows = await health.check_themes(settings, FakePrices(synthetic_daily()))
    advice = "\n".join(rows[0].advice)
    assert "NOISE" in advice and "联动变弱" in advice
    assert "可以考虑加进同业：PEER2" in advice
    assert rows[0].night["FOREIGN.KS"][0] > 0.5 and "FOREIGN.KS" not in advice
    text = health.render_health(settings, rows, brief=True)
    assert text.startswith("**联动体检**")
    assert health.render_health(settings, [], brief=True) == ""


# ───────── 调阈值（tsen scope tune / 飞书 /scope tune） ─────────


def test_tune_targets_resolve_to_the_right_place(cfg_copy):
    from tickersentinel.scope.tune import parse_numbers, resolve_target

    s, _ = cfg_copy
    assert resolve_target(s, "MU")[:3] == ("MU", "assets", None)
    code, where, theme, notes = resolve_target(s, "000660.KS")  # 篮子的成分 → 调的是篮子
    assert (code, where, theme) == ("night:memory:XKRX", "theme_basket", "memory") and "成分" in notes[0]
    assert resolve_target(s, "memory")[0] == "night:memory:XKRX"
    assert resolve_target(s, "存储芯片夜间信号")[0] == "night:memory:XKRX"
    with pytest.raises(ScopeError, match="不在监控范围"):
        resolve_target(s, "ZZZZ")
    assert parse_numbers("全天 5 8 12") == ([5.0, 8.0, 12.0], None) == parse_numbers("关口 5 8 12")  # 旧写法也认
    assert parse_numbers("急涨 5:2 15:3.5，30:5") == (None, {5: 2.0, 15: 3.5, 30: 5.0})


async def test_tune_suggests_from_volatility_and_writes_where_it_belongs(cfg_copy):
    from tickersentinel.scope.tune import propose_tune, render_tune

    s, tmp = cfg_copy
    daily = synthetic_daily()
    prices = FakePrices({"MU": daily["NEWCO"], "000660.KS": daily["PEER1"], "005930.KS": daily["PEER2"]})
    plan = await propose_tune(s, "MU", prices, replay_days=0)  # 合成的 NEWCO 日波动约 2.5%：照默认
    assert plan.sigma_pct is not None
    plan = await propose_tune(s, "MU", prices, day=[6, 10, 15], replay_days=0)
    text = render_tune(plan)
    assert "全天：5/8/12% → 6/10/15%" in text and "急涨急跌：5分钟 1.5%、15分钟 2.5%、30分钟 3%、60分钟 3%（不变）" in text
    assert "config/assets.yaml 里 MU 那一项的 alert" in text
    new = ScopeEditor(s).apply(plan.change)
    assert new.rules_for("MU").day_pct == [6.0, 10.0, 15.0] and new.rules_for("SNDK").day_pct == [5, 8, 12]
    assert "alert: {day_pct: [6, 10, 15]" in (tmp / "config" / "assets.yaml").read_text(encoding="utf-8")
    lines = describe_change_effects(s, new)
    assert "MU：全天 5/8/12% → 6/10/15%" in lines  # 改了阈值也会告诉读者改成了什么
    # 夜间篮子：写进主题文件的 overnight_alert，原有注释不动
    plan = await propose_tune(new, "005930.KS", prices, day=[4, 7], replay_days=0)
    assert plan.where == "theme_basket" and "overnight_alert" in plan.destination
    newer = ScopeEditor(new).apply(plan.change)
    theme_text = (tmp / "config" / "themes" / "memory.yaml").read_text(encoding="utf-8")
    assert "overnight_alert: {" in theme_text and "day_pct: [4, 7]" in theme_text and "# 主题「存储芯片」" in theme_text
    assert newer.rules_for("night:memory:XKRX").day_pct == [4.0, 7.0]
    msg, back = ScopeEditor(newer).undo()  # 撤销最近一次（篮子），MU 的改动还在
    assert back.rules_for("night:memory:XKRX").day_pct == [5, 8, 12] and back.rules_for("MU").day_pct == [6, 10, 15]


async def test_scope_chat_tune_flow(cfg_copy):
    from tickersentinel.scope.chat import ScopeChat
    from tickersentinel.storage import Storage

    s, tmp = cfg_copy
    applied = []

    async def on_change(new):
        applied.append(new)
        effects = describe_change_effects(s, new)
        s.universe = new.universe
        return effects

    chat = ScopeChat(s, Storage(":memory:"), FakePrices({}), None, on_change=on_change, replay_days=0)
    reply = await chat.handle("c", "/scope tune SNDK 全天 6 10 15")
    assert "全天：5/8/12% → 6/10/15%" in reply and "回复「确认」写入" in reply
    reply = await chat.handle("c", "确认")
    assert reply.startswith("✅ 已写入：调整 SNDK 的警报标准") and "SNDK：全天 5/8/12% → 6/10/15%" in reply
    assert s.rules_for("SNDK").day_pct == [6.0, 10.0, 15.0]
    assert "没有日波动数据" in await chat.handle("c", "/scope tune MU")  # 取不到日线又没给数字：说明原因


async def test_nomination_asks_the_online_model_to_search(settings, yahoo):
    llm = fake_llm(settings)
    await propose(settings, "NEWCO", FakePrices(synthetic_daily()), llm=llm)
    call = llm.providers["fake"].calls[0]
    assert call["web_search"] is True and call["json_schema"] is not None  # Claude 联网核实代码；本地模型照样按 schema 输出
    from tickersentinel.config import LLMCfg

    cfg = LLMCfg()  # 默认不调用任何 API（在 Claude Code 里用订阅额度准备候选）；线上 Claude 留作备用档
    assert cfg.tasks.scope == [] and cfg.providers["claude_research"].model == "claude-opus-5-5"
    assert cfg.providers["claude_research"].effort == "high"


async def test_candidates_prepared_in_claude_code_replace_the_llm(settings, yahoo):
    """Claude Code 写好的候选文件（data/scope_candidates/代码.json）：自动读取，不再问大模型；候选照样要过数据打分。"""
    from tickersentinel.scope.discover import candidates_brief, candidates_path, load_candidates

    path = candidates_path(settings, "newco")
    assert path.name == "NEWCO.json" and path.parent.name == "scope_candidates"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(LLM_JSON, ensure_ascii=False), encoding="utf-8")
    llm = fake_llm(settings)
    prop = await propose(settings, "NEWCO", FakePrices(synthetic_daily()), llm=llm)
    assert llm.providers["fake"].calls == []  # 有候选文件就不调用大模型
    assert prop.theme_key == "widgets" and prop.theme_name == "小部件"
    assert "PEER1" in {p.symbol for p in prop.peers}
    assert "FAKE.SZ" not in {p.symbol for p in prop.overnight + prop.catalysts + prop.rejected}  # Yahoo 查不到的代码直接去掉
    assert any("Claude Code 准备的候选" in line for line in render_proposal(prop, settings).splitlines())
    # 旧文件（超过 30 天）不自动用；明确给了路径就照用
    import os
    import time

    old = time.time() - 40 * 86400
    os.utime(path, (old, old))
    assert load_candidates(settings, "NEWCO") is None and load_candidates(settings, "NEWCO", path) is not None
    with pytest.raises(discover.ScopeError, match="不存在"):
        load_candidates(settings, "NEWCO", path.with_name("NOPE.json"))
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(discover.ScopeError, match="JSON 对象"):
        load_candidates(settings, "NEWCO", path)
    brief = await candidates_brief(settings, "NEWCO")  # tsen scope prompt：给 Claude Code 的上下文
    assert "NewCo Holdings Inc." in brief and str(path) in brief and '"theme_key"' in brief and "tsen scope add NEWCO" in brief and "--yes" in brief


def test_proposal_survives_the_json_roundtrip_of_a_pending_feishu_confirmation(cfg_copy):
    """飞书 /scope add 的待确认方案存成 JSON：数字键变成字符串，读回来要还原，否则等于默认的阈值会被写死。"""
    from tickersentinel.scope.discover import Pick, Proposal

    s, tmp = cfg_copy
    p = Proposal(symbol="NEWCO", name="NewCo", asset_class="us_equity", theme_key="memory", theme_name="存储芯片",
                 new_theme=False, rapid=dict(s.rules_for("MU").min_move_pct), day_pct=list(s.rules_for("MU").day_pct),
                 overnight_alert={"rapid_pct": {5: 2.0, 60: 3.5}}, peers=[Pick("WDC", "西部数据", 0.7, 120)])
    back = Proposal.from_dict(json.loads(json.dumps(p.to_dict())))
    assert back.rapid == p.rapid and back.overnight_alert["rapid_pct"] == {5: 2.0, 60: 3.5}
    change = ScopeEditor(s).plan_add(back)
    added = next(iter(change.files.values()))
    assert "NEWCO" in added and "rapid_pct" not in added.split("NEWCO")[1]  # 和默认一样：不写 alert
    assert "警报标准" not in render_proposal(back, s)


def test_new_theme_puts_basket_thresholds_on_the_basket():
    """新主题里同一交易所有两只夜间信号：会合成篮子触发，按波动换算的阈值写给篮子（overnight_alert），成分不写。"""
    from tickersentinel.scope.discover import Pick, Proposal
    from tickersentinel.scope.edit import theme_yaml

    p = Proposal(symbol="NEWCO", name="NewCo", asset_class="us_equity", theme_key="widgets", theme_name="小部件",
                 new_theme=True, overnight=[Pick("300308.SZ", "中际旭创", 0.4, 200), Pick("300502.SZ", "新易盛", 0.38, 200),
                                            Pick("2330.TW", "台积电", 0.35, 200)],
                 overnight_alert={"rapid_pct": {5: 2.0, 15: 3.0, 30: 3.5, 60: 3.5}, "day_pct": [6.0, 10.0, 15.0]})
    text = theme_yaml(p, "2026-10-24")
    assert "overnight_alert: {rapid_pct: {5: 2, 15: 3, 30: 3.5, 60: 3.5}, day_pct: [6, 10, 15]}" in text
    assert '"300308.SZ": "中际旭创"   #' in text and '"2330.TW": {name: "台积电", alert:' in text  # 台湾只有一只：写它自己的


async def test_tune_only_changes_the_windows_you_give(cfg_copy):
    from tickersentinel.scope.tune import propose_tune

    s, _ = cfg_copy
    plan = await propose_tune(s, "MU", None, rapid={60: 4.0}, replay_days=0)
    assert plan.new["rapid_pct"] == {5: 1.5, 15: 2.5, 30: 3.0, 60: 4.0}  # 其他窗口不变
    plan = await propose_tune(s, "MU", None, rapid={60: 0}, replay_days=0)
    assert plan.new["rapid_pct"] == {5: 1.5, 15: 2.5, 30: 3.0}  # 写 0 去掉这一档
    # 写在 assets.yaml 里的夜间篮子成分：调的是篮子（成分只跑“个股特异”）
    from tickersentinel.config import Instrument
    from tickersentinel.scope.tune import resolve_target

    s.universe.watchlist.append(Instrument(symbol="000660.KS", name="SK海力士", asset_class="foreign_equity", holding=False,
                                           themes=["memory"], related_to=["SKHY"]))
    assert resolve_target(s, "000660.KS")[:2] == ("night:memory:XKRX", "theme_basket")


async def test_tune_single_overnight_signal_in_list_form_and_in_place(cfg_copy):
    from tickersentinel.scope.tune import propose_tune

    s, tmp = cfg_copy
    theme = tmp / "config" / "themes" / "memory.yaml"
    plan = await propose_tune(s, "285A.T", None, day=[7, 11, 16], replay_days=0)
    new = ScopeEditor(s).apply(plan.change)
    text = theme.read_text(encoding="utf-8")
    assert new.rules_for("285A.T").day_pct == [7.0, 11.0, 16.0]
    assert "东京，和首尔同一时段" in text and "\n\ncatalysts:" in text  # 这一项的注释、后面的空行都还在
    # 文档允许的列表写法：调单只夜间信号不能崩溃
    start = text.index("\novernight:")
    end = text.index("\n\ncatalysts:")
    theme.write_text(text[:start] + '\novernight: ["000660.KS", "005930.KS", "285A.T"]' + text[end:], encoding="utf-8")
    s2 = ScopeEditor(new).reload()
    plan = await propose_tune(s2, "285A.T", None, day=[8, 12], replay_days=0)
    assert ScopeEditor(s2).apply(plan.change).rules_for("285A.T").day_pct == [8.0, 12.0]


def test_undo_refuses_when_the_file_changed_afterwards(cfg_copy):
    s, tmp = cfg_copy
    editor = ScopeEditor(s)
    change = editor.plan_remove("SNDK")
    editor.apply(change)
    assets = tmp / "config" / "assets.yaml"
    assets.write_text(assets.read_text(encoding="utf-8") + "\n# 之后手工加的一行\n", encoding="utf-8")
    with pytest.raises(ScopeEditError, match="又被改过"):
        editor.undo()
    msg, back = editor.undo(force=True)
    assert "SNDK" in back.universe.symbols and "手工加的一行" not in assets.read_text(encoding="utf-8")


def test_misspelled_asset_class_in_defaults_is_an_error(tmp_path):
    from pydantic import ValidationError

    (tmp_path / "settings.yaml").write_text("universe: {benchmark: SPY}\n", encoding="utf-8")
    (tmp_path / "assets.yaml").write_text("defaults: {us_equty: {day_pct: [9]}}\nassets:\n  - {symbol: AAPL, name: 苹果}\n",
                                          encoding="utf-8")
    with pytest.raises(ValidationError, match="us_equty"):
        load_settings(tmp_path / "settings.yaml", load_env=False)


async def test_settings_only_change_asks_for_a_restart_instead_of_reloading(cfg_copy):
    from tickersentinel.app import App

    s, tmp = cfg_copy
    s.notify.provider = "console"
    s.events.macro.enabled = False
    app = App(s, dry_run=True)
    groups = app.monitor.groups
    cfg = tmp / "config" / "settings.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace("merge_minutes: 10", "merge_minutes: 5"), encoding="utf-8")
    await app.maybe_reload_scope()
    assert app.monitor.groups is groups  # 监控范围没变：不重载
    assert "要重启" in app.notifier.sent[-1][1].body
    await app.aclose()
