"""冬令时切换（2026-11-01 02:00 CDT → 01:00 CST，本地 01:00–02:00 出现两遍）：网页图表的本地时间轴不能往回跳
（图表库要求时间严格递增，否则整张图不画），切换前后的标注都要准确。"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("fastapi")

from tickersentinel.barstore import BarStore  # noqa: E402
from tickersentinel.market_calendar import MarketCalendar  # noqa: E402
from tickersentinel.web.alerts_chart import drop_repeated_hour, shift  # noqa: E402
from tickersentinel.web import macro_data as md  # noqa: E402

CT, UTC = ZoneInfo("America/Chicago"), timezone.utc


def test_shift_keeps_wall_clock_on_both_sides_and_drop_repeated_hour():
    before = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)  # 01:30 CDT
    after = datetime(2026, 11, 1, 7, 30, tzinfo=UTC)  # 01:30 CST
    assert shift(before, CT) == shift(after, CT)  # 本地时间相同：第二遍往回跳
    noon_fri = datetime(2026, 10, 30, 17, 0, tzinfo=UTC)  # 12:00 CDT
    noon_mon = datetime(2026, 11, 2, 18, 0, tzinfo=UTC)  # 12:00 CST
    assert shift(noon_fri, CT) % 86400 == 12 * 3600 and shift(noon_mon, CT) % 86400 == 12 * 3600  # 两边都标 12:00
    pts = [(shift(datetime(2026, 11, 1, 5, 0, tzinfo=UTC) + timedelta(minutes=m), CT), m) for m in range(0, 240, 15)]
    out = drop_repeated_hour(pts)
    ts = [p[0] for p in out]
    assert all(b > a for a, b in zip(ts, ts[1:])) and len(out) == len(pts) - 4  # 重复的那一小时（4 个 15 分钟点）跳过


def test_macro_series_across_dst_is_strictly_increasing(settings):
    store = BarStore(":memory:")
    idx = pd.date_range(datetime(2026, 10, 31, 22, 0, tzinfo=CT), datetime(2026, 11, 1, 4, 0, tzinfo=CT), freq="1min",
                        inclusive="left").tz_convert(UTC)
    store.upsert("BTC-USD", pd.DataFrame({"close": np.linspace(70000, 71000, len(idx))}, index=idx))
    start, end = idx[30].to_pydatetime(), idx[-1].to_pydatetime()
    pay = md.series_payload(store, settings, MarketCalendar("XNYS"), start, end)
    btc = [it for g in pay["groups"] for it in g["items"] if it.get("symbol") == "BTC-USD" or it["key"] == "btc"]
    assert btc and btc[0]["points"]
    ts = [p[0] for p in btc[0]["points"]]
    assert all(b > a for a, b in zip(ts, ts[1:]))
