"""本地分钟线库（barstore.py）：写入覆盖、读取格式、压缩成 5 分钟线、跨压缩边界读取、只读打开、财政部曲线。"""

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from tickersentinel.barstore import BarStore
from tickersentinel.sources.macro import parse_treasury_csv, parse_treasury_rows
from tickersentinel.timeutil import UTC

T0 = datetime(2026, 10, 26, 14, 0, tzinfo=UTC)


def bars(n: int, start: datetime = T0, base: float = 100.0, step: float = 0.1) -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq="1min", tz=UTC)
    close = base + step * np.arange(n)
    return pd.DataFrame({"open": close - 0.05, "high": close + 0.2, "low": close - 0.3, "close": close,
                         "volume": np.full(n, 10.0)}, index=idx)


def test_upsert_and_series_roundtrip():
    st = BarStore(":memory:")
    assert st.upsert("MU", bars(30), "yfinance") == 30
    out = st.series(["MU", "NOPE"], T0, T0 + timedelta(minutes=29))
    assert list(out) == ["MU"]
    df = out["MU"]
    assert len(df) == 30 and list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert str(df.index.tz) == "UTC" and df.index[0] == pd.Timestamp(T0)
    assert df["close"].iloc[-1] == pytest.approx(102.9)
    # 区间是闭区间，按时间截取
    assert len(st.series(["MU"], T0 + timedelta(minutes=10), T0 + timedelta(minutes=19))["MU"]) == 10


def test_upsert_overwrites_same_minute_and_skips_bad_rows():
    st = BarStore(":memory:")
    st.upsert("MU", bars(5), "yfinance")
    fixed = bars(1, start=T0 + timedelta(minutes=4), base=200.0)
    fixed.index = fixed.index + pd.Timedelta(seconds=30)  # 秒数不整齐也落到同一分钟
    st.upsert("MU", fixed, "yfinance")
    df = st.series(["MU"], T0, T0 + timedelta(minutes=10))["MU"]
    assert len(df) == 5 and df["close"].iloc[-1] == 200.0
    bad = bars(3, start=T0 + timedelta(minutes=10))
    bad.loc[bad.index[1], "close"] = np.nan
    assert st.upsert("MU", bad) == 2
    assert st.upsert("MU", pd.DataFrame()) == 0


def test_upsert_requires_timezone():
    st = BarStore(":memory:")
    df = bars(3)
    df.index = df.index.tz_localize(None)
    with pytest.raises(ValueError):
        st.upsert("MU", df)


def test_compact_aggregates_ohlcv_and_series_spans_both_tables():
    st = BarStore(":memory:")
    st.upsert("CL=F", bars(20), "yfinance")  # 14:00–14:19
    moved = st.compact(T0 + timedelta(minutes=12))  # 对齐到 14:10：前 10 根压成两根 5 分钟线
    assert moved == 10
    cov = st.coverage("CL=F")
    assert cov.rows == 12 and cov.rows_5m == 2
    assert cov.first == T0 and cov.last == T0 + timedelta(minutes=19)
    df = st.series(["CL=F"], T0, T0 + timedelta(hours=1))["CL=F"]
    assert len(df) == 12
    first = df.iloc[0]  # 14:00–14:04
    src = bars(20)
    assert first["open"] == pytest.approx(src["open"].iloc[0])
    assert first["close"] == pytest.approx(src["close"].iloc[4])
    assert first["high"] == pytest.approx(src["high"].iloc[:5].max())
    assert first["low"] == pytest.approx(src["low"].iloc[:5].min())
    assert first["volume"] == pytest.approx(50.0)
    assert df.index[2] == pd.Timestamp(T0 + timedelta(minutes=10))  # 之后是原来的 1 分钟线
    # 再压一次不会重复计数
    assert st.compact(T0 + timedelta(minutes=12)) == 0


def test_last_ts_covers_and_symbols():
    st = BarStore(":memory:")
    assert st.last_ts("MU") is None
    st.upsert("MU", bars(60))
    st.upsert("^TNX", bars(5))
    assert st.last_ts("MU") == T0 + timedelta(minutes=59)
    assert st.covers("MU", T0, T0 + timedelta(minutes=59))
    assert st.covers("MU", T0 + timedelta(minutes=10), T0 + timedelta(minutes=70))  # 最后 15 分钟还没采到也算
    assert not st.covers("MU", T0 - timedelta(hours=1), T0 + timedelta(minutes=59))
    assert not st.covers("SNDK", T0, T0 + timedelta(minutes=5))
    assert st.symbols() == ["MU", "^TNX"]


def test_readonly_open(tmp_path):
    path = tmp_path / "bars.sqlite3"
    w = BarStore(path)
    w.upsert("MU", bars(3))
    ro = BarStore.open_readonly(path)
    assert ro.readonly
    assert len(ro.series(["MU"], T0, T0 + timedelta(minutes=5))["MU"]) == 3
    with pytest.raises(Exception):
        ro.upsert("MU", bars(1, start=T0 + timedelta(minutes=10)))
    ro.close()
    w.close()
    empty = BarStore.open_readonly(tmp_path / "missing.sqlite3")
    assert empty.symbols() == [] and not (tmp_path / "missing.sqlite3").exists()


CSV = (
    'Date,"1 Mo","2 Yr","5 Yr","10 Yr","30 Yr"\n'
    "10/29/2026,4.10,3.60,3.70,4.05,4.60\n"
    "10/28/2026,4.11,3.62,3.71,4.08,4.62\n"
    "bad,1,2,3,4,5\n"
)


def test_treasury_rows_and_curve_table():
    rows = parse_treasury_rows(CSV)
    assert [r.day for r in rows] == [date(2026, 10, 29), date(2026, 10, 28)]
    assert parse_treasury_csv(CSV).yields["10Y"] == 4.05
    st = BarStore(":memory:")
    assert st.upsert_curve((r.day, r.yields) for r in rows) == 8
    df = st.curve(date(2026, 10, 1))
    assert list(df.index) == [date(2026, 10, 28), date(2026, 10, 29)]
    assert df.loc[date(2026, 10, 29), "2Y"] == 3.60
    assert st.curve(date(2026, 11, 1)).empty
