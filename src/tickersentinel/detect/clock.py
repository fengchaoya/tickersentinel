"""交易时段（“段”）：把 24 小时切成一段一段，每段内部的检测与美股常规时段完全一样（session_frame 对齐网格、
用前几个同类段估计波动、全天累计按参照收盘价算）。不同资产按自己的“钟”切段：

- us（美股）：每个 NYSE 交易日切成 盘前 [04:00, 开盘)、盘中 [开盘, 收盘)、盘后 [收盘, 20:00 美东)，提前收盘日按实际收盘。
  参照价：盘前 / 盘中 = 上一交易日收盘；盘后 = 当天收盘（盘后的 +8% 指较今天收盘，和券商 App 的“盘后涨跌”一致）。
  美东 20:00–04:00 的夜盘没有免费数据，不切段。
- cme（期货）：每个工作日一段，美东前一天 18:00 到当天 17:00（周日晚开盘的那段算周一的）；节假日没有数据，自然不触发。
  参照价 = 上一段结束（美东 17:00）时的价格。
- utc（加密货币）：UTC 自然日，参照价 = UTC 0 点。
- 海外交易所（XKRX 首尔、XTKS 东京……）：exchange_calendars 的常规时段，参照价 = 上一交易日收盘。

段的 key（如 "2026-10-21"、"2026-10-21:post"、"cme:2026-10-21"）用作冷却去重和入库的 session_day：
盘前和盘中参照同一个昨收、共用一个 key，所以盘前已经推过的全天 5% 那一档，开盘后不会再推一次（盘中段还带上盘前的高低点，见 SessionContext.carry）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from tickersentinel.config import FOREIGN_EXCHANGES, NIGHT_PREFIX, exchange_of  # noqa: F401 - 交易所映射放在 config
from tickersentinel.market_calendar import MarketCalendar, Session
from tickersentinel.timeutil import ET, UTC

PRE_OPEN = time(4, 0)  # 美股盘前开始（美东）
POST_CLOSE = time(20, 0)  # 美股盘后结束（美东）
CME_OPEN, CME_CLOSE = time(18, 0), time(17, 0)

SEGMENT_NAMES = {"pre": "盘前", "regular": "盘中", "post": "盘后", "cme": "期货", "utc": "", "exchange": ""}


@dataclass(frozen=True)
class Segment(Session):
    """一段连续交易时间。day = 所属交易日；kind = pre / regular / post / cme / utc / exchange；
    key = 冷却与入库用的标识；ref_at = 参照价取哪个时刻的收盘（全天累计的“较昨收”）。"""

    kind: str = "regular"
    key: str = ""
    ref_at: datetime | None = None

    @property
    def label(self) -> str:
        return SEGMENT_NAMES.get(self.kind, "")


def _at(day: date, t: time, tz=ET) -> datetime:
    return datetime.combine(day, t, tzinfo=tz).astimezone(UTC)


class Clock:
    """切段的接口。"""

    name = "clock"

    def segment_at(self, ts: datetime) -> Segment | None:
        raise NotImplementedError

    def previous(self, seg: Segment, n: int) -> list[Segment]:
        """seg 之前 n 个同类段（时间升序），用来估计波动和成交量画像。"""
        raise NotImplementedError

    def completed(self, now: datetime, days: int) -> list[Segment]:
        """最近 days 个交易日里已经结束的段（时间升序），回放用。"""
        raise NotImplementedError


class UsEquityClock(Clock):
    name = "us"

    def __init__(self, calendar: MarketCalendar, extended: bool = True):
        self.calendar = calendar
        self.extended = extended

    def segments_on(self, day: date) -> list[Segment]:
        s = self.calendar.session(day)
        if s is None:
            return []
        prev_close = self.calendar.previous_session(day).close
        regular = Segment(day, s.open, s.close, kind="regular", key=day.isoformat(), ref_at=prev_close)
        if not self.extended:
            return [regular]
        pre = Segment(day, _at(day, PRE_OPEN), s.open, kind="pre", key=day.isoformat(), ref_at=prev_close)
        post_end = max(_at(day, POST_CLOSE), s.close + timedelta(hours=1))
        post = Segment(day, s.close, post_end, kind="post", key=f"{day.isoformat()}:post", ref_at=s.close)
        return [pre, regular, post]

    def segment_at(self, ts: datetime) -> Segment | None:
        return next((g for g in self.segments_on(self.calendar.exchange_date(ts)) if g.contains(ts)), None)

    def completed(self, now: datetime, days: int) -> list[Segment]:
        today = self.calendar.exchange_date(now)
        sessions = self.calendar.sessions_before(today, days)
        if (s_today := self.calendar.session(today)) is not None:
            sessions.append(s_today)
        segs = [g for s in sessions for g in self.segments_on(s.day) if g.close <= now]
        keep = sorted({g.day for g in segs})[-days:]  # 只算有段已经结束的交易日
        return [g for g in segs if g.day in keep]

    def previous(self, seg: Segment, n: int) -> list[Segment]:
        out: list[Segment] = []
        for s in reversed(self.calendar.sessions_before(seg.day, n)):
            out.extend(g for g in self.segments_on(s.day) if g.kind == seg.kind)
        return sorted(out, key=lambda g: g.open)[-n:]


class CmeClock(Clock):
    """CME Globex：美东前一天 18:00 到当天 17:00，周一到周五各一段。"""

    name = "cme"

    def segment_for(self, day: date) -> Segment | None:
        if day.weekday() >= 5:
            return None
        open_ = _at(day - timedelta(days=1), CME_OPEN)
        close = _at(day, CME_CLOSE)
        prev = day - timedelta(days=3 if day.weekday() == 0 else 1)
        return Segment(day, open_, close, kind="cme", key=f"cme:{day.isoformat()}", ref_at=_at(prev, CME_CLOSE))

    def segment_at(self, ts: datetime) -> Segment | None:
        local = ts.astimezone(ET)
        day = local.date() + timedelta(days=1) if local.time() >= CME_OPEN else local.date()
        seg = self.segment_for(day)
        return seg if seg is not None and seg.contains(ts) else None

    def completed(self, now: datetime, days: int) -> list[Segment]:
        out: list[Segment] = []
        d = now.astimezone(ET).date() + timedelta(days=1)
        while len(out) < days:
            g = self.segment_for(d)
            if g is not None and g.close <= now:
                out.append(g)
            d -= timedelta(days=1)
        return out[::-1]

    def previous(self, seg: Segment, n: int) -> list[Segment]:
        out: list[Segment] = []
        d = seg.day
        while len(out) < n:
            d -= timedelta(days=1)
            g = self.segment_for(d)
            if g is not None:
                out.append(g)
        return out[::-1]


class UtcClock(Clock):
    name = "utc"

    def segment_for(self, day: date) -> Segment:
        open_ = datetime.combine(day, time(0), tzinfo=UTC)
        return Segment(day, open_, open_ + timedelta(days=1), kind="utc", key=f"utc:{day.isoformat()}", ref_at=open_)

    def segment_at(self, ts: datetime) -> Segment | None:
        return self.segment_for(ts.astimezone(UTC).date())

    def previous(self, seg: Segment, n: int) -> list[Segment]:
        return [self.segment_for(seg.day - timedelta(days=k)) for k in range(n, 0, -1)]

    def completed(self, now: datetime, days: int) -> list[Segment]:
        today = now.astimezone(UTC).date()
        return [self.segment_for(today - timedelta(days=k)) for k in range(days, 0, -1)]


class ExchangeClock(Clock):
    """海外交易所的常规时段（exchange_calendars）。午休在网格里前向填充；检测时午休后的头几分钟当作开盘对待
    （见 detect/price.py：跨午休的窗口不算，复盘后先预热），否则午休前后的价差会被当成“几分钟内急涨急跌”。"""

    def __init__(self, exchange: str):
        self.name = exchange
        self.calendar = MarketCalendar(exchange)

    def _seg(self, s: Session) -> Segment:
        prev = self.calendar.previous_session(s.day).close
        return Segment(s.day, s.open, s.close, s.break_start, s.break_end, kind="exchange",
                       key=f"{self.name}:{s.day.isoformat()}", ref_at=prev)

    def segment_at(self, ts: datetime) -> Segment | None:
        s = self.calendar.session_at(ts)
        return self._seg(s) if s is not None else None

    def previous(self, seg: Segment, n: int) -> list[Segment]:
        return [self._seg(s) for s in self.calendar.sessions_before(seg.day, n)]

    def completed(self, now: datetime, days: int) -> list[Segment]:
        return [self._seg(s) for s in self.calendar.last_completed_sessions(now, days)]


def clock_key(asset_class: str, symbol: str) -> str:
    """资产按哪个钟切段。夜间篮子按它的交易所。"""
    if symbol.startswith(NIGHT_PREFIX):
        return exchange_of(symbol) or "us"
    if asset_class == "future":
        return "cme"
    if asset_class == "crypto":
        return "utc"
    if asset_class == "foreign_equity":
        return exchange_of(symbol) or "us"
    return "us"


def build_clock(key: str, calendar: MarketCalendar, *, extended: bool = True) -> Clock:
    if key == "us":
        return UsEquityClock(calendar, extended=extended)
    if key == "cme":
        return CmeClock()
    if key == "utc":
        return UtcClock()
    return ExchangeClock(key)
