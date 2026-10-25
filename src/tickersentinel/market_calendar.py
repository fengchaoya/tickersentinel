"""交易日历：基于 exchange_calendars，自动处理节假日与提前收盘（如感恩节次日 13:00 ET 收盘）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

from tickersentinel.timeutil import UTC


@dataclass(frozen=True)
class Session:
    day: date
    open: datetime  # UTC
    close: datetime  # UTC
    break_start: datetime | None = None  # 午休（东京 11:30–12:30、上海深圳 11:30–13:00、香港 12:00–13:00）；没有午休为 None
    break_end: datetime | None = None

    @property
    def minutes(self) -> int:
        return int((self.close - self.open).total_seconds() // 60)

    @property
    def break_offsets(self) -> tuple[int, int] | None:
        """午休在分钟网格里的位置：(午休开始, 午休结束) 距开盘的分钟数；没有午休为 None。"""
        if self.break_start is None or self.break_end is None:
            return None
        return (int((self.break_start - self.open).total_seconds() // 60),
                int((self.break_end - self.open).total_seconds() // 60))

    @property
    def early_close(self) -> bool:
        return self.minutes < 390

    def contains(self, ts: datetime) -> bool:
        return self.open <= ts < self.close


class MarketCalendar:
    def __init__(self, exchange: str = "XNYS"):
        self.exchange = exchange
        self._cal = xcals.get_calendar(exchange)
        self.tz = ZoneInfo(str(self._cal.tz))

    def is_session(self, day: date) -> bool:
        return bool(self._cal.is_session(pd.Timestamp(day)))

    def session(self, day: date) -> Session | None:
        ts = pd.Timestamp(day)
        if not self._cal.is_session(ts):
            return None
        bs, be = self._cal.session_break_start(ts), self._cal.session_break_end(ts)
        return Session(
            day=day,
            open=self._cal.session_open(ts).to_pydatetime().astimezone(UTC),
            close=self._cal.session_close(ts).to_pydatetime().astimezone(UTC),
            break_start=None if pd.isna(bs) else bs.to_pydatetime().astimezone(UTC),
            break_end=None if pd.isna(be) else be.to_pydatetime().astimezone(UTC),
        )

    def holiday_names(self, start: date, end: date) -> dict[date, str]:
        """[start, end] 内的例行休市日 → 英文名称（exchange_calendars 的 regular_holidays；临时休市不在内）。"""
        try:
            hol = self._cal.regular_holidays
            if hol is None:
                return {}
            names = hol.holidays(pd.Timestamp(start), pd.Timestamp(end), return_name=True)
        except Exception:
            return {}
        return {ts.date(): str(name) for ts, name in names.items()}

    def exchange_date(self, ts: datetime) -> date:
        return ts.astimezone(self.tz).date()

    def session_at(self, ts: datetime) -> Session | None:
        """ts 位于某个交易时段内（开盘 ≤ ts < 收盘）时返回该时段，否则 None。"""
        s = self.session(self.exchange_date(ts))
        return s if s and s.contains(ts) else None

    def previous_session(self, day: date) -> Session:
        """严格早于 day 的最近一个交易日。"""
        ts = pd.Timestamp(day)
        if self._cal.is_session(ts):
            prev = self._cal.previous_session(ts)
        else:
            prev = self._cal.date_to_session(ts, direction="previous")
        return self.session(prev.date())  # type: ignore[return-value]

    def next_session(self, day: date) -> Session:
        """不早于 day 的最近一个交易日。"""
        ts = self._cal.date_to_session(pd.Timestamp(day), direction="next")
        return self.session(ts.date())  # type: ignore[return-value]

    def sessions_before(self, day: date, n: int) -> list[Session]:
        """严格早于 day 的 n 个交易日（按时间升序）。"""
        last = self.previous_session(day)
        days = self._cal.sessions_window(pd.Timestamp(last.day), -n)
        return [self.session(d.date()) for d in days]  # type: ignore[misc]

    def sessions_between(self, start: date, end: date) -> list[Session]:
        days = self._cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
        return [self.session(d.date()) for d in days]  # type: ignore[misc]

    def last_completed_sessions(self, now: datetime, n: int) -> list[Session]:
        """截至 now 已经收盘的最近 n 个交易日。"""
        today = self.exchange_date(now)
        s = self.session(today)
        if s and now >= s.close:
            return [*self.sessions_before(today, n - 1), s] if n > 1 else [s]
        return self.sessions_before(today, n)


def session_grid(session: Session) -> pd.DatetimeIndex:
    """交易时段内每分钟 K 线的起始时刻（UTC）。"""
    return pd.date_range(session.open, session.close - timedelta(minutes=1), freq="1min")
