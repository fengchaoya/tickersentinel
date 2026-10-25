"""飞书开放平台接口的薄封装：日历、多维表格、云文档权限、消息记录。基于官方 SDK lark-oapi 1.7.5 的类型化请求。

- 全部以应用身份调用（tenant_access_token，SDK 自动获取并缓存约 2 小时）；应用必须开通机器人能力。
- 用 SDK 的同步方法、放进线程执行：SDK 的异步方法（acreate 等）在获取 token 时仍是同步的 requests 调用，会卡住主循环。
- 请求体用 SDK 的模型类从 dict 构造（模型的构造函数接受 dict），响应的 data 转回 dict，上层只和 dict 打交道。
- 失败抛 FeishuError（带飞书错误码）。限流类错误自动重试两次。缺权限（99991672）时飞书的错误信息里列出了
  可开通的权限和申请链接，FeishuError.guidance() 把它整理成中文提示。

用到的接口与权限（2026-10-21 对照开放平台文档整理；“任一”指开通其中一个即可，第一个是 README 里建议开通的）：
  日历      POST /calendar/v4/calendars                              创建日历（任一：calendar:calendar、calendar:calendar:create）
            GET  /calendar/v4/calendars/:calendar_id                 查询日历（calendar:calendar、calendar:calendar:readonly …）
            GET/POST /calendar/v4/calendars/:calendar_id/acls        日历成员（calendar:calendar、calendar:calendar.acl:create）
            POST/PATCH/DELETE /calendar/v4/calendars/:calendar_id/events[/:event_id]
                                                                     日程（calendar:calendar、calendar:calendar.event:create …）
            POST /calendar/v4/calendars/:calendar_id/events/:event_id/attendees   参与人（calendar:calendar）
            频率 1000 次/分钟、50 次/秒；应用身份调用要求开通机器人能力（否则 190007）。
  多维表格  POST /bitable/v1/apps                                     创建（bitable:app、base:app:create），20 次/分钟
            POST /bitable/v1/apps/:app_token/tables                  建表（bitable:app、base:table:create），首列只能是文本 / 数字 / 日期等
            GET/POST …/tables/:table_id/fields                       字段（bitable:app、base:field:create）
            POST …/records/batch_create | batch_update               每次最多 1000 条；同一张表不要并发写（1254291）
            POST …/records/search                                    每页最多 500 条（bitable:app、bitable:app:readonly）
  云文档权限 POST /drive/v1/permissions/:token/members?type=bitable   加协作者（bitable:app、docs:permission.member:create、drive:drive）
  消息      GET /im/v1/messages?container_id_type=chat                从会话记录里找出你的 open_id（im:message.p2p_msg:readonly，
                                                                     推送那一步已经开通；2026-10-21 实测可用）
            GET /im/v1/chats/:chat_id/members                        备选（im:chat:readonly、im:chat.members:read；实测当前未开通）
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Callable

log = logging.getLogger(__name__)

DOMAINS = {"feishu": "https://open.feishu.cn", "lark": "https://open.larksuite.com"}

NO_SCOPE = 99991672  # 应用没有开通接口需要的权限（实测：msg 里带权限列表和申请链接）
AUTH_ERRORS = {99991661, 99991663, 99991664, 99991665, 99991668, 10003, 10014, 190006}  # 凭证 / token 有问题
NO_BOT = 190007
RETRYABLE = {99991400, 190004, 190005, 190010, 1254290, 1254291, 1254607, 1066002, 1255040}  # 限流 / 并发冲突 / 超时
CALENDAR_GONE = {191000, 191001, 191003}  # 日历不存在 / ID 无效 / 已删除
EVENT_GONE = {193000, 193001, 193003}  # 日程 ID 无效 / 不存在 / 已删除
BITABLE_GONE = {1254003, 1254040, 1254702, 1254041, 1254004}  # 多维表格 / 数据表不存在
RECORD_GONE = {1254006, 1254043}  # 记录不存在（你在飞书里删掉了那一行）
ALREADY_MEMBER = 1063003  # 加协作者时“无效操作”：常见原因是对方已经有同等或更高的权限
# 这些错误重试也没用，整个同步应当停下来、提示你去处理
FATAL = {NO_SCOPE, NO_BOT, *AUTH_ERRORS, *CALENDAR_GONE, *BITABLE_GONE}

# 没能从错误信息里解析出权限时，按接口给出建议开通的权限
RECOMMENDED_SCOPES = {"calendar": "calendar:calendar", "bitable": "bitable:app", "drive": "bitable:app",
                      "im_messages": "im:message.p2p_msg:readonly", "im_members": "im:chat:readonly"}

HINTS = {
    NO_BOT: "应用没有开通机器人能力：开发者后台 → 添加应用能力 → 机器人，然后发布新版本。",
    190006: "App ID 与 App Secret 不匹配：检查 .env 里的 FEISHU_APP_ID / FEISHU_APP_SECRET。",
    191002: "应用对这个日历没有权限（它可能不是本应用创建的）。",
    193002: "应用没有权限修改这个日程。",
    195100: "这个用户不在应用所在的飞书企业里（或已离职）：检查 open_id。",
    1254302: "应用对这份多维表格没有权限（高级权限限制）。",
    1254701: "应用对目标云空间节点没有权限。",
    1063001: "参数错误：open_id 或文档 token 不对。",
    1063002: "应用没有这份文档的管理权限，无法添加协作者。",
    1063004: "应用没有这份文档的分享权限。",
    ALREADY_MEMBER: "无法添加协作者：对方可能已经有同等或更高的权限，或双方在通讯录里互不可见。",
}

_SCOPES_RE = re.compile(r"\[([a-z0-9_:.\-]+(?:\s*,\s*[a-z0-9_:.\-]+)*)\]", re.I)
_LINK_RE = re.compile(r"https://open\.(?:feishu\.cn|larksuite\.com)/app/[^\s，。；)）\]]+")


class FeishuError(RuntimeError):
    def __init__(self, code: int, msg: str, api: str = "", *, area: str = "", log_id: str | None = None):
        self.code = int(code) if code is not None else -1
        self.msg = msg or ""
        self.api = api
        self.area = area  # calendar / bitable / drive / im_messages / im_members：用来给出建议开通的权限
        self.log_id = log_id
        super().__init__(f"{api or '飞书接口'}失败（{self.code}）：{self.msg}")

    @property
    def scopes(self) -> list[str]:
        """缺权限时飞书列出的、开通其一即可的权限。"""
        m = _SCOPES_RE.search(self.msg)
        return [s.strip() for s in m.group(1).split(",")] if m else []

    @property
    def link(self) -> str | None:
        """飞书在缺权限错误里给的申请链接（打开即跳到开发者后台、勾好权限）。"""
        m = _LINK_RE.search(self.msg)
        return m.group(0) if m else None

    @property
    def fatal(self) -> bool:
        return self.code in FATAL

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE

    def guidance(self) -> str:
        """给人看的中文处理建议。"""
        if self.code == NO_SCOPE:
            rec = RECOMMENDED_SCOPES.get(self.area)
            scopes = self.scopes or [rec or "（见下面的错误原文）"]
            if rec and rec in scopes:  # 推荐的放最前：先开只读权限的话，之后建日程 / 写记录又会报缺权限
                scopes = [rec, *[x for x in scopes if x != rec]]
            lines = [f"飞书应用缺少权限（{self.api}）。到开发者后台「权限管理」开通下面任意一个"
                     + (f"（建议 {rec}，读写都够用）" if rec else "") + f"：{'、'.join(scopes)}",
                     "开通后到「版本管理与发布」创建并发布一个新版本，权限才会生效（你是管理员时直接通过）。"]
            if self.link:
                lines.append(f"直达链接：{self.link}")
            return "\n".join(lines)
        if self.code in AUTH_ERRORS:
            return "飞书凭证无效：检查 .env 里的 FEISHU_APP_ID / FEISHU_APP_SECRET 是否正确、应用是否已发布。" \
                   f"（{self.code} {self.msg}）"
        if self.code in CALENDAR_GONE:
            return "飞书日历不存在或已被删除：重新运行 tsen feishu calendar-setup 会新建一个。"
        if self.code in BITABLE_GONE:
            return "飞书多维表格或数据表不存在：重新运行 tsen feishu bitable-setup 会新建一份。"
        if self.retryable:
            return f"飞书接口限流或繁忙（{self.code}），稍后再试。"
        hint = HINTS.get(self.code)
        tail = f"（{self.api}：{self.code} {self.msg}" + (f"，log_id={self.log_id}" if self.log_id else "") + "）"
        return f"{hint}{tail}" if hint else f"飞书接口报错{tail}"


def _to_dict(obj: Any) -> dict:
    if obj is None:
        return {}
    from lark_oapi.core.json import JSON

    return json.loads(JSON.marshal(obj) or "{}")


class FeishuApi:
    """只封装本项目用到的接口。每个方法返回 dict / list；失败抛 FeishuError。"""

    RETRIES = 2
    RETRY_DELAY = 1.0  # 秒；第 n 次重试前等 n 倍（测试里设为 0）

    def __init__(self, app_id: str, app_secret: str, domain: str = "feishu", *, timeout: float = 20.0,
                 client: Any | None = None):
        if client is None:
            try:
                import lark_oapi as lark
            except ImportError as exc:  # pragma: no cover - 取决于是否安装了可选依赖
                raise RuntimeError('使用飞书同步需要安装 SDK：uv pip install -e ".[feishu]"') from exc
            client = (lark.Client.builder().app_id(app_id).app_secret(app_secret).domain(DOMAINS[domain])
                      .timeout(timeout).log_level(lark.LogLevel.WARNING).build())
        self.client = client

    async def _call(self, api: str, area: str, method: Callable[[Any], Any], request: Any) -> dict:
        from lark_oapi.core.exception import ObtainAccessTokenException

        for attempt in range(self.RETRIES + 1):
            try:
                resp = await asyncio.to_thread(method, request)
            except ObtainAccessTokenException as exc:  # 获取 tenant_access_token 失败（App ID / Secret 错）
                raise FeishuError(exc.code, exc.msg, api, area=area) from exc
            except Exception as exc:  # 网络错误、响应不是 JSON……
                raise FeishuError(-1, f"{type(exc).__name__}: {exc}", api, area=area) from exc
            if resp.success():
                return _to_dict(getattr(resp, "data", None))  # 删除类接口的响应没有 data
            err = FeishuError(resp.code, resp.msg or "", api, area=area, log_id=resp.get_log_id())
            if err.retryable and attempt < self.RETRIES:
                log.info("%s，%.0f 秒后重试", err, self.RETRY_DELAY * (attempt + 1))
                await asyncio.sleep(self.RETRY_DELAY * (attempt + 1))
                continue
            raise err
        raise AssertionError("unreachable")  # pragma: no cover

    # ───────── 日历 ─────────

    async def create_calendar(self, summary: str, description: str = "",
                              permissions: str = "show_only_free_busy") -> dict:
        from lark_oapi.api.calendar.v4 import Calendar, CreateCalendarRequest

        req = CreateCalendarRequest.builder().request_body(
            Calendar({"summary": summary, "description": description, "permissions": permissions})).build()
        data = await self._call("创建日历", "calendar", self.client.calendar.v4.calendar.create, req)
        return data.get("calendar") or {}

    async def get_calendar(self, calendar_id: str) -> dict:
        from lark_oapi.api.calendar.v4 import GetCalendarRequest

        req = GetCalendarRequest.builder().calendar_id(calendar_id).build()
        return await self._call("查询日历", "calendar", self.client.calendar.v4.calendar.get, req)

    async def list_calendar_acls(self, calendar_id: str) -> list[dict]:
        from lark_oapi.api.calendar.v4 import ListCalendarAclRequest

        out: list[dict] = []
        token: str | None = None
        while True:
            b = ListCalendarAclRequest.builder().calendar_id(calendar_id).user_id_type("open_id").page_size(50)
            if token:
                b = b.page_token(token)
            data = await self._call("查询日历成员", "calendar", self.client.calendar.v4.calendar_acl.list, b.build())
            out += data.get("acls") or []
            token = data.get("page_token")
            if not data.get("has_more") or not token:
                return out

    async def create_calendar_acl(self, calendar_id: str, open_id: str, role: str) -> dict:
        from lark_oapi.api.calendar.v4 import CalendarAcl, CreateCalendarAclRequest

        req = (CreateCalendarAclRequest.builder().calendar_id(calendar_id).user_id_type("open_id")
               .request_body(CalendarAcl({"role": role, "scope": {"type": "user", "user_id": open_id}})).build())
        return await self._call("添加日历成员", "calendar", self.client.calendar.v4.calendar_acl.create, req)

    async def create_event(self, calendar_id: str, event: dict, idempotency_key: str | None = None) -> dict:
        from lark_oapi.api.calendar.v4 import CalendarEvent, CreateCalendarEventRequest

        b = CreateCalendarEventRequest.builder().calendar_id(calendar_id).user_id_type("open_id")
        if idempotency_key:
            b = b.idempotency_key(idempotency_key)  # 32–128 个字符；同一应用、同一日历内唯一，防止重试时重复创建
        data = await self._call("创建日程", "calendar", self.client.calendar.v4.calendar_event.create,
                                b.request_body(CalendarEvent(event)).build())
        return data.get("event") or {}

    async def patch_event(self, calendar_id: str, event_id: str, event: dict) -> dict:
        from lark_oapi.api.calendar.v4 import CalendarEvent, PatchCalendarEventRequest

        req = (PatchCalendarEventRequest.builder().calendar_id(calendar_id).event_id(event_id).user_id_type("open_id")
               .request_body(CalendarEvent(event)).build())
        data = await self._call("更新日程", "calendar", self.client.calendar.v4.calendar_event.patch, req)
        return data.get("event") or {}

    async def delete_event(self, calendar_id: str, event_id: str) -> None:
        from lark_oapi.api.calendar.v4 import DeleteCalendarEventRequest

        # need_notification 是查询参数；SDK 会把 Python 的 False 拼成 "False"，所以直接给字符串
        req = (DeleteCalendarEventRequest.builder().calendar_id(calendar_id).event_id(event_id)
               .need_notification("false").build())
        await self._call("删除日程", "calendar", self.client.calendar.v4.calendar_event.delete, req)

    async def add_event_attendees(self, calendar_id: str, event_id: str, open_ids: list[str]) -> None:
        from lark_oapi.api.calendar.v4 import (
            CreateCalendarEventAttendeeRequest, CreateCalendarEventAttendeeRequestBody,
        )

        body = CreateCalendarEventAttendeeRequestBody({
            "attendees": [{"type": "user", "user_id": oid} for oid in open_ids],
            "need_notification": False,
        })
        req = (CreateCalendarEventAttendeeRequest.builder().calendar_id(calendar_id).event_id(event_id)
               .user_id_type("open_id").request_body(body).build())
        await self._call("添加日程参与人", "calendar", self.client.calendar.v4.calendar_event_attendee.create, req)

    # ───────── 多维表格 ─────────

    async def create_bitable(self, name: str, time_zone: str | None = None) -> dict:
        """返回 app：app_token、url、default_table_id……（不指定文件夹：建在应用自己的云空间根目录）"""
        from lark_oapi.api.bitable.v1 import CreateAppRequest, ReqApp

        body = {"name": name, **({"time_zone": time_zone} if time_zone else {})}
        req = CreateAppRequest.builder().request_body(ReqApp(body)).build()
        data = await self._call("创建多维表格", "bitable", self.client.bitable.v1.app.create, req)
        return data.get("app") or {}

    async def get_bitable(self, app_token: str) -> dict:
        from lark_oapi.api.bitable.v1 import GetAppRequest

        req = GetAppRequest.builder().app_token(app_token).build()
        data = await self._call("查询多维表格", "bitable", self.client.bitable.v1.app.get, req)
        return data.get("app") or {}

    async def create_table(self, app_token: str, name: str, fields: list[dict], default_view_name: str) -> dict:
        """fields 的第一项是首列（索引字段）。返回 table_id、default_view_id、field_id_list。"""
        from lark_oapi.api.bitable.v1 import CreateAppTableRequest, CreateAppTableRequestBody

        body = CreateAppTableRequestBody({"table": {"name": name, "default_view_name": default_view_name,
                                                    "fields": fields}})
        req = CreateAppTableRequest.builder().app_token(app_token).request_body(body).build()
        return await self._call("创建数据表", "bitable", self.client.bitable.v1.app_table.create, req)

    async def delete_table(self, app_token: str, table_id: str) -> None:
        from lark_oapi.api.bitable.v1 import DeleteAppTableRequest

        req = DeleteAppTableRequest.builder().app_token(app_token).table_id(table_id).build()
        await self._call("删除数据表", "bitable", self.client.bitable.v1.app_table.delete, req)

    async def list_fields(self, app_token: str, table_id: str) -> list[dict]:
        from lark_oapi.api.bitable.v1 import ListAppTableFieldRequest

        out: list[dict] = []
        token: str | None = None
        while True:
            b = ListAppTableFieldRequest.builder().app_token(app_token).table_id(table_id).page_size(100)
            if token:
                b = b.page_token(token)
            data = await self._call("查询字段", "bitable", self.client.bitable.v1.app_table_field.list, b.build())
            out += data.get("items") or []
            token = data.get("page_token")
            if not data.get("has_more") or not token:
                return out

    async def create_field(self, app_token: str, table_id: str, field: dict) -> dict:
        from lark_oapi.api.bitable.v1 import AppTableField, CreateAppTableFieldRequest

        req = (CreateAppTableFieldRequest.builder().app_token(app_token).table_id(table_id)
               .request_body(AppTableField(field)).build())
        data = await self._call("新增字段", "bitable", self.client.bitable.v1.app_table_field.create, req)
        return data.get("field") or {}

    async def batch_create_records(self, app_token: str, table_id: str, records: list[dict],
                                   client_token: str | None = None) -> list[dict]:
        """records：[{字段名: 值}]。返回 [{record_id, fields}]（文档没有保证顺序与请求一致，按字段内容对应）。"""
        from lark_oapi.api.bitable.v1 import BatchCreateAppTableRecordRequest, BatchCreateAppTableRecordRequestBody

        b = BatchCreateAppTableRecordRequest.builder().app_token(app_token).table_id(table_id)
        if client_token:
            b = b.client_token(client_token)  # UUIDv4 格式的幂等键：网络重试不会重复写入
        body = BatchCreateAppTableRecordRequestBody({"records": [{"fields": f} for f in records]})
        data = await self._call("新增记录", "bitable", self.client.bitable.v1.app_table_record.batch_create,
                                b.request_body(body).build())
        return data.get("records") or []

    async def batch_update_records(self, app_token: str, table_id: str, records: list[tuple[str, dict]]) -> list[dict]:
        """records：[(record_id, {字段名: 值})]，只改给出的字段（其他列，例如你填的 label / notes，不受影响）。"""
        from lark_oapi.api.bitable.v1 import BatchUpdateAppTableRecordRequest, BatchUpdateAppTableRecordRequestBody

        body = BatchUpdateAppTableRecordRequestBody(
            {"records": [{"record_id": rid, "fields": f} for rid, f in records]})
        req = (BatchUpdateAppTableRecordRequest.builder().app_token(app_token).table_id(table_id)
               .request_body(body).build())
        data = await self._call("更新记录", "bitable", self.client.bitable.v1.app_table_record.batch_update, req)
        return data.get("records") or []

    async def search_records(self, app_token: str, table_id: str, field_names: list[str]) -> list[dict]:
        """读出整张表（只取 field_names 这几列）。多行文本的值是 [{type: text, text: ...}] 片段数组。"""
        from lark_oapi.api.bitable.v1 import SearchAppTableRecordRequest, SearchAppTableRecordRequestBody

        out: list[dict] = []
        token: str | None = None
        while True:
            b = SearchAppTableRecordRequest.builder().app_token(app_token).table_id(table_id).page_size(500)
            if token:
                b = b.page_token(token)
            req = b.request_body(SearchAppTableRecordRequestBody({"field_names": field_names})).build()
            data = await self._call("读取记录", "bitable", self.client.bitable.v1.app_table_record.search, req)
            out += data.get("items") or []
            token = data.get("page_token")
            if not data.get("has_more") or not token:
                return out

    # ───────── 云文档权限 ─────────

    async def add_member(self, token: str, doc_type: str, open_id: str, perm: str) -> dict:
        """把用户加为文档协作者。perm：view / edit / full_access。
        need_notification 只对用户身份生效，应用身份调用时飞书不发通知，这里不传。"""
        from lark_oapi.api.drive.v1 import BaseMember, CreatePermissionMemberRequest

        body = BaseMember({"member_type": "openid", "member_id": open_id, "perm": perm, "type": "user"})
        req = CreatePermissionMemberRequest.builder().token(token).type(doc_type).request_body(body).build()
        data = await self._call("添加协作者", "drive", self.client.drive.v1.permission_member.create, req)
        return data.get("member") or {}

    # ───────── 消息 / 会话 ─────────

    async def recent_senders(self, chat_id: str, limit: int = 50) -> list[dict]:
        """会话里最近 limit 条消息的发送人：[{id, id_type, sender_type}]，按时间倒序。"""
        from lark_oapi.api.im.v1 import ListMessageRequest

        req = (ListMessageRequest.builder().container_id_type("chat").container_id(chat_id)
               .sort_type("ByCreateTimeDesc").page_size(min(limit, 50)).build())
        data = await self._call("读取会话消息", "im_messages", self.client.im.v1.message.list, req)
        return [m.get("sender") or {} for m in data.get("items") or []]

    async def chat_member_ids(self, chat_id: str) -> list[str]:
        from lark_oapi.api.im.v1 import GetChatMembersRequest

        req = GetChatMembersRequest.builder().chat_id(chat_id).member_id_type("open_id").page_size(100).build()
        data = await self._call("读取会话成员", "im_members", self.client.im.v1.chat_members.get, req)
        return [m["member_id"] for m in data.get("items") or [] if m.get("member_id")]


async def find_user_open_id(api: FeishuApi, chat_id: str) -> str | None:
    """找出你的 open_id：先看会话 FEISHU_CHAT_ID 里最近由“用户”发出的消息（推送那一步开通的权限就够），
    再用会话成员列表（需要 im:chat:readonly）核对。只在能确定是同一个人时返回；
    群聊里有多人发言或多个成员时返回 None（找错人会把多维表格的编辑权限和日历共享给别人，2026-10-21 审查发现），
    这时请用 --open-id 指定。两条路都缺权限时抛第一条的错误。"""
    first_error: FeishuError | None = None
    senders: list[str] = []
    try:
        for s in await api.recent_senders(chat_id):
            if s.get("sender_type") == "user" and s.get("id_type") == "open_id" and s.get("id"):
                if str(s["id"]) not in senders:
                    senders.append(str(s["id"]))
    except FeishuError as exc:
        if exc.code in AUTH_ERRORS or exc.code == NO_BOT:
            raise
        first_error = exc
    if len(senders) > 1:
        log.warning("会话 %s 里最近有 %d 个人发过消息，没法判断哪个是你；请用 --open-id 指定", chat_id, len(senders))
        return None
    try:
        ids = await api.chat_member_ids(chat_id)
    except FeishuError:
        if senders:  # 没有成员列表权限：只有一个人发过言，按单聊处理
            return senders[0]
        if first_error is not None:
            raise first_error
        return None
    if len(ids) > 1:  # 群聊里有多个成员：即使只有一个人发过言，也不能断定就是你
        log.warning("会话 %s 有 %d 个成员，没法判断哪个是你；请用 --open-id 指定", chat_id, len(ids))
        return None
    return senders[0] if senders else (ids[0] if ids else None)
