"""飞书日历 / 多维表格同步：用一个内存里的“假飞书”替换 lark-oapi 的 HTTP 传输层（Transport.execute），
请求仍由 SDK 的类型化构造器生成、响应仍由 SDK 解析，所以路径、查询参数、请求体格式都被一并检查。不联网。"""

import json
import re
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest

pytest.importorskip("lark_oapi")

from lark_oapi.core.http import Transport  # noqa: E402
from lark_oapi.core.json import JSON  # noqa: E402
from lark_oapi.core.model import RawResponse  # noqa: E402

from tickersentinel.config import Secrets  # noqa: E402
from tickersentinel.feishu import FeishuApi, FeishuError, FeishuSync  # noqa: E402
from tickersentinel.feishu.bitable_sync import (  # noqa: E402
    DELETED, F_DETAIL, F_DRIVER, F_ID, F_LABEL, F_NOTES, F_STATUS, F_SUMMARY, F_TITLE, BitableSync, cell_text,
)
from tickersentinel.feishu.calendar_sync import CalendarSync, event_body  # noqa: E402
from tickersentinel.feishu.client import find_user_open_id  # noqa: E402
from tickersentinel.models import Alert, Event, Trigger, earnings_key  # noqa: E402
from tickersentinel.storage import Storage  # noqa: E402

from conftest import TEST_CONFIG, utc  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "feishu"
NOW = utc(2026, 10, 21, 18, 0)  # 10-21 13:00 CDT


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def ok(data: dict | None = None) -> dict:
    return {"code": 0, "msg": "success", "data": data or {}}


class FakeFeishu:
    """飞书开放平台的最小内存实现，只覆盖我们用到的接口。fail[接口名] = [错误响应, ...] 依次注入失败。"""

    ROUTES = [
        ("POST", r"/open-apis/auth/v3/tenant_access_token/internal", "token"),
        ("POST", r"/open-apis/calendar/v4/calendars", "create_calendar"),
        ("GET", r"/open-apis/calendar/v4/calendars/(?P<cid>[^/]+)", "get_calendar"),
        ("GET", r"/open-apis/calendar/v4/calendars/(?P<cid>[^/]+)/acls", "list_acls"),
        ("POST", r"/open-apis/calendar/v4/calendars/(?P<cid>[^/]+)/acls", "create_acl"),
        ("POST", r"/open-apis/calendar/v4/calendars/(?P<cid>[^/]+)/events", "create_event"),
        ("PATCH", r"/open-apis/calendar/v4/calendars/(?P<cid>[^/]+)/events/(?P<eid>[^/]+)", "patch_event"),
        ("DELETE", r"/open-apis/calendar/v4/calendars/(?P<cid>[^/]+)/events/(?P<eid>[^/]+)", "delete_event"),
        ("POST", r"/open-apis/calendar/v4/calendars/(?P<cid>[^/]+)/events/(?P<eid>[^/]+)/attendees", "add_attendees"),
        ("POST", r"/open-apis/bitable/v1/apps", "create_app"),
        ("GET", r"/open-apis/bitable/v1/apps/(?P<app>[^/]+)", "get_app"),
        ("POST", r"/open-apis/bitable/v1/apps/(?P<app>[^/]+)/tables", "create_table"),
        ("DELETE", r"/open-apis/bitable/v1/apps/(?P<app>[^/]+)/tables/(?P<tid>[^/]+)", "delete_table"),
        ("GET", r"/open-apis/bitable/v1/apps/(?P<app>[^/]+)/tables/(?P<tid>[^/]+)/fields", "list_fields"),
        ("POST", r"/open-apis/bitable/v1/apps/(?P<app>[^/]+)/tables/(?P<tid>[^/]+)/fields", "create_field"),
        ("POST", r"/open-apis/bitable/v1/apps/(?P<app>[^/]+)/tables/(?P<tid>[^/]+)/records/batch_create", "batch_create"),
        ("POST", r"/open-apis/bitable/v1/apps/(?P<app>[^/]+)/tables/(?P<tid>[^/]+)/records/batch_update", "batch_update"),
        ("POST", r"/open-apis/bitable/v1/apps/(?P<app>[^/]+)/tables/(?P<tid>[^/]+)/records/search", "search"),
        ("POST", r"/open-apis/drive/v1/permissions/(?P<token>[^/]+)/members", "add_member"),
        ("GET", r"/open-apis/im/v1/messages", "list_messages"),
        ("GET", r"/open-apis/im/v1/chats/(?P<chat>[^/]+)/members", "chat_members"),
    ]

    def __init__(self):
        self.calendars: dict[str, dict] = {}
        self.acls: dict[str, list[dict]] = {}
        self.events: dict[tuple[str, str], dict] = {}
        self.idem: dict[tuple[str, str], str] = {}
        self.attendees: dict[tuple[str, str], list[str]] = {}
        self.apps: dict[str, dict] = {}
        self.members: list[tuple[str, str, dict]] = []
        self.senders: list[dict] = fixture("message_senders.json")["senders"]
        self.chat_member_list: list[str] = []
        self.fail: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, str, dict, dict | None]] = []
        self._n = 0

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n}"

    def names(self) -> list[str]:
        return [name for name, *_ in self.calls]

    def execute(self, conf, req, option=None):
        path = req.uri
        for k, v in req.paths.items():
            path = path.replace(":" + k, str(v))
        query = dict(req.queries)
        body = json.loads(JSON.marshal(req.body)) if req.body is not None else None
        method = req.http_method.name
        for m, pattern, name in self.ROUTES:
            match = re.fullmatch(pattern, path)
            if m == method and match:
                break
        else:
            raise AssertionError(f"假飞书没有实现 {method} {path}")
        if name != "token":
            self.calls.append((name, path, query, body))
        queued = self.fail.get(name)
        payload = queued.pop(0) if queued else getattr(self, f"_{name}")(query, body, **match.groupdict())
        raw = RawResponse()
        raw.status_code, raw.headers = 200, {}
        raw.content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return raw

    # —— 鉴权 ——
    def _token(self, query, body):
        return {"code": 0, "msg": "ok", "tenant_access_token": "t-fake", "expire": 7200}

    # —— 日历 ——
    def _create_calendar(self, query, body):
        cid = self._id("feishu.cn_cal") + "@group.calendar.feishu.cn"
        self.calendars[cid] = {**body, "calendar_id": cid, "type": "shared", "role": "owner", "is_deleted": False}
        self.acls[cid] = []
        return ok({"calendar": self.calendars[cid]})

    def _get_calendar(self, query, body, cid):
        if cid not in self.calendars:
            return {"code": 191000, "msg": "calendar not found"}
        return ok(self.calendars[cid])

    def _list_acls(self, query, body, cid):
        assert query.get("user_id_type") == "open_id"
        return ok({"acls": self.acls[cid], "has_more": False})

    def _create_acl(self, query, body, cid):
        acl = {"acl_id": self._id("user_"), **body}
        self.acls[cid].append(acl)
        return ok(acl)

    def _create_event(self, query, body, cid):
        if cid not in self.calendars:
            return {"code": 191000, "msg": "calendar not found"}
        key = query.get("idempotency_key")
        assert key and 32 <= len(key) <= 128
        if (cid, key) in self.idem:
            eid = self.idem[(cid, key)]
            return ok({"event": {**self.events.get((cid, eid), {}), "event_id": eid}})
        eid = self._id("evt_") + "_0"
        self.idem[(cid, key)] = eid
        self.events[(cid, eid)] = dict(body)
        return ok({"event": {**body, "event_id": eid, "organizer_calendar_id": cid}})

    def _patch_event(self, query, body, cid, eid):
        if (cid, eid) not in self.events:
            return {"code": 193001, "msg": "event not found"}
        self.events[(cid, eid)].update(body)
        return ok({"event": {**self.events[(cid, eid)], "event_id": eid}})

    def _delete_event(self, query, body, cid, eid):
        assert query.get("need_notification") == "false"
        if self.events.pop((cid, eid), None) is None:
            return {"code": 193003, "msg": "event deleted"}
        return ok()

    def _add_attendees(self, query, body, cid, eid):
        assert body["need_notification"] is False and query.get("user_id_type") == "open_id"
        self.attendees.setdefault((cid, eid), []).extend(a["user_id"] for a in body["attendees"])
        return ok({"attendees": body["attendees"]})

    # —— 多维表格 ——
    def _create_app(self, query, body):
        token = self._id("bascn")
        default = self._id("tblDefault")
        self.apps[token] = {"name": body["name"], "time_zone": body.get("time_zone"),
                            "tables": {default: {"name": "数据表", "fields": [{"field_name": "多行文本", "type": 1}],
                                                 "records": {}}}}
        return ok({"app": {"app_token": token, "name": body["name"], "url": f"https://x.feishu.cn/base/{token}",
                           "default_table_id": default, "time_zone": body.get("time_zone")}})

    def _get_app(self, query, body, app):
        if app not in self.apps:
            return {"code": 1254040, "msg": "app_token not found"}
        return ok({"app": {"app_token": app, "name": self.apps[app]["name"]}})

    def _create_table(self, query, body, app):
        tid = self._id("tbl")
        fields = [dict(f, field_id=self._id("fld")) for f in body["table"]["fields"]]
        self.apps[app]["tables"][tid] = {"name": body["table"]["name"], "fields": fields, "records": {},
                                         "view": body["table"]["default_view_name"]}
        return ok({"table_id": tid, "default_view_id": "vew1", "field_id_list": [f["field_id"] for f in fields]})

    def _delete_table(self, query, body, app, tid):
        del self.apps[app]["tables"][tid]
        return ok()

    def _table(self, app, tid):
        return self.apps.get(app, {}).get("tables", {}).get(tid)

    def _list_fields(self, query, body, app, tid):
        t = self._table(app, tid)
        if t is None:
            return {"code": 1254041, "msg": "table_id not found"}
        return ok({"items": t["fields"], "has_more": False, "total": len(t["fields"])})

    def _create_field(self, query, body, app, tid):
        f = dict(body, field_id=self._id("fld"))
        self._table(app, tid)["fields"].append(f)
        return ok({"field": f})

    def _batch_create(self, query, body, app, tid):
        assert uuid.UUID(query["client_token"]).version == 4  # 文档要求 UUIDv4 格式
        t = self._table(app, tid)
        names = {f["field_name"] for f in t["fields"]}
        out = []
        for rec in body["records"]:
            unknown = set(rec["fields"]) - names
            if unknown:
                return {"code": 1254045, "msg": f"FieldNameNotFound {unknown}"}
            rid = self._id("rec")
            t["records"][rid] = dict(rec["fields"])
            out.append({"record_id": rid, "id": rid, "fields": rec["fields"]})
        return ok({"records": list(reversed(out))})  # 文档不保证顺序：故意打乱

    def _batch_update(self, query, body, app, tid):
        t = self._table(app, tid)
        for rec in body["records"]:
            if rec["record_id"] not in t["records"]:
                return {"code": 1254043, "msg": "RecordIdNotFound"}
        for rec in body["records"]:
            t["records"][rec["record_id"]].update(rec["fields"])
        return ok({"records": body["records"]})

    def _search(self, query, body, app, tid):
        t = self._table(app, tid)
        types = {f["field_name"]: f["type"] for f in t["fields"]}
        items = []
        for rid, fields in t["records"].items():
            shown = {}
            for k in body.get("field_names") or fields:
                v = fields.get(k)
                if v is None:
                    continue
                shown[k] = [{"type": "text", "text": v}] if types.get(k) == 1 and isinstance(v, str) else v
            items.append({"record_id": rid, "fields": shown})
        return ok({"items": items, "has_more": False, "total": len(items)})

    # —— 权限 / 消息 ——
    def _add_member(self, query, body, token):
        self.members.append((token, query.get("type"), body))
        return ok({"member": body})

    def _list_messages(self, query, body):
        assert query["container_id_type"] == "chat" and query["sort_type"] == "ByCreateTimeDesc"
        return ok({"items": [{"message_id": f"om_{i}", "sender": s} for i, s in enumerate(self.senders)],
                   "has_more": False})

    def _chat_members(self, query, body, chat):
        return ok({"items": [{"member_id_type": "open_id", "member_id": m} for m in self.chat_member_list],
                   "has_more": False})


@pytest.fixture
def fake(monkeypatch):
    srv = FakeFeishu()
    monkeypatch.setattr(Transport, "execute", staticmethod(srv.execute))
    monkeypatch.setattr(FeishuApi, "RETRY_DELAY", 0.0)
    return srv


@pytest.fixture
def fs_settings(settings):
    settings.secrets = Secrets(feishu_app_id="cli_test", feishu_app_secret="secret", feishu_chat_id="oc_me")
    return settings


@pytest.fixture
def storage(fs_settings):
    st = Storage(fs_settings.resolve_path(fs_settings.storage.sqlite_path))
    yield st
    st.close()


def api() -> FeishuApi:
    return FeishuApi("cli_test", "secret")


def earnings(symbol="MU", day=date(2026, 10, 28), **kw) -> Event:
    return Event(kind="earnings", symbol=symbol, day=day, title=kw.pop("title", f"{symbol} 2026 年三季度业绩"),
                 key=earnings_key(symbol, day), **kw)


def seed_events(st: Storage) -> list[Event]:
    evs = [
        earnings("SKHY", date(2026, 10, 27), at=utc(2026, 10, 28, 0, 0), confirmed=True, source="sec_6k",
                 url="https://www.sec.gov/x", details={"local_time": "2026-10-28 09:00 KST", "conference_call": True}),
        earnings("MU", date(2026, 12, 17), time_hint="盘后", source="yfinance", details={"eps_avg": 3.1}),
        Event(kind="insider_sale", symbol="SNDK", day=date(2026, 10, 20), title="闪迪 内部人拟减持：张三 1 万股",
              key="insider_sale:SNDK:0001", confirmed=True, source="sec_144",
              details={"person": "张三", "shares": 10000, "market_value_usd": 1_500_000}),
    ]
    for e in evs:
        st.upsert_event(e, NOW - timedelta(days=2))
    return evs


# ───────────────────────── 日历 ─────────────────────────


async def test_ensure_calendar_is_idempotent_and_shared_once(fs_settings, storage, fake):
    cal = CalendarSync(fs_settings, storage, api())
    cid, created = await cal.ensure_calendar("ou_user_test")
    assert created and fake.calendars[cid]["permissions"] == "show_only_free_busy"  # private 的日历订阅不了
    assert fake.calendars[cid]["summary"] == "tickersentinel · 事件日历"
    assert fake.acls[cid] == [{"acl_id": "user_2", "role": "reader", "scope": {"type": "user", "user_id": "ou_user_test"}}]
    cid2, created2 = await CalendarSync(fs_settings, storage, api()).ensure_calendar("ou_user_test")
    assert (cid2, created2) == (cid, False)
    assert fake.names() == ["create_calendar", "list_acls", "create_acl", "get_calendar"]  # 第二次只确认日历还在


async def test_existing_acl_is_not_duplicated(fs_settings, storage, fake):
    cal = CalendarSync(fs_settings, storage, api())
    cid, _ = await cal.ensure_calendar()
    fake.acls[cid].append({"acl_id": "x", "role": "writer", "scope": {"type": "user", "user_id": "ou_user_test"}})
    assert await cal.share(cid, "ou_user_test") is False  # 已经是更高的角色
    assert "create_acl" not in fake.names()


async def test_event_create_update_delete_and_all_day_vs_timed(fs_settings, storage, fake):
    skhy, mu, sale = seed_events(storage)
    cal = CalendarSync(fs_settings, storage, api())
    res = await cal.sync(NOW, open_id="ou_user_test")
    assert (res.created, res.updated, res.deleted, res.errors) == (3, 0, 0, [])
    bodies = {b["summary"]: b for b in fake.events.values()}
    timed = bodies["【财报】SKHY 2026 年三季度业绩"]
    assert timed["start_time"] == {"timestamp": str(int(skhy.at.timestamp())), "timezone": "America/Chicago"}
    assert int(timed["end_time"]["timestamp"]) - int(timed["start_time"]["timestamp"]) == 30 * 60
    assert "已确认（来源：SEC 6-K 公告）" in timed["description"] and "10-27 19:00 CDT" in timed["description"]
    assert "当地时间：2026-10-28 09:00 KST" in timed["description"] and "链接：https://www.sec.gov/x" in timed["description"]
    allday = bodies["【财报】MU 2026 年三季度业绩（盘后，预计）"]
    assert allday["start_time"] == {"date": "2026-12-17"} and allday["end_time"] == {"date": "2026-12-18"}  # 结束日不含
    assert "预计日期" in allday["description"] and allday["free_busy_status"] == "free"
    assert allday["need_notification"] is False and "timezone" not in allday["start_time"]
    sale_body = bodies["【拟减持】闪迪 内部人拟减持：张三 1 万股"]
    assert "拟出售：10,000 股，约 $1.5M" in sale_body["description"]

    fake.calls.clear()
    res = await cal.sync(NOW)
    assert (res.created, res.updated, res.unchanged) == (0, 0, 3) and fake.names() == ["get_calendar"]  # 没变化不写

    # 改期 → 更新同一个日程；事件从 events 表消失（过时的估计被清掉）→ 删除日程
    storage.upsert_event(earnings("MU", date(2026, 12, 18), time_hint="盘后", source="yfinance",
                                  details={"eps_avg": 3.1}), NOW)
    storage._exec("DELETE FROM events WHERE key = ?", (sale.key,))
    fake.calls.clear()
    res = await cal.sync(NOW)
    assert (res.created, res.updated, res.deleted, res.unchanged) == (0, 1, 1, 1)
    assert sorted(fake.names()) == ["delete_event", "get_calendar", "patch_event"]
    assert {b["start_time"].get("date") for b in fake.events.values()} == {None, "2026-12-18"}
    assert len(fake.events) == 2 and sale.key not in storage.feishu_calendar_map(cal.calendar_id)

    # 同一个事件原样回来（手工录入注释掉又恢复、过时估计被删后又报回来）：要换幂等键重建，
    # 不能拿到已删除的那个日程（飞书对重复的幂等键会返回原来的日程）
    storage.upsert_event(sale, NOW)
    res = await cal.sync(NOW)
    eid = storage.feishu_calendar_map(cal.calendar_id)[sale.key][0]
    assert res.created == 1 and (cal.calendar_id, eid) in fake.events and len(fake.events) == 3


async def test_event_deleted_in_feishu_is_recreated_and_calendar_recreated(fs_settings, storage, fake):
    seed_events(storage)
    cal = CalendarSync(fs_settings, storage, api())
    await cal.sync(NOW)
    cid = cal.calendar_id
    (key, (eid, _)), *_ = storage.feishu_calendar_map(cid).items()
    del fake.events[(cid, eid)]  # 有人在飞书里删掉了一个日程
    storage.set_feishu_calendar_event(cid, key, eid, "stale")  # 内容也变了 → 更新时发现日程不在了
    res = await cal.sync(NOW)
    assert (res.created, res.updated) == (1, 0) and storage.feishu_calendar_map(cid)[key][0] != eid

    del fake.calendars[cid]  # 整个日历被删了：新建一个，所有日程重建
    res = await cal.sync(NOW)
    assert cal.calendar_id != cid and res.created == 3


async def test_invite_user_adds_attendee_without_notification(fs_settings, storage, fake):
    fs_settings.notify.feishu_sync.invite_user = True
    seed_events(storage)
    cal = CalendarSync(fs_settings, storage, api())
    await cal.sync(NOW, open_id="ou_user_test")
    assert len(fake.attendees) == 3 and all(v == ["ou_user_test"] for v in fake.attendees.values())


async def test_event_body_title_and_hint():
    from tickersentinel.config import load_settings

    s = load_settings(TEST_CONFIG, load_env=False)
    body = event_body(Event(kind="lockup_expiry", symbol="SKHY", day=date(2027, 1, 5), title="SK海力士 ADR 锁定期到期",
                            key="lockup_expiry:SKHY:x", confirmed=True, source="config",
                            details={"note": "承销协议 <180 天>"}), s)
    assert body["summary"] == "【解禁】SK海力士 ADR 锁定期到期"
    assert "备注：承销协议 ＜180 天＞" in body["description"] and "手工录入" in body["description"]


# ───────────────────────── 多维表格 ─────────────────────────


def record_alert(st: Storage, symbol="MU", minutes=0, ts=None) -> int:
    a = Alert(symbol=symbol, ts=(ts or NOW) + timedelta(minutes=minutes), price=97.0, prev_close=100.0,
              triggers=[Trigger("rapid", -1, -2.6, window_minutes=15, z=-5.2)], window_minutes=15, move_pct=-2.6,
              benchmark_move_pct=-1.0, beta=1.4, abnormal_move_pct=-1.2, volume_ratio=3.04)
    aid = st.record_alert(a, "2026-10-21")
    st.set_alert_message_id(aid, "om_1")
    return aid


async def test_bitable_setup_creates_table_fields_and_shares(fs_settings, storage, fake):
    bt = BitableSync(fs_settings, storage, api())
    app, table = await bt.ensure("ou_user_test")
    assert fake.apps[app]["time_zone"] == "America/Chicago" and list(fake.apps[app]["tables"]) == [table]  # 自带空表已删
    fields = fake.apps[app]["tables"][table]["fields"]
    assert fields[0] == {"field_name": "异动", "type": 1, "field_id": fields[0]["field_id"]}  # 首列：文本
    names = [f["field_name"] for f in fields]
    for n in ("时间", "标的", "较参照价%", "参照价", "窗口涨跌%", "板块部分%", "个股特异%", "归因结论", "驱动", "原因明细",
              "推送状态", "label 真实原因", "notes 备注", "alert_id"):
        assert n in names
    assert fake.members == [(app, "bitable", {"member_type": "openid", "member_id": "ou_user_test",
                                               "perm": "full_access", "type": "user"})]
    assert bt.url == f"https://x.feishu.cn/base/{app}"

    # 新进程：表格还在，只确认、补上缺的列，不重复共享
    fake.apps[app]["tables"][table]["fields"] = [f for f in fields if f["field_name"] != "量比"]
    fake.calls.clear()
    assert await BitableSync(fs_settings, storage, api()).ensure("ou_user_test") == (app, table)
    assert fake.names() == ["get_app", "list_fields", "create_field"]
    assert fake.calls[-1][3]["field_name"] == "量比"


async def test_alert_rows_insert_update_and_label_pull(fs_settings, storage, fake):
    a1 = record_alert(storage)
    bt = BitableSync(fs_settings, storage, api())
    res = await bt.sync_alerts()
    assert (res.created, res.updated) == (1, 0)
    app, table = bt.ids
    (rid, fields), = fake.apps[app]["tables"][table]["records"].items()
    assert fields[F_TITLE] == "MU 15分钟急跌 -2.6%" and fields[F_ID] == a1 and fields[F_STATUS] == "已推送"
    assert fields["较参照价%"] == -3.0 and fields["参照价"] == "较昨收" and fields["板块部分%"] == -1.4 and fields["量比"] == 3.0
    assert F_SUMMARY not in fields and F_LABEL not in fields

    assert (await bt.sync_alerts()).unchanged == 1  # 没变化不写
    fake.calls.clear()

    # 归因完成 → 更新同一行（只写程序维护的列）；新异动 → 插入
    storage.save_attribution(a1, "**结论**：板块拖累，见 [Reuters](https://r.example/x)",
                             {"summary": "板块拖累为主", "driver": "板块", "causes": [], "watch": ""})
    a2 = record_alert(storage, "SNDK", minutes=5)
    storage.set_alert_label(a2, "库里已有的标注", None)  # 插入时一并写上
    fake.apps[app]["tables"][table]["records"][rid][F_LABEL] = "你填的标注"
    res = await bt.sync_alerts()
    assert (res.created, res.updated) == (1, 1)
    rid2 = storage.feishu_bitable_map(app, table)[a2][0]
    assert fake.apps[app]["tables"][table]["records"][rid2][F_LABEL] == "库里已有的标注"
    row = fake.apps[app]["tables"][table]["records"][rid]
    assert row[F_SUMMARY] == "板块拖累为主" and row[F_DRIVER] == "板块"
    assert row[F_DETAIL] == "结论：板块拖累，见 Reuters https://r.example/x"
    assert row[F_LABEL] == "你填的标注"  # 更新不碰你填的列
    update = next(c for c in fake.calls if c[0] == "batch_update")[3]
    assert all(F_LABEL not in r["fields"] and F_NOTES not in r["fields"] for r in update["records"])

    # 拉回标注：label 写回 alerts 表；飞书里清空的单元格不覆盖库里已有的标注（列被改名后补建的空列不能清空标注）
    pulled = await bt.pull_labels()
    assert (pulled.changed, pulled.records) == (1, 2)
    assert storage.alert_labels()[a1] == ("你填的标注", None) and storage.alert_labels()[a2] == ("库里已有的标注", None)
    fake.apps[app]["tables"][table]["records"][rid][F_LABEL] = None
    fake.apps[app]["tables"][table]["records"][rid][F_NOTES] = "  盘后才出公告 "
    await bt.pull_labels()
    assert storage.alert_labels()[a1] == ("你填的标注", "盘后才出公告")


async def test_rows_deleted_in_feishu_are_not_resurrected(fs_settings, storage, fake):
    from tickersentinel.timeutil import utcnow

    a1 = record_alert(storage)
    a2 = record_alert(storage, "SNDK", minutes=5)
    bt = BitableSync(fs_settings, storage, api())
    await bt.sync_alerts()
    app, table = bt.ids
    rid1 = storage.feishu_bitable_map(app, table)[a1][0]
    del fake.apps[app]["tables"][table]["records"][rid1]  # 你在飞书里删了一行

    # 还没拉取就要更新：批量更新报“记录不存在”→ 逐条更新，删掉的那行标记为 deleted，另一行照常更新
    storage.save_attribution(a1, "原因一", {"summary": "一", "driver": "个股"})
    storage.save_attribution(a2, "原因二", {"summary": "二", "driver": "个股"})
    res = await bt.sync_alerts()
    assert (res.updated, res.skipped_deleted, res.errors) == (1, 1, [])
    assert storage.feishu_bitable_map(app, table)[a1][1] == DELETED
    assert len(fake.apps[app]["tables"][table]["records"]) == 1
    res = await bt.sync_alerts()
    assert res.skipped_deleted == 1 and res.created == 0

    # 拉取时发现被删的行也会标记
    rid2 = storage.feishu_bitable_map(app, table)[a2][0]
    del fake.apps[app]["tables"][table]["records"][rid2]
    # 刚写入（10 分钟内）的行检索不到时先不判定：飞书检索可能有延迟，误判后这一行就永远不再更新
    assert (await bt.pull_labels()).deleted == 0 and storage.feishu_bitable_map(app, table)[a2][1] != DELETED
    assert (await bt.pull_labels(now=utcnow() + timedelta(minutes=11))).deleted == 1


def test_cell_text_shapes():
    assert cell_text([{"type": "text", "text": "财报"}, {"type": "text", "text": "预告"}]) == "财报预告"
    assert cell_text("  ") is None and cell_text(None) is None and cell_text({"text": "x"}) == "x"


# ───────────────────────── 错误与指引 ─────────────────────────


def test_no_scope_guidance_from_real_error_message():
    raw = fixture("error_no_scope_calendar.json")
    err = FeishuError(raw["code"], raw["msg"], "查询日历", area="calendar")
    assert err.fatal and err.scopes == ["calendar:calendar:readonly", "calendar:calendar",
                                        "calendar:calendar.calendar:readonly", "calendar:calendar:read"]
    assert err.link.startswith("https://open.feishu.cn/app/cli_test/auth?q=calendar:calendar:readonly")
    text = err.guidance()
    assert "权限管理" in text and "calendar:calendar" in text and "发布一个新版本" in text and err.link in text
    # 错误信息里没有权限列表时，按接口给出建议
    assert "bitable:app" in FeishuError(99991672, "Access denied", "创建多维表格", area="bitable").guidance()
    assert "calendar-setup" in FeishuError(191000, "calendar not found").guidance()
    assert "FEISHU_APP_SECRET" in FeishuError(99991663, "invalid token").guidance()


async def test_permission_error_stops_sync_with_guidance(fs_settings, storage, fake):
    fake.fail["create_calendar"] = [fixture("error_no_scope_calendar.json")]
    with pytest.raises(FeishuError) as info:
        await CalendarSync(fs_settings, storage, api()).sync(NOW)
    assert info.value.code == 99991672 and "calendar:calendar" in info.value.guidance()

    fake.fail["create_app"] = [fixture("error_no_scope_bitable.json")]
    with pytest.raises(FeishuError, match="99991672"):
        await BitableSync(fs_settings, storage, api()).sync_alerts()


async def test_rate_limit_is_retried(fs_settings, storage, fake):
    fake.fail["create_calendar"] = [{"code": 190004, "msg": "method rate limited"},
                                    {"code": 1254291, "msg": "write conflict"}]
    cid, created = await CalendarSync(fs_settings, storage, api()).ensure_calendar()
    assert created and fake.names() == ["create_calendar"] * 3


async def test_find_user_open_id(fs_settings, storage, fake):
    a = api()
    assert await find_user_open_id(a, "oc_me") == "ou_user_test"  # 跳过机器人自己发的推送
    saved = list(fake.senders)
    fake.senders = [{"sender_type": "user", "id_type": "open_id", "id": "ou_colleague"}, *saved]
    assert await find_user_open_id(a, "oc_me") is None  # 群里有两个人发过言：不能选最后发言的人
    fake.senders = saved
    fake.chat_member_list = ["ou_user_test", "ou_colleague"]
    assert await find_user_open_id(a, "oc_me") is None  # 只有一个人发过言，但群里有多个成员：也不猜
    fake.senders = [s for s in fake.senders if s["sender_type"] == "app"]
    fake.chat_member_list = ["ou_from_members"]
    assert await find_user_open_id(a, "oc_me") == "ou_from_members"
    fake.chat_member_list = ["ou_a", "ou_b"]
    assert await find_user_open_id(a, "oc_me") is None  # 群里多个人：没法判断
    fake.fail["list_messages"] = [{"code": 99991672, "msg": "Access denied. [im:message.p2p_msg:readonly]"}]
    fake.fail["chat_members"] = [fixture("error_no_scope_chat_members.json")]
    with pytest.raises(FeishuError) as info:
        await find_user_open_id(a, "oc_me")
    assert info.value.scopes == ["im:message.p2p_msg:readonly"]

    fs = FeishuSync.from_settings(fs_settings, storage)
    assert await fs.user_open_id() is None  # 会话里只有机器人发的消息，群里又有多个人
    n = len(fake.calls)
    assert await fs.user_open_id() is None and len(fake.calls) == n  # 本进程找过一次就不再查
    assert await fs.user_open_id(override="ou_manual") == "ou_manual"
    assert await FeishuSync.from_settings(fs_settings, storage).user_open_id() == "ou_manual"  # 保存在 kv 里


# ───────────────────────── App 与命令行 ─────────────────────────


async def test_app_hooks_sync_calendar_and_alerts_and_report_failure_once(fs_settings, fake):
    from tickersentinel.app import App
    from tickersentinel.config import LLMTasks, ManualEvent
    from tickersentinel.timeutil import utcnow

    s = fs_settings
    s.sources.news.google_news_rss.enabled = False
    s.sources.news.finnhub.enabled = False
    s.sources.filings.sec_edgar.enabled = False
    s.events.sources.yfinance = s.events.sources.jpx = s.events.sources.sec_6k = s.events.sources.sec_144 = False
    s.llm.tasks = LLMTasks(brief=[], attribution=[], events=[], chat=[])
    for dry_run in (False, True):  # 默认关闭；打开了，预览（内存数据库，没有映射）也从不同步
        app = App(s, dry_run=dry_run)
        assert app.feishu is None
        await app.aclose()
        s.notify.feishu_sync.calendar = s.notify.feishu_sync.bitable = True

    s.events.manual = [ManualEvent(symbol="SKHY", day=date(2026, 11, 3), title="SK海力士 ADR 锁定期到期",
                                   kind="lockup_expiry")]
    app = App(s)
    sent = []

    async def capture(text):
        sent.append(text)

    app._notify_error = capture
    app.feishu.on_error = capture
    try:
        await app.refresh_events(NOW)  # 事件刷新之后同步日历
        assert [b["summary"] for b in fake.events.values()] == ["【解禁】SK海力士 ADR 锁定期到期"]
        cal = next(iter(fake.calendars))
        assert fake.acls[cal][0]["scope"]["user_id"] == "ou_user_test"  # 从会话记录里找到你并共享

        record_alert(app.storage, ts=utcnow())
        app.spawn_feishu_alerts()  # 盘中检测之后：后台写进多维表格
        await app.feishu.drain()
        (app_token, tables), = [(k, v["tables"]) for k, v in fake.apps.items()]
        assert sum(len(t["records"]) for t in tables.values()) == 1

        # 缺权限：只提醒一次，30 分钟内不再尝试
        fake.calls.clear()
        fake.fail["batch_create"] = [fixture("error_no_scope_bitable.json")]
        record_alert(app.storage, "SNDK", minutes=1)
        await app.sync_feishu_alerts(NOW)
        await app.sync_feishu_alerts(NOW + timedelta(minutes=15))
        assert len(sent) == 1 and "飞书多维表格同步失败" in sent[0] and "bitable:app" in sent[0]
        assert fake.names().count("batch_create") == 1
        await app.sync_feishu_alerts(NOW + timedelta(minutes=31))  # 退避期过了：重试成功
        assert sum(len(t["records"]) for t in tables.values()) == 2 and len(sent) == 1
    finally:
        await app.aclose()


async def test_cli_setup_and_sync_print_links_and_guidance(fs_settings, storage, fake, capsys, monkeypatch):
    from tickersentinel.cli import cmd_feishu_sync

    monkeypatch.setattr("tickersentinel.feishu.calendar_sync.utcnow", lambda: NOW)

    seed_events(storage)
    record_alert(storage)
    await cmd_feishu_sync(fs_settings, "sync")
    assert "还没有设置飞书日历或多维表格" in capsys.readouterr().out

    await cmd_feishu_sync(fs_settings, "calendar-setup")
    out = capsys.readouterr().out
    assert "你的 open_id：ou_user_test" in out and "已新建飞书日历「tickersentinel · 事件日历」" in out
    assert "新建 " in out and "订阅日历" in out and "notify.feishu_sync.calendar 改成 true" in out

    await cmd_feishu_sync(fs_settings, "bitable-setup")
    out = capsys.readouterr().out
    assert "https://x.feishu.cn/base/" in out and "新增 1" in out and "label 真实原因" in out

    await cmd_feishu_sync(fs_settings, "sync")
    out = capsys.readouterr().out
    assert "✅ 飞书日历：新建 0、更新 0、删除 0、未变 3" in out and "拉回标注：0 条有变化（表格共 1 行）" in out

    fake.fail["get_calendar"] = [fixture("error_no_scope_calendar.json")]
    await cmd_feishu_sync(fs_settings, "sync")
    out = capsys.readouterr().out
    assert "❌ 飞书日历：飞书应用缺少权限" in out and "https://open.feishu.cn/app/cli_test/auth" in out
    assert "✅ 飞书多维表格" in out  # 一部分失败不影响另一部分

    fs_settings.secrets = Secrets()
    await cmd_feishu_sync(fs_settings, "sync")
    assert "FEISHU_APP_ID" in capsys.readouterr().out


def test_config_defaults_off():
    from tickersentinel.config import load_settings

    cfg = load_settings(TEST_CONFIG, load_env=False).notify.feishu_sync
    assert (cfg.calendar, cfg.bitable, cfg.invite_user, cfg.calendar_role) == (False, False, False, "reader")


async def test_per_record_errors_count_as_failure_and_closing_stops_new_runs(fs_settings, storage, fake):
    """单条写入失败（如「时间」列被改成文本，1254060）也要提醒一次并退避，不能每分钟静默重试；退出中不再开始新的同步。"""
    fs_settings.notify.feishu_sync.bitable = True
    sent = []

    async def capture(text):
        sent.append(text)

    fs = FeishuSync.from_settings(fs_settings, storage, api=api(), on_error=capture)
    await fs.bitable.ensure()
    record_alert(storage)
    fake.calls.clear()
    fake.fail["batch_create"] = [{"code": 1254060, "msg": "TextFieldConvFail"}] * 3
    await fs.run_alerts(now=NOW)
    await fs.run_alerts(now=NOW + timedelta(minutes=5))  # 退避期内：不再尝试
    assert len(sent) == 1 and "写入失败" in sent[0] and fake.names().count("batch_create") >= 1
    n = fake.names().count("batch_create")
    fs._blocked_until.clear()
    fs.closing = True
    await fs.run_alerts(now=NOW + timedelta(hours=1))
    assert fake.names().count("batch_create") == n and len(sent) == 1


def test_calendar_only_syncs_events_in_the_monitoring_scope(fs_settings, storage):
    """移除一个主题后，它的同业和相关公司的事件从飞书日历撤掉（和汇总、简报一致）；宏观日程、手工录入的总在。"""
    now = utc(2026, 10, 21, 15)
    storage.upsert_event(earnings("MU"), now)
    storage.upsert_event(earnings("INTC", title="英特尔 业绩"), now)  # 不在任何主题里
    storage.upsert_event(Event(kind="macro", symbol="FED", day=date(2026, 10, 28), title="议息决议", source="fed",
                               key="macro:FED:fomc:2026-7"), now)
    keys = {e.symbol for e in CalendarSync(fs_settings, storage, api()).events_to_sync(now)}
    assert "MU" in keys and "FED" in keys and "INTC" not in keys
