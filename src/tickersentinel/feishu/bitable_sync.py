"""异动记录（alerts 表）→ 飞书多维表格，以及把你在表格里填的标注拉回 alerts.label / alerts.notes。

alerts 表是一个事件研究数据集（CLAUDE.md 设计决策 7）：每次异动的特征 + 当时的归因，事后补上真实原因，
用来评估归因准确率、校准阈值。多维表格是它在手机上的“标注界面”：

- 应用以自己的身份建一份多维表格（默认「tickersentinel · 异动记录」）和一张数据表「异动」，把你加为协作者（full_access：
  可以编辑、加视图 / 筛选 / 仪表盘、再分享）。表格建在应用自己的云空间里，在飞书「云文档 → 与我共享」里能找到。
- 每条异动一行：入库（推送）后插入，归因完成、补发成功后更新。只更新程序生成的列，
  「label 真实原因」「notes 备注」两列只在插入时写一次（库里已有的标注），之后以你在飞书里填的非空值为准，
  pull_labels() 把它们拉回 alerts 表。飞书里的空单元格不覆盖库里已有的标注（列被改名 / 删掉后补建的空列
  不能清空标注）；想清空标注要在库里清。
- 映射（alert id ↔ 记录 ID + 内容哈希）在 feishu_bitable_rows 表；内容没变不写，重复同步不会重复插入。
  你在飞书里删掉的行，下次拉取标注时发现，记为 deleted，不再写回（不会“复活”）。刚写入 FRESH_ROW_GRACE 以内的行
  检索不到时先不判定（飞书检索可能有延迟，误判后这一行永远收不到归因和状态更新）。
- 同一张表不要并发写（飞书会报 1254291）：同步与拉取共用一把锁。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from tickersentinel.config import Settings, pseudo_label
from tickersentinel.feishu.client import BITABLE_GONE, RECORD_GONE, ALREADY_MEMBER, FeishuApi, FeishuError
from tickersentinel.storage import Storage
from tickersentinel.timeutil import utcnow

log = logging.getLogger(__name__)

APP_KEY = "feishu:bitable_app_token"
TABLE_KEY = "feishu:bitable_table_id"
URL_KEY = "feishu:bitable_url"
DEFAULT_TABLE_KEY = "feishu:bitable_default_table"  # 新建多维表格时自带的空表，建好我们的表后删掉
SHARED_KEY = "feishu:bitable_shared"
TABLE_NAME = "异动"
VIEW_NAME = "全部异动"
DELETED = "deleted"
FRESH_ROW_GRACE = timedelta(minutes=10)  # 刚写入的行检索不到时先不判定为“已删除”（飞书检索可能有延迟）
BATCH = 500
FIELD_GONE = {1254045}  # 字段不存在：列被删掉或改名了

# 字段类型（飞书多维表格）：1 多行文本、2 数字、3 单选、5 日期
TEXT, NUMBER, SELECT, DATE = 1, 2, 3, 5
F_TITLE, F_TIME, F_SYMBOL, F_RULE, F_DIRECTION = "异动", "时间", "标的", "规则", "方向"
F_TRIGGERS, F_PRICE, F_DAY, F_WINDOW, F_MOVE = "触发条件", "现价", "较参照价%", "窗口", "窗口涨跌%"
F_REF = "参照价"  # 较昨收 / 较今日收盘（盘后）/ 较上一交易时段（期货）……：上一列按它算
F_SECTOR, F_IDIO, F_VOLUME = "板块部分%", "个股特异%", "量比"
F_SUMMARY, F_DRIVER, F_DETAIL, F_STATUS = "归因结论", "驱动", "原因明细", "推送状态"
F_LABEL, F_NOTES, F_ID = "label 真实原因", "notes 备注", "alert_id"

RULE_NAMES = {"rapid": "急涨急跌", "day": "全天累计", "idio": "个股特异", "level": "关口"}  # level：旧记录
STATUS_SENT, STATUS_PENDING = "已推送", "未送达"


def _opts(*names: str) -> dict:
    return {"options": [{"name": n} for n in names]}


def field_specs(settings: Settings) -> list[dict]:
    """数据表的列。第一列是索引列（只能是文本 / 数字 / 日期等），用一句话标题。"""
    pct = {"formatter": "0.00"}
    return [
        {"field_name": F_TITLE, "type": TEXT},
        {"field_name": F_TIME, "type": DATE, "property": {"date_formatter": "yyyy-MM-dd HH:mm"}},
        {"field_name": F_SYMBOL, "type": SELECT, "property": _opts(*settings.universe.symbols)},
        {"field_name": F_RULE, "type": SELECT, "property": _opts(*RULE_NAMES.values())},
        {"field_name": F_DIRECTION, "type": SELECT, "property": _opts("涨", "跌")},
        {"field_name": F_TRIGGERS, "type": TEXT},
        {"field_name": F_PRICE, "type": NUMBER, "property": pct},
        {"field_name": F_DAY, "type": NUMBER, "property": pct},
        {"field_name": F_REF, "type": TEXT},
        {"field_name": F_WINDOW, "type": TEXT},
        {"field_name": F_MOVE, "type": NUMBER, "property": pct},
        {"field_name": F_SECTOR, "type": NUMBER, "property": pct},
        {"field_name": F_IDIO, "type": NUMBER, "property": pct},
        {"field_name": F_VOLUME, "type": NUMBER, "property": {"formatter": "0.0"}},
        {"field_name": F_SUMMARY, "type": TEXT},
        {"field_name": F_DRIVER, "type": SELECT, "property": _opts("个股", "板块", "宏观", "不明")},
        {"field_name": F_DETAIL, "type": TEXT},
        {"field_name": F_STATUS, "type": SELECT, "property": _opts(STATUS_SENT, STATUS_PENDING)},
        {"field_name": F_LABEL, "type": TEXT},
        {"field_name": F_NOTES, "type": TEXT},
        {"field_name": F_ID, "type": NUMBER, "property": {"formatter": "0"}},
    ]


_LINK_MD = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def plain(text: str | None) -> str:
    """多维表格不渲染 Markdown：[文字](链接) → 文字 链接，去掉粗体记号。"""
    if not text:
        return ""
    return _LINK_MD.sub(r"\1 \2", text).replace("**", "").strip()


def _round(v: float | None, n: int = 2) -> float | None:
    return None if v is None else round(float(v), n)


def row_fields(row: dict) -> dict[str, Any]:
    """alerts 表的一行 → 多维表格里程序维护的列（不含 label / notes）。值为 None 的列不写。"""
    a = Storage._row_to_alert(row)
    head = a.headline
    data: dict = {}
    if row.get("attribution_json"):
        try:
            data = json.loads(row["attribution_json"]) or {}
        except ValueError:
            data = {}
    text = plain(row.get("attribution_text"))
    summary = data.get("summary") or (text.splitlines()[0] if text else None)
    sector = a.beta * a.benchmark_move_pct if a.beta is not None and a.benchmark_move_pct is not None else None
    out: dict[str, Any] = {
        F_TITLE: f"{pseudo_label(a.symbol)} {head.short(a.ref_label)}" + ("（扩大）" if a.is_escalation else ""),
        F_TIME: int(a.ts.timestamp() * 1000),
        F_SYMBOL: a.symbol,
        F_RULE: RULE_NAMES.get(head.rule, head.rule),
        F_DIRECTION: "涨" if a.direction > 0 else "跌",
        F_TRIGGERS: ("【" + a.segment_label + "】" if a.segment_label else "") + "；".join(t.describe(a.ref_label) for t in a.triggers),
        F_PRICE: _round(a.price),
        F_DAY: _round(a.day_change_pct),
        F_REF: a.ref_label,
        F_WINDOW: f"{a.window_minutes}分钟" if a.window_minutes else a.ref_label,
        F_MOVE: _round(a.move_pct),
        F_SECTOR: _round(sector),
        F_IDIO: _round(a.abnormal_move_pct),
        F_VOLUME: _round(a.volume_ratio, 1),
        F_SUMMARY: plain(summary) or None,
        F_DRIVER: data.get("driver") or None,
        F_DETAIL: text[:20000] or None,
        F_STATUS: STATUS_PENDING if row.get("message_id") is None else STATUS_SENT,
        F_ID: int(row["id"]),
    }
    return {k: v for k, v in out.items() if v is not None}


def row_hash(fields: dict) -> str:
    return hashlib.sha1(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def cell_text(v: Any) -> str | None:
    """读回的文本单元格：多行文本是 [{type, text}] 片段数组（search 接口），也兼容纯字符串。空值返回 None。"""
    if v is None:
        return None
    if isinstance(v, list):
        s = "".join(str(seg.get("text", "")) if isinstance(seg, dict) else str(seg) for seg in v)
    elif isinstance(v, dict):
        s = str(v.get("text", ""))
    else:
        s = str(v)
    s = s.strip()
    return s or None


def cell_int(v: Any) -> int | None:
    if isinstance(v, list):  # 个别接口把数字也包成片段
        v = cell_text(v)
    try:
        return int(float(v)) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


@dataclass
class BitableSyncResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped_deleted: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        s = f"新增 {self.created}、更新 {self.updated}、未变 {self.unchanged}"
        if self.skipped_deleted:
            s += f"、跳过已在飞书删除的 {self.skipped_deleted}"
        return s + (f"；失败：{self.errors[0]}" if self.errors else "")


@dataclass
class LabelPullResult:
    changed: int = 0
    records: int = 0
    deleted: int = 0  # 新发现你在飞书里删掉的行


class BitableSync:
    def __init__(self, settings: Settings, storage: Storage, api: FeishuApi):
        self.settings = settings
        self.storage = storage
        self.api = api
        self.cfg = settings.notify.feishu_sync
        self._ready: tuple[str, str] | None = None  # 本进程里已经确认存在、字段齐全的 (app_token, table_id)
        self._lock = asyncio.Lock()

    @property
    def ids(self) -> tuple[str, str] | None:
        app, table = self.storage.kv_get(APP_KEY), self.storage.kv_get(TABLE_KEY)
        return (app, table) if app and table else None

    @property
    def url(self) -> str | None:
        return self.storage.kv_get(URL_KEY)

    async def ensure(self, open_id: str | None = None) -> tuple[str, str]:
        """确保多维表格、数据表、各列都在（缺列就补上），并把你加为协作者。返回 (app_token, table_id)。"""
        ids = self.ids
        if self._ready is not None and self._ready == ids and (
                not open_id or self.storage.kv_get(SHARED_KEY) == f"{ids[0]}|{open_id}"):
            return self._ready
        app = self.storage.kv_get(APP_KEY)
        table = self.storage.kv_get(TABLE_KEY)
        if app:
            try:
                await self.api.get_bitable(app)
            except FeishuError as exc:
                if exc.code not in BITABLE_GONE:
                    raise
                log.warning("飞书多维表格 %s 不存在（%s），新建一份", app, exc.code)
                app = table = None
        if not app:
            created = await self.api.create_bitable(self.cfg.bitable_name, self.settings.timezone)
            app = created.get("app_token")
            if not app:
                raise FeishuError(-1, "创建多维表格的响应里没有 app_token", "创建多维表格", area="bitable")
            self.storage.kv_set(APP_KEY, app)
            self.storage.kv_set(URL_KEY, created.get("url") or "")
            if created.get("default_table_id"):
                self.storage.kv_set(DEFAULT_TABLE_KEY, created["default_table_id"])
            self.storage.kv_delete(TABLE_KEY)
            self.storage.kv_delete(SHARED_KEY)
            table = None
            log.info("已新建飞书多维表格 %s（%s）", self.cfg.bitable_name, created.get("url"))
        specs = field_specs(self.settings)
        if table:
            try:
                existing = {f.get("field_name") for f in await self.api.list_fields(app, table)}
            except FeishuError as exc:
                if exc.code not in BITABLE_GONE:
                    raise
                log.warning("飞书数据表 %s 不存在（%s），新建一张", table, exc.code)
                table = None
            else:
                for spec in specs:
                    if spec["field_name"] not in existing:
                        await self.api.create_field(app, table, spec)
                        log.info("飞书数据表补上一列：%s", spec["field_name"])
        if not table:
            res = await self.api.create_table(app, TABLE_NAME, specs, VIEW_NAME)
            table = res.get("table_id")
            if not table:
                raise FeishuError(-1, "创建数据表的响应里没有 table_id", "创建数据表", area="bitable")
            self.storage.kv_set(TABLE_KEY, table)
            default = self.storage.kv_get(DEFAULT_TABLE_KEY)
            if default and default != table:
                try:
                    await self.api.delete_table(app, default)
                except FeishuError as exc:  # 删不掉就留着一张空表，不影响使用
                    log.info("删除多维表格自带的空数据表失败：%s", exc)
                self.storage.kv_delete(DEFAULT_TABLE_KEY)
        if open_id:
            await self.share(app, open_id)
        self._ready = (app, table)
        return app, table

    def _forget_if_gone(self, exc: FeishuError) -> None:
        """列被删掉 / 改名、数据表或整份多维表格被删：清掉缓存，下一次 ensure() 重新核对、补建（不用重启）。"""
        if exc.code in FIELD_GONE or exc.code in BITABLE_GONE:
            self._ready = None

    async def share(self, app_token: str, open_id: str) -> bool:
        marker = f"{app_token}|{open_id}"
        if self.storage.kv_get(SHARED_KEY) == marker:
            return False
        try:
            await self.api.add_member(app_token, "bitable", open_id, "full_access")
        except FeishuError as exc:
            if exc.code != ALREADY_MEMBER:
                raise
            log.info("把你加为多维表格协作者时飞书返回 %s（多半是已经有权限）：%s", exc.code, exc.msg)
        self.storage.kv_set(SHARED_KEY, marker)
        return True

    async def sync_alerts(self, since: datetime | None = None, open_id: str | None = None) -> BitableSyncResult:
        """把 alerts 表（since 之后的；为空则全部）写进多维表格：没有的插入，程序维护的列变了就更新。"""
        async with self._lock:
            app, table = await self.ensure(open_id)
            res = BitableSyncResult()
            mapping = self.storage.feishu_bitable_map(app, table)
            creates: list[tuple[int, dict, str]] = []
            updates: list[tuple[int, str, dict, str]] = []
            for row in self.storage.alert_rows(since):
                aid = int(row["id"])
                fields = row_fields(row)
                h = row_hash(fields)
                known = mapping.get(aid)
                if known is None:
                    extra = {F_LABEL: row.get("label"), F_NOTES: row.get("notes")}
                    creates.append((aid, {**fields, **{k: v for k, v in extra.items() if v}}, h))
                elif known[1] == DELETED:
                    res.skipped_deleted += 1
                elif known[1] != h:
                    updates.append((aid, known[0], fields, h))
                else:
                    res.unchanged += 1
            for i in range(0, len(creates), BATCH):
                res.created += await self._create(app, table, creates[i:i + BATCH], res)
            for i in range(0, len(updates), BATCH):
                res.updated += await self._update(app, table, updates[i:i + BATCH], res)
            if creates or updates:
                log.info("飞书多维表格同步：%s", res.summary())
            return res

    async def _create(self, app: str, table: str, chunk: list[tuple[int, dict, str]], res: BitableSyncResult) -> int:
        # 幂等键：同一批异动只会插入一次（网络重试时飞书按 client_token 去重）。文档要求 UUIDv4 格式，
        # 这里用内容哈希填充、再设上 v4 的版本位：格式合规，同一批异动得到同一个值
        seed = f"tickersentinel:{app}:{table}:" + ",".join(str(a) for a, _, _ in chunk)
        token = str(uuid.UUID(bytes=hashlib.sha256(seed.encode()).digest()[:16], version=4))
        try:
            records = await self.api.batch_create_records(app, table, [f for _, f, _ in chunk], client_token=token)
        except FeishuError as exc:
            self._forget_if_gone(exc)
            if exc.fatal:
                raise
            res.errors.append(str(exc))
            log.warning("飞书多维表格插入失败：%s", exc)
            return 0
        by_id = {cell_int((r.get("fields") or {}).get(F_ID)): r.get("record_id") for r in records}
        if None in by_id and len(records) == len(chunk):  # 响应里没带回 alert_id 时退回按顺序对应
            by_id = {aid: r.get("record_id") for (aid, _, _), r in zip(chunk, records)}
        n = 0
        for aid, _, h in chunk:
            rid = by_id.get(aid)
            if rid:
                self.storage.set_feishu_bitable_row(app, table, aid, rid, h)
                n += 1
        return n

    async def _update(self, app: str, table: str, chunk: list[tuple[int, str, dict, str]],
                      res: BitableSyncResult) -> int:
        try:
            await self.api.batch_update_records(app, table, [(rid, f) for _, rid, f, _ in chunk])
        except FeishuError as exc:
            self._forget_if_gone(exc)
            if exc.fatal:
                raise
            if exc.code not in RECORD_GONE:
                res.errors.append(str(exc))
                log.warning("飞书多维表格更新失败：%s", exc)
                return 0
            return await self._update_one_by_one(app, table, chunk, res)  # 有行被你删了：逐条更新，找出是哪一行
        for aid, rid, _, h in chunk:
            self.storage.set_feishu_bitable_row(app, table, aid, rid, h)
        return len(chunk)

    async def _update_one_by_one(self, app: str, table: str, chunk: list[tuple[int, str, dict, str]],
                                 res: BitableSyncResult) -> int:
        n = 0
        for aid, rid, fields, h in chunk:
            try:
                await self.api.batch_update_records(app, table, [(rid, fields)])
            except FeishuError as exc:
                self._forget_if_gone(exc)
                if exc.fatal:
                    raise
                if exc.code in RECORD_GONE:
                    self.storage.set_feishu_bitable_row(app, table, aid, rid, DELETED)
                    res.skipped_deleted += 1
                else:
                    res.errors.append(str(exc))
                continue
            self.storage.set_feishu_bitable_row(app, table, aid, rid, h)
            n += 1
        return n

    async def pull_labels(self, now: datetime | None = None) -> LabelPullResult:
        """把你在多维表格里填的「label 真实原因」「notes 备注」拉回 alerts.label / alerts.notes。"""
        async with self._lock:
            app, table = await self.ensure()
            # 映射要在检索之前读：另一个进程（tsen feishu sync 与 tsen run 同时跑）在检索之后插入的行
            # 不在检索结果里，如果映射在检索之后读，就会被误判为“已被删除”而永远不再更新（2026-10-21 审查发现）
            mapped = self.storage.feishu_bitable_map(app, table)
            try:
                records = await self.api.search_records(app, table, [F_ID, F_LABEL, F_NOTES])
            except FeishuError as exc:
                self._forget_if_gone(exc)
                raise
            res = LabelPullResult(records=len(records))
            current = self.storage.alert_labels()
            seen: set[str] = set()
            for r in records:
                rid = r.get("record_id")
                if rid:
                    seen.add(rid)
                f = r.get("fields") or {}
                aid = cell_int(f.get(F_ID))
                if aid is None or aid not in current:
                    continue
                # 飞书里的空单元格不覆盖库里已有的标注：列被改名 / 删掉后程序会补建一个空列，
                # 照搬空值会把辛苦标注的真实原因全部清空（审查实测）。代价：想清空标注要在库里清
                cur_label, cur_notes = current[aid]
                new = (cell_text(f.get(F_LABEL)) or cur_label, cell_text(f.get(F_NOTES)) or cur_notes)
                if new != current[aid]:
                    self.storage.set_alert_label(aid, *new)
                    res.changed += 1
            fresh = self.storage.feishu_bitable_synced_since(app, table, (now or utcnow()) - FRESH_ROW_GRACE)
            for aid, (rid, h) in mapped.items():
                if h != DELETED and rid not in seen and aid not in fresh:
                    self.storage.set_feishu_bitable_row(app, table, aid, rid, DELETED)
                    res.deleted += 1
            if res.changed or res.deleted:
                log.info("从飞书拉回标注：%d 条有变化，%d 行已被删除", res.changed, res.deleted)
            return res
