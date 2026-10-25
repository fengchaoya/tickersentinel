"""测试公共工具：离线的合成行情、假数据源、测试用配置。所有测试都不需要联网和 API key。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tickersentinel.config import Secrets, Settings, load_settings
from tickersentinel.market_calendar import MarketCalendar, Session
from tickersentinel.models import NewsItem
from tickersentinel.sources.base import NewsQuery, NewsSource, PriceSource
from tickersentinel.timeutil import UTC

# 测试用的配置：仓库 config/ 的一份冻结副本（持仓 MU / SNDK / SKHY + 存储芯片主题），示例配置改了也不影响测试
TEST_CONFIG = Path(__file__).parent / "fixtures" / "config" / "settings.yaml"
TARGET_DAY = date(2026, 10, 9)  # 回放/检测用的“当天”（周五）
BENCH = "SOXX"


@pytest.fixture(scope="session")
def calendar() -> MarketCalendar:
    return MarketCalendar()


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = load_settings(TEST_CONFIG, load_env=False)
    s.secrets = Secrets()
    s.storage.sqlite_path = str(tmp_path / "test.sqlite3")
    s.notify.provider = "console"
    s.notify.console_log_path = None
    s.events.macro.enabled = False  # 宏观日程的来源会联网；测试全部离线，需要时在测试里单独打开并替换来源
    s.briefs.weekly.linkage_check = False  # 联动体检会查 Yahoo 的相似股票（联网）
    return s


def isolate(settings: Settings, symbols, *, peers: bool = False) -> Settings:
    """测试里只留这几个资产：资产清单只保留 symbols；主题的同业清空（peers=False），夜间信号只留 symbols 里的。"""
    u = settings.universe
    u.watchlist = [i for i in u.watchlist if i.symbol in symbols]
    for th in u.themes.values():
        if not peers:
            th.peers = {}
        th.overnight = {k: v for k, v in th.overnight.items() if k in symbols}
    return settings


@dataclass
class SyntheticMarket:
    sessions: list[Session]  # 有分钟数据的交易日（含估计窗口）
    intraday: dict[str, pd.DataFrame]
    daily: dict[str, pd.DataFrame]

    @property
    def target(self) -> Session:
        return self.sessions[-1]


def build_market(
    calendar: MarketCalendar,
    *,
    target: date = TARGET_DAY,
    n_prior: int = 5,
    n_extra_days: int = 0,
    symbols: tuple[str, ...] = ("MU", "SNDK", "SKHY", "WDC"),
    betas: dict[str, float] | None = None,
    shocks: dict[tuple[str, date, int], float] | None = None,
    sigma_m: float = 0.0004,
    sigma_e: float = 0.0004,
    seed: int = 7,
) -> SyntheticMarket:
    """合成一个单因子市场：r_i = β_i·r_m + ε_i（分钟级），日线与分钟线一致。

    shocks[(代码, 日期, 第几分钟)] = 额外的对数收益；对 SOXX 的冲击会按 β 传导到所有股票。
    """
    rng = np.random.default_rng(seed)
    betas = betas or {"MU": 1.3, "SNDK": 1.5, "SKHY": 1.2, "WDC": 1.1}
    shocks = shocks or {}
    sessions = [*calendar.sessions_before(target, n_prior), calendar.session(target)]
    for _ in range(n_extra_days):
        nxt = calendar.next_session(sessions[-1].day + timedelta(days=1))
        sessions.append(nxt)
    all_syms = (BENCH, *symbols)

    # 1) 估计窗口之前 80 个交易日的日线（用于 β）
    hist_days = calendar.sessions_before(sessions[0].day, 80)
    rm_d = rng.normal(0, 0.015, len(hist_days))
    daily_rows: dict[str, list[tuple[pd.Timestamp, float]]] = {s: [] for s in all_syms}
    price = {BENCH: 250.0, "MU": 110.0, "SNDK": 60.0, "SKHY": 150.0, "WDC": 70.0}
    for i, d in enumerate(hist_days):
        for s in all_syms:
            r = rm_d[i] if s == BENCH else betas[s] * rm_d[i] + rng.normal(0, 0.01)
            price[s] *= float(np.exp(r))
            daily_rows[s].append((pd.Timestamp(d.day), price[s]))

    # 2) 分钟线，每天收盘价同时写入日线
    intraday: dict[str, list[pd.DataFrame]] = {s: [] for s in all_syms}
    for sess in sessions:
        n = sess.minutes
        rm = rng.normal(0, sigma_m, n)
        for (sym, day, k), v in shocks.items():
            if sym == BENCH and day == sess.day:
                rm[k] += v
        idx = pd.date_range(sess.open, periods=n, freq="1min")
        for s in all_syms:
            if s == BENCH:
                r = rm.copy()
            else:
                r = betas[s] * rm + rng.normal(0, sigma_e, n)
                for (sym, day, k), v in shocks.items():
                    if sym == s and day == sess.day:
                        r[k] += v
            closes = price[s] * np.exp(np.cumsum(r))
            vol = rng.integers(800, 1200, n).astype(float)
            intraday[s].append(pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": vol}, index=idx))
            price[s] = float(closes[-1])
            daily_rows[s].append((pd.Timestamp(sess.day), price[s]))
    daily = {s: pd.DataFrame({"close": [p for _, p in rows]}, index=pd.DatetimeIndex([d for d, _ in rows])) for s, rows in daily_rows.items()}
    return SyntheticMarket(sessions=sessions, intraday={s: pd.concat(v) for s, v in intraday.items()}, daily=daily)


class FakePriceSource(PriceSource):
    name = "fake"

    def __init__(self, market: SyntheticMarket):
        self.market = market

    async def intraday_bars(self, symbols, start, end=None, *, prepost=False):
        out = {}
        for s in symbols:
            df = self.market.intraday.get(s)
            if df is None:
                continue
            mask = df.index >= start
            if end is not None:
                mask &= df.index < end
            out[s] = df[mask]
        return out

    async def daily_bars(self, symbols, lookback_days):
        return {s: self.market.daily[s] for s in symbols if s in self.market.daily}


class FakeNewsSource(NewsSource):
    name = "fake_news"

    def __init__(self, items: list[NewsItem]):
        self.items = items
        self.queries: list[NewsQuery] = []

    async def fetch(self, q: NewsQuery) -> list[NewsItem]:
        self.queries.append(q)
        return [i for i in self.items if q.start <= i.published_at <= q.end]


def at(session: Session, minutes: int) -> datetime:
    return session.open + timedelta(minutes=minutes)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)
