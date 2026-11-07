"""本地分钟线库（决策 21）：tsen run 每隔两分钟把宏观变量、持仓、基准、同业、夜间信号的分钟线存下来（jobs/collect.py），
网页的宏观页、总览的小走势线、异动详情的走势图都读这里——不用每次去 Yahoo 现取，也不受“1 分钟线只保留约 30 天”的限制。

单独一个库文件（storage.bars_path，默认 data/bars.sqlite3），不放主库：tsen ask / tsen web --preview 会把主库整份复制进内存，
分钟线一年几百 MB，放进去会拖慢它们。WAL：tsen run 写、单独运行的 tsen web 同时读。

表：
- bars：1 分钟线，(symbol, ts) 主键，ts = K 线开始时刻的 UTC 秒。同一分钟再写一次就覆盖（Yahoo 会修正最近几根，当前这一根也还没走完）。
- bars_5m：超过 keep_minute_days 的 1 分钟线压成 5 分钟线（compact），长期保留；读取时两张表自动拼起来。
- treasury_curve：财政部每日收益率曲线（2Y / 5Y / 10Y / 30Y），画日线用。
"""

from __future__ import annotations

import logging
import math
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd

from tickersentinel.timeutil import UTC

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    symbol TEXT NOT NULL,
    ts INTEGER NOT NULL,
    open REAL, high REAL, low REAL, close REAL NOT NULL, volume REAL,
    source TEXT,
    PRIMARY KEY (symbol, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS bars_5m (
    symbol TEXT NOT NULL,
    ts INTEGER NOT NULL,
    open REAL, high REAL, low REAL, close REAL NOT NULL, volume REAL,
    source TEXT,
    PRIMARY KEY (symbol, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS treasury_curve (
    day TEXT NOT NULL,
    tenor TEXT NOT NULL,
    yield REAL NOT NULL,
    PRIMARY KEY (day, tenor)
) WITHOUT ROWID;
"""

COLUMNS = ("open", "high", "low", "close", "volume")
FIVE_MIN = 300


@dataclass
class Coverage:
    symbol: str
    first: datetime | None
    last: datetime | None
    rows: int  # 1 分钟线 + 5 分钟线的行数
    rows_5m: int = 0


def _ts(t: datetime) -> int:
    if t.tzinfo is None:
        raise ValueError("时间要带时区")
    return int(t.timestamp())


def _dt(ts: int | None) -> datetime | None:
    return None if ts is None else datetime.fromtimestamp(int(ts), tz=UTC)


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


class BarStore:
    def __init__(self, path: str | Path, *, readonly: bool = False):
        """path=":memory:" 用于测试；readonly=True 只读打开（预览：看得到正式库的数据，写入会报错）。"""
        self.path = str(path)
        self.readonly = readonly
        self._lock = threading.Lock()
        if readonly:
            self.conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True, check_same_thread=False)
        else:
            if self.path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path, check_same_thread=False)
            if self.path != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA busy_timeout=10000")  # tsen run 和 tsen bars --backfill 同时写时等一等，不直接报 locked
            self.conn.executescript(_SCHEMA)
            self.conn.commit()

    @classmethod
    def open_readonly(cls, path: str | Path) -> "BarStore":
        """预览 / dry_run：库存在就只读打开，不存在（还没跑过 tsen run）就给一个空的内存库。"""
        if Path(path).exists():
            try:
                return cls(path, readonly=True)
            except sqlite3.Error as exc:
                log.warning("分钟线库只读打开失败，改用空库：%s", exc)
        return cls(":memory:")

    def close(self) -> None:
        self.conn.close()

    # ───────── 写 ─────────

    def upsert(self, symbol: str, df: pd.DataFrame | None, source: str | None = None) -> int:
        """写入一个代码的分钟线（索引带时区；列 open / high / low / close / volume，缺的列记空）。返回写入的行数。"""
        if df is None or df.empty or "close" not in df.columns:
            return 0
        idx = pd.DatetimeIndex(df.index)
        if idx.tz is None:
            raise ValueError("分钟线的时间索引要带时区")
        # 不用 asi8 // 1e9：pandas 3 的时间戳默认是微秒，单位随来源而变
        secs = ((idx.tz_convert(UTC) - pd.Timestamp(0, tz=UTC)) // pd.Timedelta(seconds=1)).tolist()
        cols = {c: (df[c].tolist() if c in df.columns else [None] * len(df)) for c in COLUMNS}
        rows = []
        for i, ts in enumerate(secs):
            close = _num(cols["close"][i])
            if close is None:
                continue
            rows.append((symbol, int(ts) // 60 * 60, _num(cols["open"][i]), _num(cols["high"][i]), _num(cols["low"][i]),
                         close, _num(cols["volume"][i]), source))
        if not rows:
            return 0
        with self._lock:
            self.conn.executemany("INSERT OR REPLACE INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
            self.conn.commit()
        return len(rows)

    def compact(self, before: datetime) -> int:
        """把 before 之前的 1 分钟线压成 5 分钟线（开 = 第一根、高低 = 极值、收 = 最后一根、量 = 合计），删掉原来的。返回压掉的行数。"""
        cut = _ts(before) // FIVE_MIN * FIVE_MIN  # 对齐到 5 分钟，不把一个 5 分钟桶劈成两半
        total = 0
        with self._lock:
            symbols = [r[0] for r in self.conn.execute("SELECT DISTINCT symbol FROM bars WHERE ts < ?", (cut,))]
            for sym in symbols:
                rows = self.conn.execute(
                    "SELECT ts, open, high, low, close, volume, source FROM bars WHERE symbol = ? AND ts < ? ORDER BY ts",
                    (sym, cut)).fetchall()
                df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "source"])
                df["bucket"] = df["ts"] // FIVE_MIN * FIVE_MIN
                g = df.groupby("bucket", sort=True)
                agg = pd.DataFrame({
                    "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                    "close": g["close"].last(), "volume": g["volume"].sum(min_count=1), "source": g["source"].last(),
                })
                out = [(sym, int(b), _num(r.open), _num(r.high), _num(r.low), float(r.close), _num(r.volume), r.source)
                       for b, r in agg.iterrows()]
                self.conn.executemany("INSERT OR REPLACE INTO bars_5m VALUES (?, ?, ?, ?, ?, ?, ?, ?)", out)
                self.conn.execute("DELETE FROM bars WHERE symbol = ? AND ts < ?", (sym, cut))
                total += len(rows)
            self.conn.commit()
        return total

    def upsert_curve(self, rows: Iterable[tuple[date, dict[str, float]]]) -> int:
        data = [(d.isoformat(), tenor, float(v)) for d, ylds in rows for tenor, v in ylds.items() if _num(v) is not None]
        if not data:
            return 0
        with self._lock:
            self.conn.executemany("INSERT OR REPLACE INTO treasury_curve VALUES (?, ?, ?)", data)
            self.conn.commit()
        return len(data)

    # ───────── 读 ─────────

    def series(self, symbols: Iterable[str], start: datetime, end: datetime | None = None) -> dict[str, pd.DataFrame]:
        """[start, end] 内的 K 线，格式同 PriceSource.intraday_bars（UTC 索引，列 open / high / low / close / volume）。
        早于压缩边界的部分是 5 分钟线，两张表拼在一起；没有数据的代码不出现在结果里。"""
        lo = _ts(start)
        hi = _ts(end) if end is not None else 2**62
        out: dict[str, pd.DataFrame] = {}
        with self._lock:
            for sym in dict.fromkeys(symbols):
                rows = self.conn.execute(
                    "SELECT ts, open, high, low, close, volume FROM bars_5m WHERE symbol = ? AND ts BETWEEN ? AND ? "
                    "UNION ALL SELECT ts, open, high, low, close, volume FROM bars WHERE symbol = ? AND ts BETWEEN ? AND ? "
                    "ORDER BY ts", (sym, lo, hi, sym, lo, hi)).fetchall()
                if not rows:
                    continue
                df = pd.DataFrame(rows, columns=["ts", *COLUMNS])
                df.index = pd.to_datetime(df.pop("ts"), unit="s", utc=True)
                df.index.name = None
                df = df[~df.index.duplicated(keep="last")]  # 1 分钟线排在后面，重叠时以它为准
                out[sym] = df.astype(float)
        return out

    def last_ts(self, symbol: str) -> datetime | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT max(t) FROM (SELECT max(ts) AS t FROM bars WHERE symbol = ? UNION ALL "
                "SELECT max(ts) FROM bars_5m WHERE symbol = ?)", (symbol, symbol)).fetchone()
        return _dt(row[0]) if row else None

    def covers(self, symbol: str, start: datetime, end: datetime, *, slack: timedelta = timedelta(minutes=15)) -> bool:
        """库里这个代码的数据是否盖住了 [start, end]（两头各留 slack：开头可能刚好不在交易、最后几分钟还没采到）。"""
        cov = self.coverage(symbol, count=False)
        return (cov.first is not None and cov.last is not None
                and cov.first <= start + slack and cov.last >= end - slack)

    def coverage(self, symbol: str, *, count: bool = True) -> Coverage:
        with self._lock:
            first = last = None
            n = n5 = 0
            for table in ("bars", "bars_5m"):
                a, b = self.conn.execute(f"SELECT min(ts), max(ts) FROM {table} WHERE symbol = ?", (symbol,)).fetchone()
                if a is not None:
                    first = a if first is None else min(first, a)
                    last = b if last is None else max(last, b)
                if count:
                    c = self.conn.execute(f"SELECT count(*) FROM {table} WHERE symbol = ?", (symbol,)).fetchone()[0]
                    n += c
                    if table == "bars_5m":
                        n5 = c
        return Coverage(symbol, _dt(first), _dt(last), n, n5)

    def symbols(self) -> list[str]:
        with self._lock:
            rows = self.conn.execute("SELECT symbol FROM (SELECT DISTINCT symbol FROM bars UNION "
                                     "SELECT DISTINCT symbol FROM bars_5m) ORDER BY symbol").fetchall()
        return [r[0] for r in rows]

    def curve(self, since: date) -> pd.DataFrame:
        """财政部收益率曲线：行 = 日期，列 = 期限（2Y / 5Y / 10Y / 30Y），单位 %。"""
        with self._lock:
            rows = self.conn.execute("SELECT day, tenor, yield FROM treasury_curve WHERE day >= ? ORDER BY day",
                                     (since.isoformat(),)).fetchall()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows, columns=["day", "tenor", "yield"]).pivot(index="day", columns="tenor", values="yield")
        df.index = pd.to_datetime(df.index).date
        return df.sort_index()
