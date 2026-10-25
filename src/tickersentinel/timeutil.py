"""时间工具。约定：内部一律使用带时区的 UTC 时间；只在展示时转换为本地时区。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
ET = ZoneInfo("America/New_York")


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("不允许使用不带时区的 datetime")
    return ts.astimezone(UTC)


def floor_minute(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0)


def fmt_local(ts: datetime, tz: ZoneInfo | str, fmt: str = "%m-%d %H:%M") -> str:
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    return ts.astimezone(zone).strftime(fmt)


def tz_abbr(ts: datetime, tz: ZoneInfo | str) -> str:
    """时区缩写，例如 CDT / CST（夏令时切换后自动变化）。"""
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    return ts.astimezone(zone).tzname() or ""


_WEEKDAYS = "一二三四五六日"


def weekday_cn(ts: datetime, tz: ZoneInfo | str) -> str:
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    return "周" + _WEEKDAYS[ts.astimezone(zone).weekday()]


def parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def minutes(n: float) -> timedelta:
    return timedelta(minutes=n)
