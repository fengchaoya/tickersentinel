"""数据源接口。新增数据源时实现对应抽象类，并在 tickersentinel/sources/__init__.py 的工厂函数里注册。

约定：
- 所有网络调用都在 sources/ 下完成，必须设置超时；失败时抛 SourceError，由调用方决定降级。
- K 线 DataFrame：index 为 UTC 时间戳（分钟 K 线为该分钟起点），列至少包含 open/high/low/close/volume。
- 分钟数据可能包含盘前盘后，由调用方按交易时段过滤。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd

from tickersentinel.models import Event, NewsItem, Quote


class SourceError(RuntimeError):
    """数据源请求失败（网络、限流、鉴权、格式变化等）。status：HTTP 状态码（不是 HTTP 层面的失败则为 None）。"""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class PriceSource(ABC):
    name: str = "price"

    @abstractmethod
    async def intraday_bars(
        self, symbols: list[str], start: datetime, end: datetime | None = None, *, prepost: bool = False
    ) -> dict[str, pd.DataFrame]:
        """1 分钟 K 线。缺数据的代码不出现在结果里。prepost=True 时含美股盘前盘后（04:00–20:00 美东）。"""

    @abstractmethod
    async def daily_bars(self, symbols: list[str], lookback_days: int) -> dict[str, pd.DataFrame]:
        """日 K 线（未复权 close；如有 adj_close 列则为复权价）。index 为不带时区的交易日日期。"""

    async def latest_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """最新价（含盘前盘后）与昨收。默认用日 K 线近似，子类可覆盖。"""
        daily = await self.daily_bars(symbols, lookback_days=10)
        out: dict[str, Quote] = {}
        for sym, df in daily.items():
            closes = df["close"].dropna()
            if len(closes) >= 2:
                out[sym] = Quote(sym, float(closes.iloc[-1]), float(closes.iloc[-2]), session="closed",
                                 day=closes.index[-1].date())
        return out

    async def aclose(self) -> None:  # noqa: B027 - 可选覆盖
        pass


@dataclass(frozen=True)
class NewsQuery:
    start: datetime
    end: datetime
    symbols: tuple[str, ...]
    # symbol（或 SECTOR）→ 语言 → 检索词
    queries: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    langs: tuple[str, ...] = ("en", "ko", "zh")


class NewsSource(ABC):
    """新闻与公告来源（公告以 NewsItem(kind="filing") 返回）。"""

    name: str = "news"

    @abstractmethod
    async def fetch(self, q: NewsQuery) -> list[NewsItem]:
        ...

    async def aclose(self) -> None:  # noqa: B027
        pass


class EventSource(ABC):
    """事件来源（财报日、内部人拟减持、电话会预告……），由 events.hub.EventHub 定时汇总入库。

    collect() 返回事件日期落在 [start, end]（含两端）之间、与 symbols 相关的事件；
    不支持的代码直接跳过（例如 SEC 只管美股代码）；整个来源不可用时抛 SourceError，单个代码失败只记日志。
    """

    name: str = "events"
    scope: set[str] | None = None  # 只看这些代码（None 表示全部），由 EventHub 过滤后传入

    @abstractmethod
    async def collect(self, symbols: list[str], start: date, end: date) -> list[Event]:
        ...

    async def aclose(self) -> None:  # noqa: B027
        pass


class OptionsSource(ABC):
    """期权链快照（第 4 阶段实现）。

    计划：每 poll_seconds 拉一次各标的近月期权链，返回 DataFrame：
    contract, expiry, strike, right(C/P), bid, ask, last, volume, open_interest, iv, delta, ts。
    候选实现：券商 API（Tradier / Schwab / IBKR，开户即有实时数据）、yfinance（延迟，仅原型）、Massive（付费逐笔）。
    """

    name: str = "options"

    @abstractmethod
    async def chain_snapshot(self, symbol: str, max_days_to_expiry: int = 60) -> pd.DataFrame:
        ...
