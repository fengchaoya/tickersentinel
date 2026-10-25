"""SQLite 存储：新闻去重记录、异动与归因日志、简报记录、事件日历、简单键值。

alerts 表同时是一个小型“事件研究”数据集：每次异动的特征 + 当时的归因，
以后可以补充人工标注（label / notes），回头评估归因准确率、校准阈值。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from tickersentinel.models import EVENT_SOURCE_RANK, Alert, Event, NewsItem, Trigger
from tickersentinel.timeutil import UTC, utcnow


def trigger_to_json(t: Trigger) -> dict[str, Any]:
    d = asdict(t)
    if t.anchor_ts is not None:
        d["anchor_ts"] = t.anchor_ts.isoformat()
    return d


def trigger_from_json(d: dict[str, Any]) -> Trigger:
    """旧记录没有起点等字段，用默认值；规则 level（旧版关口）原样保留。"""
    d = dict(d)
    if isinstance(d.get("anchor_ts"), str):
        d["anchor_ts"] = datetime.fromisoformat(d["anchor_ts"])
    known = Trigger.__dataclass_fields__
    return Trigger(**{k: v for k, v in d.items() if k in known})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS news_seen (
    key           TEXT PRIMARY KEY,
    title         TEXT,
    url           TEXT,
    source        TEXT,
    provider      TEXT,
    published_at  TEXT,
    symbols       TEXT,
    first_seen_at TEXT,
    context       TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    ts                  TEXT NOT NULL,
    session_day         TEXT NOT NULL,
    rule                TEXT NOT NULL,
    direction           INTEGER NOT NULL,
    price               REAL,
    prev_close          REAL,
    window_minutes      INTEGER,
    move_pct            REAL,
    benchmark_move_pct  REAL,
    beta                REAL,
    abnormal_move_pct   REAL,
    volume_ratio        REAL,
    peers_json          TEXT,
    triggers_json       TEXT NOT NULL,
    is_escalation       INTEGER NOT NULL DEFAULT 0,
    message_id          TEXT,
    attribution_text    TEXT,
    attribution_json    TEXT,
    label               TEXT,   -- 预留：事后人工标注的真实原因
    notes               TEXT,
    macro_json          TEXT,   -- 推送时的宏观快照（利率、油价、金银、大盘……），见 sources/macro.py
    segment             TEXT,   -- 发生在哪一段：regular / pre / post / cme / utc / exchange（见 detect/clock.py）
    benchmark           TEXT    -- 拆分用的板块基准（ETF 代码，或同业篮子 basket:<主题>，见 config/themes/）
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts (ts);
CREATE INDEX IF NOT EXISTS idx_alerts_day ON alerts (session_day, symbol);
CREATE TABLE IF NOT EXISTS briefs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    title       TEXT,
    body        TEXT,
    message_id  TEXT
);
CREATE TABLE IF NOT EXISTS events (
    key          TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    day          TEXT NOT NULL,
    at           TEXT,
    time_hint    TEXT,
    title        TEXT NOT NULL,
    confirmed    INTEGER NOT NULL DEFAULT 0,
    source       TEXT,
    url          TEXT,
    details_json TEXT,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    changed_at   TEXT NOT NULL,  -- 最近一次值得告诉用户的变化（新增 / 改期 / 由估计变为确认）的时间
    change       TEXT,           -- new / rescheduled / confirmed
    prev_day     TEXT            -- 改期前的日期
);
CREATE INDEX IF NOT EXISTS idx_events_day ON events (day);
CREATE TABLE IF NOT EXISTS chat_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT NOT NULL,
    role        TEXT NOT NULL,   -- user / assistant
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_messages ON chat_messages (chat_id, id);
CREATE TABLE IF NOT EXISTS kv (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  TEXT
);
-- 飞书同步的映射（见 tickersentinel/feishu）。按日历 / 数据表区分：重建日历或表格后旧映射自然失效
CREATE TABLE IF NOT EXISTS feishu_calendar_events (
    calendar_id   TEXT NOT NULL,
    event_key     TEXT NOT NULL,   -- events.key
    event_id      TEXT NOT NULL,   -- 飞书日程 ID
    content_hash  TEXT NOT NULL,   -- 上次写入的日程内容，变了才更新
    synced_at     TEXT NOT NULL,
    PRIMARY KEY (calendar_id, event_key)
);
CREATE TABLE IF NOT EXISTS feishu_bitable_rows (
    app_token     TEXT NOT NULL,
    table_id      TEXT NOT NULL,
    alert_id      INTEGER NOT NULL,  -- alerts.id
    record_id     TEXT NOT NULL,     -- 飞书多维表格的记录 ID
    content_hash  TEXT NOT NULL,     -- 上次写入的内容；'deleted' 表示你在飞书里删掉了这一行，不再写入
    synced_at     TEXT NOT NULL,
    PRIMARY KEY (app_token, table_id, alert_id)
);
"""


STALE_DAYS = 3  # 事件连续多少天没被原来源报告才算过时
_EPOCH = "1970-01-01T00:00:00+00:00"


def _retimed(row: sqlite3.Row, e: Event) -> bool:
    """同一天内时间有实质变化：精确时间相差超过 1 小时，或盘前 / 盘后 / 盘中互换。"""
    if row["at"] and e.at and abs((_from_iso(row["at"]) - e.at).total_seconds()) > 3600:
        return True
    return bool(row["time_hint"] and e.time_hint and row["time_hint"] != e.time_hint)


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat()


def _from_iso(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(UTC)


class Storage:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        if str(path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """给旧库补上后来新增的列（CREATE TABLE IF NOT EXISTS 不会改已有的表）。"""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(alerts)")}
        for col in ("macro_json", "segment", "benchmark"):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE alerts ADD COLUMN {col} TEXT")

    def set_alert_macro(self, alert_id: int, macro: dict) -> None:
        self._exec("UPDATE alerts SET macro_json = ? WHERE id = ?", (json.dumps(macro, ensure_ascii=False), alert_id))

    @classmethod
    def in_memory_copy(cls, path: str | Path) -> "Storage":
        """把正式库只读地复制进内存：预览时能读到历史数据，写入不落盘。库不存在时就是空的内存库。"""
        st = cls(":memory:")
        if Path(path).exists():
            src = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
            try:
                src.backup(st.conn)
            finally:
                src.close()
            st.conn.executescript(_SCHEMA)  # 旧库可能缺新加的表 / 列
            st._migrate()
            st.conn.commit()
        return st

    def close(self) -> None:
        self.conn.close()

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, tuple(params))
            self.conn.commit()
            return cur

    # ───────── 新闻去重 ─────────

    def unseen(self, items: list[NewsItem]) -> list[NewsItem]:
        if not items:
            return []
        keys = [i.key for i in items]
        seen: set[str] = set()
        with self._lock:
            for chunk_start in range(0, len(keys), 500):
                chunk = keys[chunk_start : chunk_start + 500]
                rows = self.conn.execute(
                    f"SELECT key FROM news_seen WHERE key IN ({','.join('?' * len(chunk))})", chunk
                ).fetchall()
                seen.update(r["key"] for r in rows)
        return [i for i in items if i.key not in seen]

    def mark_seen(self, items: list[NewsItem], context: str = "") -> None:
        now = _iso(utcnow())
        with self._lock:
            self.conn.executemany(
                "INSERT OR IGNORE INTO news_seen (key, title, url, source, provider, published_at, symbols, first_seen_at, context)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (i.key, i.title, i.url, i.source, i.provider, _iso(i.published_at), ",".join(i.symbols), now, context)
                    for i in items
                ],
            )
            self.conn.commit()

    # ───────── 异动 ─────────

    def record_alert(self, alert: Alert, session_day: str) -> int:
        cur = self._exec(
            "INSERT INTO alerts (created_at, symbol, ts, session_day, rule, direction, price, prev_close, window_minutes,"
            " move_pct, benchmark_move_pct, beta, abnormal_move_pct, volume_ratio, peers_json, triggers_json, is_escalation,"
            " segment, benchmark) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _iso(utcnow()),
                alert.symbol,
                _iso(alert.ts),
                session_day,
                alert.headline.rule,
                alert.direction,
                alert.price,
                alert.prev_close,
                alert.window_minutes,
                alert.move_pct,
                alert.benchmark_move_pct,
                alert.beta,
                alert.abnormal_move_pct,
                alert.volume_ratio,
                json.dumps(alert.peers_move_pct),
                json.dumps([trigger_to_json(t) for t in alert.triggers]),
                int(alert.is_escalation),
                alert.segment,
                alert.benchmark,
            ),
        )
        alert.id = int(cur.lastrowid)
        return alert.id

    def undelivered_alerts(self, session_day: str, since: datetime) -> list[Alert]:
        """推送失败、还没送达的异动（message_id 为空），只取 since 之后发生的。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM alerts WHERE session_day = ? AND message_id IS NULL AND ts >= ? ORDER BY ts",
                (session_day, _iso(since)),
            ).fetchall()
        return [self._row_to_alert(r) for r in rows]

    def set_alert_message_id(self, alert_id: int, message_id: str | None) -> None:
        self._exec("UPDATE alerts SET message_id = ? WHERE id = ?", (message_id, alert_id))

    def save_attribution(self, alert_id: int, text: str, data: dict | None) -> None:
        self._exec(
            "UPDATE alerts SET attribution_text = ?, attribution_json = ? WHERE id = ?",
            (text, json.dumps(data, ensure_ascii=False) if data is not None else None, alert_id),
        )

    def alerts_between(self, start: datetime, end: datetime) -> list[Alert]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM alerts WHERE ts >= ? AND ts < ? ORDER BY ts", (_iso(start), _iso(end))
            ).fetchall()
        return [self._row_to_alert(r) for r in rows]

    def alerts_for_day(self, session_day: str) -> list[Alert]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM alerts WHERE session_day = ? ORDER BY ts", (session_day,)
            ).fetchall()
        return [self._row_to_alert(r) for r in rows]

    def attribution_summary(self, alert_id: int, _depth: int = 0) -> str | None:
        """一句话归因。跟帖（同一波行情）取那一串第一条的归因；没有 JSON 的（降级归因、模型没按格式输出）取原文第一行。"""
        with self._lock:
            row = self.conn.execute(
                "SELECT attribution_json, attribution_text FROM alerts WHERE id = ?", (alert_id,)
            ).fetchone()
        if not row:
            return None
        if row["attribution_json"]:
            try:
                data = json.loads(row["attribution_json"])
            except ValueError:
                data = None
            if isinstance(data, dict):
                lead = data.get("same_wave_as")
                if lead and _depth < 2 and (s := self.attribution_summary(int(lead), _depth + 1)):
                    return f"同一波行情：{s}"
                if data.get("summary"):
                    return data["summary"]
        text = (row["attribution_text"] or "").strip()
        first = next((ln for ln in text.splitlines() if ln.strip()), "")
        return first.replace("**", "").strip() or None

    @staticmethod
    def _row_to_alert(r: sqlite3.Row) -> Alert:
        triggers = [trigger_from_json(t) for t in json.loads(r["triggers_json"])]
        return Alert(
            symbol=r["symbol"],
            ts=_from_iso(r["ts"]),
            price=r["price"],
            prev_close=r["prev_close"],
            triggers=triggers,
            window_minutes=r["window_minutes"],
            move_pct=r["move_pct"],
            benchmark_move_pct=r["benchmark_move_pct"],
            beta=r["beta"],
            abnormal_move_pct=r["abnormal_move_pct"],
            volume_ratio=r["volume_ratio"],
            peers_move_pct=json.loads(r["peers_json"] or "{}"),
            is_escalation=bool(r["is_escalation"]),
            segment=(r["segment"] if "segment" in r.keys() else None) or "regular",
            benchmark=r["benchmark"] if "benchmark" in r.keys() else None,
            id=r["id"],
        )

    # ───────── 简报 ─────────

    def record_brief(self, kind: str, title: str, body: str, message_id: str | None, at: datetime | None = None) -> int:
        cur = self._exec(
            "INSERT INTO briefs (kind, created_at, title, body, message_id) VALUES (?, ?, ?, ?, ?)",
            (kind, _iso(at or utcnow()), title, body, message_id),
        )
        return int(cur.lastrowid)

    def last_brief_at(self, kinds: tuple[str, ...]) -> datetime | None:
        """这几类简报里最近一份成功推送的时间。"""
        with self._lock:
            row = self.conn.execute(
                f"SELECT created_at FROM briefs WHERE kind IN ({','.join('?' * len(kinds))}) ORDER BY id DESC LIMIT 1",
                kinds,
            ).fetchone()
        return _from_iso(row["created_at"]) if row else None

    def recent_briefs(self, limit: int = 3) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT kind, created_at, title, body FROM briefs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ───────── 事件日历 ─────────

    def upsert_event(self, e: Event, now: datetime | None = None) -> str | None:
        """写入或更新一个事件，返回值得告诉用户的变化：new / rescheduled / retimed / confirmed，没有则 None。

        不提醒、只静默更新的情况（避免误报）：
        - 事件已经过去（Form 144 除外，它本身就是“刚发生的消息”）：例如发布后 Yahoo 补上实际时间、标成已发布；
        - 已确认的事件遇到估计日期：完全忽略（公司公告了日期后，yfinance 可能仍给出旧的估计）；
        - 可靠程度更低的来源给出了不同的日期 / 时间：忽略，除非库里那条已经 STALE_DAYS 天没有被原来源报告
          （例如 Finnhub 偶尔失败时 yfinance 的估计会临时“顶上来”，不能因此推送一次改期、恢复后再改回来）。
        """
        now = now or utcnow()
        now_s = _iso(now)
        with self._lock:
            row = self.conn.execute("SELECT * FROM events WHERE key = ?", (e.key,)).fetchone()
        values = (e.kind, e.symbol, e.day.isoformat(), _iso(e.at) if e.at else None, e.time_hint, e.title,
                  int(e.confirmed), e.source, e.url, json.dumps(e.details, ensure_ascii=False, default=str))
        past = (e.at < now) if e.at else (e.day < (now - timedelta(days=1)).date())
        quiet = past and e.kind != "insider_sale"
        if row is None:
            change = None if quiet else "new"
            self._exec(
                "INSERT INTO events (kind, symbol, day, at, time_hint, title, confirmed, source, url, details_json,"
                " key, first_seen, last_seen, changed_at, change, prev_day) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (*values, e.key, now_s, now_s, now_s if change else _EPOCH, change),
            )
            return change
        if row["confirmed"] and not e.confirmed:
            return None
        rank_old = EVENT_SOURCE_RANK.get(row["source"] or "", 9)
        rank_new = EVENT_SOURCE_RANK.get(e.source, 9)
        stale = _from_iso(row["last_seen"]) < now - timedelta(days=STALE_DAYS)
        if rank_new > rank_old and not e.confirmed and not stale:
            return None
        change = None
        if row["day"] != e.day.isoformat():
            change = "rescheduled"
        elif not row["confirmed"] and e.confirmed:
            change = "confirmed"
        elif _retimed(row, e):
            change = "retimed"
        if quiet:
            change = None
        self._exec(
            "UPDATE events SET kind=?, symbol=?, day=?, at=?, time_hint=?, title=?, confirmed=?, source=?, url=?,"
            " details_json=?, last_seen=?, changed_at=?, change=?, prev_day=? WHERE key=?",
            (*values, now_s, now_s if change else row["changed_at"], change or row["change"],
             row["day"] if change == "rescheduled" else row["prev_day"], e.key),
        )
        return change

    def events_between(self, start: date, end: date) -> list[Event]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE day >= ? AND day <= ? ORDER BY day, at", (start.isoformat(), end.isoformat())
            ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def events_changed_since(self, ts: datetime) -> list[tuple[Event, str, date | None]]:
        """(事件, 变化类型, 改期前日期)，按事件日期排序。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE change IS NOT NULL AND changed_at > ? ORDER BY day, at", (_iso(ts),)
            ).fetchall()
        return [(self._row_to_event(r), r["change"] or "new", date.fromisoformat(r["prev_day"]) if r["prev_day"] else None)
                for r in rows]

    def prune_stale_estimates(self, sources: list[str], now: datetime, from_day: date) -> int:
        """删掉这些来源已经 STALE_DAYS 天没再报告的“估计日期”事件（只管 from_day 及以后的）。
        例如估计日期从 12-30 改到 01-02 会换一个季度 key，旧的那条最终靠这里清掉。
        宽限几天是为了不被一两次临时失败（某个代码限流、超时）误删，恢复后又当成“新增”推送。"""
        if not sources:
            return 0
        cur = self._exec(
            f"DELETE FROM events WHERE confirmed = 0 AND source IN ({','.join('?' * len(sources))})"
            " AND last_seen < ? AND day >= ?",
            (*sources, _iso(now - timedelta(days=STALE_DAYS)), from_day.isoformat()),
        )
        return cur.rowcount

    def drop_superseded_estimates(self, confirmed: Event, window_days: int = 30) -> int:
        """同一代码已有确认的财报日时，删掉它前后 window_days 天内其他 key 的估计（跨季度时 key 会不同）。"""
        cur = self._exec(
            "DELETE FROM events WHERE kind = 'earnings' AND confirmed = 0 AND symbol = ? AND key != ?"
            " AND day >= ? AND day <= ?",
            (confirmed.symbol, confirmed.key, (confirmed.day - timedelta(days=window_days)).isoformat(),
             (confirmed.day + timedelta(days=window_days)).isoformat()),
        )
        return cur.rowcount

    def sync_manual_events(self, keep_keys: set[str]) -> int:
        """手工录入（source=config）的事件以配置为准：配置里删掉或改了 key 的，从库里删掉。"""
        with self._lock:
            rows = self.conn.execute("SELECT key FROM events WHERE source = 'config'").fetchall()
        drop = [r["key"] for r in rows if r["key"] not in keep_keys]
        for key in drop:
            self._exec("DELETE FROM events WHERE key = ?", (key,))
        return len(drop)

    @staticmethod
    def _row_to_event(r: sqlite3.Row) -> Event:
        return Event(
            kind=r["kind"], symbol=r["symbol"], day=date.fromisoformat(r["day"]), title=r["title"], key=r["key"],
            at=_from_iso(r["at"]) if r["at"] else None, time_hint=r["time_hint"] or "", confirmed=bool(r["confirmed"]),
            source=r["source"] or "", url=r["url"] or "", details=json.loads(r["details_json"] or "{}"),
        )

    # ───────── 对话记录 ─────────

    def add_chat_message(self, chat_id: str, role: str, text: str) -> None:
        self._exec("INSERT INTO chat_messages (chat_id, role, text, created_at) VALUES (?, ?, ?, ?)",
                   (chat_id, role, text, _iso(utcnow())))

    def recent_chat(self, chat_id: str, limit: int) -> list[tuple[str, str]]:
        """最近 limit 条消息 (role, text)，按时间正序。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT role, text FROM chat_messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?", (chat_id, limit)
            ).fetchall()
        return [(r["role"], r["text"]) for r in reversed(rows)]

    def clear_chat(self, chat_id: str) -> None:
        self._exec("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))

    # ───────── 键值 ─────────

    def kv_get(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        self._exec(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, _iso(utcnow())),
        )

    def kv_delete(self, key: str) -> None:
        self._exec("DELETE FROM kv WHERE key = ?", (key,))

    # ───────── 飞书同步（日历 / 多维表格） ─────────

    def event_keys(self) -> set[str]:
        """events 表里现有的全部 key（飞书日历据此删除已经不存在的事件）。"""
        with self._lock:
            rows = self.conn.execute("SELECT key FROM events").fetchall()
        return {r["key"] for r in rows}

    def feishu_calendar_map(self, calendar_id: str) -> dict[str, tuple[str, str]]:
        """事件 key → (飞书日程 ID, 内容哈希)。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT event_key, event_id, content_hash FROM feishu_calendar_events WHERE calendar_id = ?", (calendar_id,)
            ).fetchall()
        return {r["event_key"]: (r["event_id"], r["content_hash"]) for r in rows}

    def set_feishu_calendar_event(self, calendar_id: str, event_key: str, event_id: str, content_hash: str) -> None:
        self._exec(
            "INSERT INTO feishu_calendar_events (calendar_id, event_key, event_id, content_hash, synced_at)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT(calendar_id, event_key) DO UPDATE SET"
            " event_id = excluded.event_id, content_hash = excluded.content_hash, synced_at = excluded.synced_at",
            (calendar_id, event_key, event_id, content_hash, _iso(utcnow())),
        )

    def delete_feishu_calendar_event(self, calendar_id: str, event_key: str) -> None:
        self._exec("DELETE FROM feishu_calendar_events WHERE calendar_id = ? AND event_key = ?", (calendar_id, event_key))

    def feishu_bitable_map(self, app_token: str, table_id: str) -> dict[int, tuple[str, str]]:
        """alert id → (飞书记录 ID, 内容哈希)。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT alert_id, record_id, content_hash FROM feishu_bitable_rows WHERE app_token = ? AND table_id = ?",
                (app_token, table_id),
            ).fetchall()
        return {int(r["alert_id"]): (r["record_id"], r["content_hash"]) for r in rows}

    def feishu_bitable_synced_since(self, app_token: str, table_id: str, since: datetime) -> set[int]:
        """最近（since 之后）写入或更新过的行的 alert id。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT alert_id FROM feishu_bitable_rows WHERE app_token = ? AND table_id = ? AND synced_at >= ?",
                (app_token, table_id, _iso(since)),
            ).fetchall()
        return {int(r["alert_id"]) for r in rows}

    def set_feishu_bitable_row(self, app_token: str, table_id: str, alert_id: int, record_id: str,
                               content_hash: str) -> None:
        self._exec(
            "INSERT INTO feishu_bitable_rows (app_token, table_id, alert_id, record_id, content_hash, synced_at)"
            " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(app_token, table_id, alert_id) DO UPDATE SET"
            " record_id = excluded.record_id, content_hash = excluded.content_hash, synced_at = excluded.synced_at",
            (app_token, table_id, alert_id, record_id, content_hash, _iso(utcnow())),
        )

    def alert_rows(self, since: datetime | None = None) -> list[dict]:
        """alerts 表的原始行（含归因、message_id、label / notes），按时间排序；since 为空时取全部。
        用 Storage._row_to_alert(row) 可以还原成 Alert。"""
        with self._lock:
            if since is None:
                rows = self.conn.execute("SELECT * FROM alerts ORDER BY ts, id").fetchall()
            else:
                rows = self.conn.execute("SELECT * FROM alerts WHERE ts >= ? ORDER BY ts, id", (_iso(since),)).fetchall()
        return [dict(r) for r in rows]

    def alert_labels(self) -> dict[int, tuple[str | None, str | None]]:
        """alert id → (label, notes)。"""
        with self._lock:
            rows = self.conn.execute("SELECT id, label, notes FROM alerts").fetchall()
        return {int(r["id"]): (r["label"], r["notes"]) for r in rows}

    def set_alert_label(self, alert_id: int, label: str | None, notes: str | None) -> None:
        """事后人工标注（目前来自飞书多维表格里你填的两列）。"""
        self._exec("UPDATE alerts SET label = ?, notes = ? WHERE id = ?", (label, notes, alert_id))
