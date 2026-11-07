"""总览页的消息流：和飞书里收到的一样（messages 表），从晚到早；归因挂在对应的异动下面。

- 一条消息 → 一张卡片（FeedItem）。归因、跟帖（reply_to 指向页面上的某条消息，或归因的 alert_ids 和某条异动重合）
  放进那张卡片的 thread 里，按时间先后。
- 历史记录：messages 表是新加的，老用户一开始是空的。消息不够一页时，用 alerts 表（每条异动按 render_alert 重排一遍，
  归因原文挂在下面）和 briefs 表补上更早的部分，只取最早一条消息之前的，标“历史记录”。
- 翻页游标：先按消息 id 往前翻（before_id）；消息翻完了再按时间翻历史记录（before_ts，UTC 的 ISO 时间）。
- 历史记录里的跟帖（attribution_json 的 same_wave_as）挂在同一波第一条的下面；第一条不在这一页时，归因换成那一条的结论并给出链接。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from tickersentinel.storage import Storage
from tickersentinel.web.common import to_dt

log = logging.getLogger(__name__)

PAGE_SIZE = 30
BOUNDARY_SCAN = 200  # 判断“历史记录是否已经在消息里”时，看最早的这么多条消息

KIND_NAMES = {"alert": "异动", "attribution": "归因", "brief": "简报", "events": "事件日历", "macro": "宏观",
              "info": "通知", "error": "出错"}
BRIEF_NAMES = {"premarket": "盘前简报", "close": "收盘简报", "weekly": "周报"}


@dataclass
class AlertRef:
    id: int
    symbol: str
    direction: int


@dataclass
class FeedItem:
    key: str  # 页面里的唯一标识：m<消息 id> / a<异动 id> / b<简报 id>
    kind: str
    title: str
    body: str
    at: datetime | None
    id: int | None = None  # 消息 id（历史记录没有）
    history: bool = False
    channel: str | None = None
    message_id: str | None = None
    reply_to: str | None = None
    alert_ids: list[int] = field(default_factory=list)
    alerts: list[AlertRef] = field(default_factory=list)
    ok: bool = True
    error: str | None = None
    sub: str | None = None  # 简报种类等
    lead_id: int | None = None  # 跟帖（同一波行情）的第一条异动 id（历史记录里从 attribution_json 读出）
    thread: list["FeedItem"] = field(default_factory=list)

    @property
    def direction(self) -> int:
        """异动的方向：先看关联的异动记录，再看标题里的 🔺 / 🔻。"""
        for a in self.alerts:
            if a.direction:
                return 1 if a.direction > 0 else -1
        t = self.title or ""
        if "🔺" in t[:3]:
            return 1
        if "🔻" in t[:3]:
            return -1
        return 0

    @property
    def clean_title(self) -> str:
        """标题去掉开头的 🔺 / 🔻 / 🧭（卡片自己画箭头和图标）。"""
        t = (self.title or "").strip()
        for mark in ("🔺", "🔻", "🧭"):
            if t.startswith(mark):
                t = t[len(mark):].strip()
        return t or KIND_NAMES.get(self.kind, "消息")

    @property
    def parts(self) -> tuple[str, str]:
        """(正文, 宏观快照)：异动推送末尾那段宏观数据单独折叠起来。已经有归因挂在下面时去掉“正在检索”那一行。"""
        body = self.body or ""
        main, sep, rest = body.partition("\n🌐")
        extra = ("🌐" + rest).strip() if sep else ""
        if self.thread or self.history:
            main = "\n".join(ln for ln in main.splitlines() if not ln.strip().startswith("⏳"))
        return main.strip(), extra

    @property
    def preview(self) -> str:
        """折叠时显示的头一两行（纯文字）。"""
        from tickersentinel.web.markdown import strip_md

        lines = [ln.strip() for ln in strip_md(self.body or "").splitlines() if ln.strip()]
        text = " · ".join(lines[:2])
        return text if len(text) <= 160 else text[:159] + "…"

    @property
    def kind_name(self) -> str:
        if self.kind == "brief" and self.sub:
            return BRIEF_NAMES.get(self.sub, "简报")
        return KIND_NAMES.get(self.kind, self.kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "id": self.id, "kind": self.kind, "title": self.title, "body": self.body,
            "at": self.at.isoformat() if self.at else None, "history": self.history, "channel": self.channel,
            "message_id": self.message_id, "reply_to": self.reply_to, "alert_ids": self.alert_ids,
            "alerts": [{"id": a.id, "symbol": a.symbol, "direction": a.direction} for a in self.alerts],
            "ok": self.ok, "error": self.error, "lead_id": self.lead_id, "thread": [t.to_dict() for t in self.thread],
        }


def parse_ids(raw: Any) -> list[int]:
    out: list[int] = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


def _alert_refs(storage: Storage, ids: list[int]) -> list[AlertRef]:
    refs = []
    for i in ids[:20]:
        try:
            row = storage.alert_row(i)
        except Exception:  # noqa: BLE001 - 坏数据不影响整页
            row = None
        if row:
            try:
                d = int(row.get("direction") or 0)
            except (TypeError, ValueError):
                d = 0
            refs.append(AlertRef(i, str(row.get("symbol") or ""), d))
    return refs


def item_from_message(storage: Storage, row: dict) -> FeedItem:
    ids = parse_ids(row.get("alert_ids"))
    return FeedItem(
        key=f"m{row['id']}", id=int(row["id"]), kind=str(row.get("kind") or "info"), title=row.get("title") or "",
        body=row.get("body") or "", at=to_dt(row.get("sent_at")), channel=row.get("channel"),
        message_id=row.get("message_id") or None, reply_to=row.get("reply_to") or None, alert_ids=ids,
        alerts=_alert_refs(storage, ids), ok=bool(row.get("ok", 1)), error=row.get("error"),
    )


def thread_items(items: list[FeedItem]) -> list[FeedItem]:
    """把回复（归因、跟帖）挂到页面上它所回复的那张卡片下面。items 从晚到早，返回的根卡片也从晚到早。"""
    roots: list[FeedItem] = []
    by_mid: dict[str, FeedItem] = {}
    by_alert: dict[int, FeedItem] = {}
    for it in sorted(items, key=lambda x: (_sort_at(x), x.id or 0)):
        parent = None
        if it.reply_to and it.reply_to in by_mid:
            parent = by_mid[it.reply_to]
        elif it.kind == "alert" and it.lead_id is not None and it.lead_id in by_alert:
            parent = by_alert[it.lead_id]  # 历史记录里的跟帖：挂在同一波的第一条下面
        elif it.kind == "attribution":
            parent = next((by_alert[i] for i in it.alert_ids if i in by_alert), None)
        if parent is not None:
            parent.thread.append(it)
            target = parent
        else:
            roots.append(it)
            target = it
        if it.message_id:
            by_mid.setdefault(it.message_id, target)
        if it.kind == "alert":
            for i in it.alert_ids:
                by_alert.setdefault(i, target)
    roots.reverse()
    return roots


def _sort_at(it: FeedItem) -> float:
    return it.at.timestamp() if it.at else 0.0


# ───────── 历史记录（messages 表出现之前的） ─────────


def _lead_of(row: dict) -> int | None:
    """跟帖（同一波行情）指向的第一条异动 id：attribution_json 里的 same_wave_as。"""
    raw = row.get("attribution_json")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        lead = int(data.get("same_wave_as")) if isinstance(data, dict) and data.get("same_wave_as") else None
    except (TypeError, ValueError):
        return None
    return lead if lead and lead != row.get("id") else None


def _history_alert(settings, row: dict, storage: Storage | None = None) -> FeedItem | None:
    from tickersentinel.agents.render import render_alert

    try:
        alert = Storage._row_to_alert(row)  # type: ignore[arg-type]
        msg = render_alert(alert, settings, attribution_pending=False, macro=None)
    except Exception as exc:  # noqa: BLE001 - 旧记录格式不对：跳过这一条，不让整页出错
        log.debug("历史异动 %s 排版失败：%s", row.get("id"), exc)
        return None
    try:
        d = int(row.get("direction") or alert.direction)
    except (TypeError, ValueError):
        d = alert.direction
    at = to_dt(row.get("created_at")) or to_dt(row.get("ts"))
    lead = _lead_of(row)
    item = FeedItem(key=f"a{row['id']}", kind="alert", title=msg.title, body=msg.body, at=at, history=True,
                    message_id=row.get("message_id") or None, alert_ids=[int(row["id"])],
                    alerts=[AlertRef(int(row["id"]), str(row.get("symbol") or ""), d)], lead_id=lead)
    text = (row.get("attribution_text") or "").strip()
    if lead is not None and storage is not None:
        # 跟帖的归因原文只有一句“原因见那一条的归因”：换成那一条的结论，并给出链接
        try:
            text = storage.attribution_summary(int(row["id"])) or text
        except Exception:  # noqa: BLE001
            pass
    if text:
        item.thread.append(FeedItem(key=f"a{row['id']}r", kind="attribution", title="归因", body=text, at=at,
                                    history=True, alert_ids=[int(row["id"])], lead_id=lead))
    return item


def _history_brief(row: dict) -> FeedItem:
    return FeedItem(key=f"b{row['id']}", kind="brief", title=row.get("title") or "", body=row.get("body") or "",
                    at=to_dt(row.get("created_at")), history=True, message_id=row.get("message_id") or None,
                    sub=row.get("kind"))


def _query(storage: Storage, sql: str, params: tuple) -> list[dict]:
    with storage._lock:  # noqa: SLF001 - 只读查询；Storage 还没有“某时刻之前的 N 条”这种接口
        rows = storage.conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def history_items(ctx, before: datetime | None, limit: int, *, exclude_alerts: set[int] = frozenset(),
                  exclude_mids: set[str] = frozenset()) -> tuple[list[FeedItem], str | None]:
    """before 之前的历史异动和简报，从晚到早，最多 limit 条；返回 (这一页, 下一页的游标)，没有更早的了游标为 None。

    游标是这一页最后“看过”的那条原始记录的 created_at——跳过的（已经在消息里的、排版失败的坏记录）也算看过，
    所以一页里有坏记录时照样能往前翻，不会让更早的记录再也翻不到。
    """
    if limit <= 0:
        return [], None
    storage: Storage = ctx.storage
    cut = before.astimezone(UTC).isoformat() if before else "9999"  # 和库里一样按 UTC 的 ISO 字符串比较
    n_alerts = limit + 1 + len(exclude_alerts)
    n_briefs = limit + 1 + len(exclude_mids)
    a_cols, b_cols = "*", "id, kind, created_at, title, body, message_id"
    try:
        alerts = _query(storage, f"SELECT {a_cols} FROM alerts WHERE created_at < ? ORDER BY created_at DESC, id DESC"
                                 " LIMIT ?", (cut, n_alerts))
        briefs = _query(storage, f"SELECT {b_cols} FROM briefs WHERE created_at < ? ORDER BY created_at DESC, id DESC"
                                 " LIMIT ?", (cut, n_briefs))
        # 某一路取满了（后面可能还有）：把和它最后一条同一时刻的也都取来——游标按时刻切，同一时刻的不能一半在这页一半丢掉
        for rows, n, table, cols in ((alerts, n_alerts, "alerts", a_cols), (briefs, n_briefs, "briefs", b_cols)):
            if len(rows) >= n:
                have = {r["id"] for r in rows}
                rows += [r for r in _query(storage, f"SELECT {cols} FROM {table} WHERE created_at = ?",
                                           (rows[-1]["created_at"],)) if r["id"] not in have]
    except Exception as exc:  # noqa: BLE001
        log.warning("读取历史记录失败：%s", exc)
        return [], None
    # 合并后只有不早于“取满了的那一路的最后一条”的部分次序可靠
    floor = max((str(rs[min(n, len(rs)) - 1]["created_at"]) for rs, n in ((alerts, n_alerts), (briefs, n_briefs))
                 if len(rs) >= n), default="")
    merged = sorted([("a", r) for r in alerts] + [("b", r) for r in briefs],
                    key=lambda x: (str(x[1]["created_at"]), x[0], x[1]["id"]), reverse=True)
    items: list[FeedItem] = []
    last: str | None = None
    used = 0
    for kind, r in merged:
        at = str(r["created_at"])
        if at < floor or (len(items) >= limit and at != last):
            break
        used += 1
        last = at
        if r.get("message_id") and r["message_id"] in exclude_mids:
            continue
        if kind == "a":
            if r["id"] in exclude_alerts:
                continue
            if (it := _history_alert(ctx.settings, r, storage)) is not None:
                items.append(it)
        else:
            items.append(_history_brief(r))
    more = used < len(merged) or bool(floor)
    return items, (last if more else None)


@dataclass
class FeedPage:
    items: list[FeedItem]  # 根卡片，从晚到早
    raw_count: int  # 这一页的条数（挂进 thread 之前）
    next_before_id: int | None = None
    next_before_ts: str | None = None

    @property
    def has_more(self) -> bool:
        return self.next_before_id is not None or self.next_before_ts is not None


def _message_bounds(storage: Storage) -> tuple[datetime | None, set[int], set[str]]:
    """最早一条消息的时间，以及最早那一批消息里已经出现过的异动 id 和飞书消息 id（历史记录里不再重复列出）。"""
    first = storage.messages_after(0, BOUNDARY_SCAN)
    cutoff = to_dt(first[0]["sent_at"]) if first else None
    ex_alerts = {i for r in first for i in parse_ids(r.get("alert_ids"))}
    ex_mids = {r["message_id"] for r in first if r.get("message_id")}
    return cutoff, ex_alerts, ex_mids


def feed_page(ctx, *, before_id: int | None = None, before_ts: str | None = None, limit: int = PAGE_SIZE) -> FeedPage:
    storage: Storage = ctx.storage
    limit = max(1, min(int(limit), 100))
    items: list[FeedItem] = []
    if before_ts is None:
        rows = storage.recent_messages(limit, before_id=before_id)
        items = [item_from_message(storage, r) for r in rows]
        if len(rows) == limit:
            return FeedPage(thread_items(items), len(items), next_before_id=int(rows[-1]["id"]))
        # 消息翻完了：接着补历史记录（最早一条消息之前的）
        cutoff, ex_alerts, ex_mids = _message_bounds(storage)
        hist, next_ts = history_items(ctx, cutoff, limit - len(rows), exclude_alerts=ex_alerts, exclude_mids=ex_mids)
    else:
        cut = to_dt(before_ts)
        if cut is None:
            return FeedPage([], 0)
        cutoff, ex_alerts, ex_mids = _message_bounds(storage)
        if cutoff is not None and cut > cutoff:
            cut = cutoff  # 历史记录只到最早一条消息之前
        hist, next_ts = history_items(ctx, cut, limit, exclude_alerts=ex_alerts, exclude_mids=ex_mids)
    items += hist
    return FeedPage(thread_items(items), len(items), next_before_ts=next_ts)


def sse_event(item_id: int, payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False)
    return f"id: {item_id}\nevent: message\ndata: {data}\n\n"
