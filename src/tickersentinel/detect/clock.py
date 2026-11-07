"""交易时段（“段”）：把 24 小时切成一段一段，每段内部的检测与美股常规时段完全一样（session_frame 对齐网格、
用前几个同类段估计波动、全天累计按参照收盘价算）。不同资产按自己的“钟”切段：

- us（美股）：每个 NYSE 交易日切成 夜盘 [前一天 20:00, 04:00)、盘前 [04:00, 开盘)、盘中 [开盘, 收盘)、盘后 [收盘, 20:00 美东)，
  提前收盘日按实际收盘。夜盘只给 hours: 24x5 的资产（数据来自 Hyperliquid 上的股票永续合约，见 sources/prices_overnight.py），
  覆盖美东周日 20:00 到周五 20:00，和 24 小时交易的券商一致；周五 20:00 到周日 20:00 的周末不切段。
  以后要盯周末：在 UsEquityClock 里加一个 kind="weekend" 的段（那时只有合约自己定价，没有正股成交），hours 加一档 24x7。
  参照价：盘前 / 盘中 = 上一交易日收盘；盘后 / 夜盘 = 刚收盘的那个交易日的收盘（盘后的 +8% 指较今天收盘）。
- cme（期货）：每个工作日一段，美东前一天 18:00 到当天 17:00（周日晚开盘的那段算周一的）；节假日没有数据，自然不触发。
  参照价 = 上一段结束（美东 17:00）时的价格。
- utc（加密货币）：UTC 自然日，参照价 = UTC 0 点。
- 海外交易所（XKRX 首尔、XTKS 东京……）：exchange_calendars 的常规时段，参照价 = 上一交易日收盘。

段的 key（如 "2026-10-21"、"cme:2026-10-21"）用作冷却去重和入库的 session_day。参照价相同的几段共用一个 key：
D 日盘后、D′（下一个交易日）的夜盘、盘前、盘中都以 D 日收盘为参照，key 都是 D′——冷却和全天累计在它们之间连续，
盘后推过的“跌超 5%”到夜盘、到第二天盘前都不会再推一次；后面的段带上前面几段的高低点（SessionContext.carry）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from tickersentinel.config import FOREIGN_EXCHANGES, NIGHT_PREFIX, exchange_of  # noqa: F401 - 交易所映射放在 config
from tickersentinel.market_calendar import MarketCalendar, Session, night_window
from tickersentinel.timeutil import ET, UTC

PRE_OPEN = time(4, 0)  # 美股盘前开始（美东）
POST_CLOSE = time(20, 0)  # 美股盘后结束（美东）；之后是下一个交易日的夜盘（market_calendar.night_window）
CME_OPEN, CME_CLOSE = time(18, 0), time(17, 0)

SEGMENT_NAMES = {"night": "夜盘", "pre": "盘前", "regular": "盘中", "post": "盘后", "cme": "期货", "utc": "", "exchange": ""}


@dataclass(frozen=True)
class Segment(Session):
    """一段连续交易时间。day = 所属交易日（夜盘属于它后面那个交易日）；kind = night / pre / regular / post / cme / utc / exchange；
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

    def __init__(self, calendar: MarketCalendar, extended: bool = True, overnight: bool = False):
        self.calendar = calendar
        self.extended = extended or overnight
        self.overnight = overnight

    def night_segment(self, day: date) -> Segment | None:
        w = night_window(self.calendar, day)
        if w is None:
            return None
        return Segment(day, w[0], w[1], kind="night", key=day.isoformat(), ref_at=self.calendar.previous_session(day).close)

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
        nxt = self.calendar.next_session(day + timedelta(days=1)).day  # 盘后参照今天收盘，和下一个交易日的夜盘、盘前同一个 key
        post = Segment(day, s.close, post_end, kind="post", key=nxt.isoformat(), ref_at=s.close)
        night = self.night_segment(day) if self.overnight else None
        return [*([night] if night is not None else []), pre, regular, post]

    def segment_at(self, ts: datetime) -> Segment | None:
        d = self.calendar.exchange_date(ts)
        hit = next((g for g in self.segments_on(d) if g.contains(ts)), None)
        if hit is None and self.overnight:  # 晚上 20:00 以后（含周日晚上）：属于下一个交易日的夜盘
            g = self.night_segment(self.calendar.next_session(d + timedelta(days=1)).day)
            hit = g if g is not None and g.contains(ts) else None
        return hit

    def same_key_before(self, seg: Segment) -> list[Segment]:
        """和 seg 共用一个 key（同一个参照价）、在它之前的段：盘中 ← 盘前、夜盘、上一个交易日的盘后。"""
        prev = self.calendar.previous_session(seg.day).day
        segs = [*self.segments_on(prev), *self.segments_on(seg.day)]
        return sorted((g for g in segs if g.key == seg.key and g.close <= seg.open), key=lambda g: g.open)

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


def build_clock(key: str, calendar: MarketCalendar, *, extended: bool = True, overnight: bool = False) -> Clock:
    if key == "us":
        return UsEquityClock(calendar, extended=extended, overnight=overnight)
    if key == "cme":
        return CmeClock()
    if key == "utc":
        return UtcClock()
    return ExchangeClock(key)
