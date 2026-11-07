"""事件日历（events 表）→ 飞书日历。

- 应用（机器人）以自己的身份建一个共享日历（默认「tickersentinel · 事件日历」），把你加为日历成员，角色默认 reader：
  看得到日程详情、不能改。不默认 writer：这个日历是 events 表的镜像，在飞书里改的内容下一次同步会被覆盖；
  要更正日期请写进 settings.yaml 的 events.manual（手工录入优先级最高）。
- 日历的公开范围设为 show_only_free_busy（仅显示忙闲）：飞书只允许订阅 public / show_only_free_busy 的日历，
  private 的订阅不了；同一企业里的其他人最多看到忙闲，你是成员，看得到详情。所有日程标为“空闲”，不占你的忙闲。
- 订阅接口只对“当前身份”生效，应用没法替你订阅：第一次需要你在飞书日历里订阅一次（tsen feishu calendar-setup 有提示）。
  不想订阅就打开 notify.feishu_sync.invite_user：把你加为每个日程的参与人（不发通知），日程直接出现在你的主日历里。
- 同步范围：今天之前 past_days 天起的全部事件。新事件建日程；内容变了（改期、确认、时间、标题）就更新；
  events 表里已经没有的 key（过时的估计被清掉、跨季度换了 key、手工录入被删）就删除对应日程。更早的日程留着，不再更新。
- 映射（事件 key ↔ 飞书日程 ID + 内容哈希）在 feishu_calendar_events 表，日历 ID 在 kv：重复同步不会重复建日程；
  创建时带 idempotency_key，网络重试也不会重复。
- 有精确时间（Event.at）→ 定时日程，时长 event_minutes；只有日期 → Event.day 的全天日程。
  飞书全天日程的结束日期不含当天（单日全天日程 end = 次日），与 iCalendar 一致；官方命令行 larksuite/cli 读回全天日程时
  也是把结束日期减一天来显示。全天日程的时区固定为 UTC，不传 timezone。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from tickersentinel.config import Settings
from tickersentinel.feishu.client import EVENT_GONE, CALENDAR_GONE, FeishuApi, FeishuError
from tickersentinel.models import Event
from tickersentinel.storage import Storage
from tickersentinel.timeutil import ET, fmt_local, tz_abbr, utcnow

log = logging.getLogger(__name__)

CALENDAR_KEY = "feishu:calendar_id"
SHARED_KEY = "feishu:calendar_shared"  # 已经把谁以什么角色加进了哪个日历（避免每次同步都查成员列表）
ROLE_RANK = {"free_busy_reader": 1, "reader": 2, "writer": 3, "owner": 4}
KIND_TAGS = {"earnings": "财报", "insider_sale": "拟减持", "lockup_expiry": "解禁", "custom": "日程", "macro": "宏观"}
SOURCE_NAMES = {"config": "手工录入", "sec_6k": "SEC 6-K 公告", "sec_144": "SEC Form 144", "jpx": "东京交易所",
                "finnhub": "Finnhub", "yfinance": "Yahoo Finance", "fed": "美联储", "bls": "美国劳工统计局（BLS）",
                "bea": "美国经济分析局（BEA）", "census": "美国人口普查局", "treasury": "美国财政部", "boj": "日本央行",
                "calendar_rules": "按交易所日历与惯例推算"}
FAR_FUTURE_DAYS = 3660


def _gen_key(cid: str, event_key: str) -> str:
    return f"feishu:calendar_gen:{cid}:{event_key}"


@dataclass
class CalendarSyncResult:
    calendar_id: str = ""
    created: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        s = f"新建 {self.created}、更新 {self.updated}、删除 {self.deleted}、未变 {self.unchanged}"
        return s + (f"；失败 {len(self.errors)} 条：{self.errors[0]}" if self.errors else "")


def _clean(text: str) -> str:
    """日程描述会被当成 HTML 解析：去掉尖括号，避免文本被吞掉。"""
    return str(text).replace("<", "＜").replace(">", "＞")


def _cap(text: str, n: int) -> str:
    """截断但保留换行（textutil.truncate 会把换行压成空格）。"""
    return text if len(text) <= n else text[: n - 1] + "…"


def _fmt_money(v: float) -> str:
    return f"${v / 1e9:.2f}B" if abs(v) >= 1e9 else f"${v / 1e6:.1f}M" if abs(v) >= 1e6 else f"${v:,.0f}"


def describe(e: Event, settings: Settings) -> str:
    tz = settings.tz
    d = e.details or {}
    src = SOURCE_NAMES.get(e.source, e.source or "未知")
    who = "官方公布" if e.kind == "macro" else "公司公告"
    lines = [f"状态：已确认（来源：{src}）" if e.confirmed else f"状态：预计日期（来源：{src}），以{who}为准"]
    if e.at:
        lines.append(f"时间：{fmt_local(e.at, tz, '%Y-%m-%d %H:%M')} {tz_abbr(e.at, tz)}"
                     f"（美东 {fmt_local(e.at, ET, '%m-%d %H:%M')}）")
        if d.get("local_time"):
            lines.append(f"当地时间：{d['local_time']}")
    elif e.time_hint:
        lines.append(f"时段：{e.time_hint}")
    if e.kind == "earnings":
        if d.get("conference_call"):
            lines.append("有业绩电话会")
        if d.get("day_range"):
            lines.append(f"Yahoo 给出的日期区间：{' ~ '.join(d['day_range'])}")
        if d.get("eps_avg") is not None:
            lines.append(f"EPS 一致预期：{d['eps_avg']}")
    elif e.kind == "insider_sale":
        who = d.get("person") or "内部人"
        if d.get("relationship"):
            who += f"（{d['relationship']}）"
        lines.append(f"申报人：{who}")
        if d.get("shares") is not None:
            lines.append(f"拟出售：{d['shares']:,} 股" + (f"，约 {_fmt_money(d['market_value_usd'])}"
                                                        if d.get("market_value_usd") is not None else ""))
        if d.get("approx_sale_date"):
            lines.append(f"预计出售日：{d['approx_sale_date']}")
        if d.get("broker"):
            lines.append(f"经纪商：{d['broker']}")
    if d.get("note"):
        lines.append(f"备注：{d['note']}")
    if e.url:
        lines.append(f"链接：{e.url}")
    lines.append("由 tickersentinel 根据事件日历自动同步；在这里修改会被下一次同步覆盖。")
    return _clean("\n".join(lines))


def event_body(e: Event, settings: Settings) -> dict:
    """事件 → 飞书日程（创建 / 更新共用的字段）。"""
    cfg = settings.notify.feishu_sync
    extras = [x for x in ((e.time_hint if not e.at else ""), ("" if e.confirmed else "预计")) if x]
    summary = f"【{KIND_TAGS.get(e.kind, '日程')}】{e.title}" + (f"（{'，'.join(extras)}）" if extras else "")
    if e.at:
        end = e.at + timedelta(minutes=cfg.event_minutes)
        start_time = {"timestamp": str(int(e.at.timestamp())), "timezone": settings.timezone}
        end_time = {"timestamp": str(int(end.timestamp())), "timezone": settings.timezone}
    else:
        start_time = {"date": e.day.isoformat()}
        end_time = {"date": (e.day + timedelta(days=1)).isoformat()}  # 全天日程的结束日期不含当天
    return {
        "summary": _cap(_clean(summary), 1000),
        "description": _cap(describe(e, settings), 40000),
        "start_time": start_time,
        "end_time": end_time,
        "need_notification": False,
        "visibility": "default",  # 跟随日历：成员看详情，其他人只看忙闲
        "free_busy_status": "free",  # 信息类日程，不占忙闲
        "attendee_ability": "none",
    }


def content_hash(body: dict, attendee: str | None = None) -> str:
    raw = json.dumps({"body": body, "attendee": attendee}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class CalendarSync:
    def __init__(self, settings: Settings, storage: Storage, api: FeishuApi):
        self.settings = settings
        self.storage = storage
        self.api = api
        self.cfg = settings.notify.feishu_sync

    @property
    def calendar_id(self) -> str | None:
        return self.storage.kv_get(CALENDAR_KEY)

    async def ensure_calendar(self, open_id: str | None = None) -> tuple[str, bool]:
        """返回 (日历 ID, 是否新建)。已有的日历被删了就新建一个（旧日历的映射按 calendar_id 区分，自然失效）。"""
        cid = self.calendar_id
        if cid:
            try:
                info = await self.api.get_calendar(cid)
                if info.get("is_deleted"):
                    log.warning("飞书日历 %s 已被删除，新建一个", cid)
                    cid = None
            except FeishuError as exc:
                if exc.code not in CALENDAR_GONE:
                    raise
                log.warning("飞书日历 %s 不存在（%s），新建一个", cid, exc.code)
                cid = None
        created = False
        if not cid:
            cal = await self.api.create_calendar(
                self.cfg.calendar_name,
                "tickersentinel 自动同步的事件日历：持仓及相关公司的财报、内部人拟减持（Form 144）、解禁等。",
            )
            cid = cal.get("calendar_id")
            if not cid:
                raise FeishuError(-1, "创建日历的响应里没有 calendar_id", "创建日历", area="calendar")
            self.storage.kv_set(CALENDAR_KEY, cid)
            self.storage.kv_delete(SHARED_KEY)
            created = True
            log.info("已新建飞书日历 %s（%s）", self.cfg.calendar_name, cid)
        if open_id:
            await self.share(cid, open_id)
        return cid, created

    async def share(self, calendar_id: str, open_id: str) -> bool:
        """把你加为日历成员；已经是（同等或更高角色）就什么都不做。返回是否新加。"""
        role = self.cfg.calendar_role
        marker = f"{calendar_id}|{open_id}|{role}"
        if self.storage.kv_get(SHARED_KEY) == marker:
            return False
        acls = await self.api.list_calendar_acls(calendar_id)
        mine = [a for a in acls if (a.get("scope") or {}).get("user_id") == open_id]
        added = False
        if not any(ROLE_RANK.get(a.get("role", ""), 0) >= ROLE_RANK[role] for a in mine):
            await self.api.create_calendar_acl(calendar_id, open_id, role)
            added = True
            log.info("已把用户 %s 以 %s 角色加入飞书日历", open_id, role)
        self.storage.kv_set(SHARED_KEY, marker)
        return added

    def events_to_sync(self, now: datetime) -> list[Event]:
        """要同步的事件：只要监控范围里的（移除一个主题后，它的同业和相关公司的事件从日历里撤掉，和汇总、简报一致）。"""
        from tickersentinel.events.hub import event_in_scope

        today = now.astimezone(self.settings.tz).date()
        start = today - timedelta(days=self.cfg.past_days)
        return [e for e in self.storage.events_between(start, today + timedelta(days=FAR_FUTURE_DAYS))
                if event_in_scope(self.settings, e)]

    async def sync(self, now: datetime | None = None, open_id: str | None = None) -> CalendarSyncResult:
        """把 events 表同步到飞书日历。缺权限、凭证错误这类问题直接抛 FeishuError；单个日程失败只记下来、继续。"""
        now = now or utcnow()
        cid, _ = await self.ensure_calendar(open_id)
        res = CalendarSyncResult(calendar_id=cid)
        attendee = open_id if (self.cfg.invite_user and open_id) else None
        mapping = self.storage.feishu_calendar_map(cid)
        for e in self.events_to_sync(now):
            body = event_body(e, self.settings)
            h = content_hash(body, attendee)
            known = mapping.get(e.key)
            try:
                if known is None:
                    await self._create(cid, e, body, h, attendee)
                    res.created += 1
                elif known[1] != h:
                    try:
                        await self.api.patch_event(cid, known[0], body)
                        if attendee:
                            await self.api.add_event_attendees(cid, known[0], [attendee])
                        self.storage.set_feishu_calendar_event(cid, e.key, known[0], h)
                        res.updated += 1
                    except FeishuError as exc:
                        if exc.code not in EVENT_GONE:
                            raise
                        await self._create(cid, e, body, h, attendee, retry_tag=now.isoformat())
                        res.created += 1
                else:
                    res.unchanged += 1
            except FeishuError as exc:
                if exc.fatal:
                    raise
                res.errors.append(f"{e.title}：{exc}")
                log.warning("同步日程失败（%s）：%s", e.key, exc)
        from tickersentinel.events.hub import event_in_scope

        out_of_scope = {e.key for e in self.storage.events_between(date.min, date.max)
                        if not event_in_scope(self.settings, e)}
        live = self.storage.event_keys() - out_of_scope  # 不在范围里的也当作已移除（范围恢复后按代数重建）
        for key, (event_id, _) in mapping.items():
            if key in live:
                continue
            try:
                await self.api.delete_event(cid, event_id)
            except FeishuError as exc:
                if exc.fatal:
                    raise
                if exc.code not in EVENT_GONE:
                    res.errors.append(f"删除 {key}：{exc}")
                    log.warning("删除飞书日程失败（%s）：%s", key, exc)
                    continue
            self.storage.delete_feishu_calendar_event(cid, key)
            self._bump_generation(cid, key)
            res.deleted += 1
        log.info("飞书日历同步：%s", res.summary())
        return res

    def _bump_generation(self, cid: str, key: str) -> None:
        k = _gen_key(cid, key)
        self.storage.kv_set(k, str(int(self.storage.kv_get(k) or "0") + 1))

    async def _create(self, cid: str, e: Event, body: dict, h: str, attendee: str | None, retry_tag: str = "") -> None:
        # 幂等键：同一日历、同一事件、同一内容只会建一个（网络重试、建完没来得及记映射就崩溃都不会重复）。
        # 日程被删后重建要换一个键，否则飞书可能把已删除的那个日程原样返回：在飞书里被删的用 retry_tag；
        # 我们自己删掉的（事件从库里消失）记一个“代数”，事件原样回来时代数不同、键也不同（2026-10-21 审查发现）
        gen = self.storage.kv_get(_gen_key(cid, e.key)) or "0"
        idem = hashlib.sha256(f"{cid}|{e.key}|{h}|{retry_tag}|{gen}".encode()).hexdigest()
        created = await self.api.create_event(cid, body, idempotency_key=idem)
        event_id = created.get("event_id")
        if not event_id:
            raise FeishuError(-1, "创建日程的响应里没有 event_id", "创建日程", area="calendar")
        # 先记下映射再加参与人：加参与人失败时下次同步不会重复建日程（哈希里不含参与人状态的话会漏加，所以失败时清掉哈希）
        self.storage.set_feishu_calendar_event(cid, e.key, event_id, h)
        if attendee:
            try:
                await self.api.add_event_attendees(cid, event_id, [attendee])
            except FeishuError:
                self.storage.set_feishu_calendar_event(cid, e.key, event_id, "")
                raise

