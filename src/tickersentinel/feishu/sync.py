"""常驻运行（App）和命令行（tsen feishu ...）共用的入口：找出你的 open_id，调度日历 / 多维表格同步。

App 里的同步失败只记日志，不影响推送和监控：
- 同一部分（日历 / 多维表格）第一次失败时推送一条错误提醒（带开通权限的指引），之后只记日志，恢复后重新计；
- 失败后 RETRY_AFTER 内不再尝试（盘中每分钟都会触发异动同步，不能每分钟撞一次缺权限的错误）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from tickersentinel.config import Settings
from tickersentinel.feishu.bitable_sync import BitableSync, BitableSyncResult, LabelPullResult
from tickersentinel.feishu.calendar_sync import CalendarSync, CalendarSyncResult
from tickersentinel.feishu.client import FeishuApi, FeishuError, find_user_open_id
from tickersentinel.storage import Storage
from tickersentinel.timeutil import utcnow

log = logging.getLogger(__name__)

USER_KEY = "feishu:user_open_id"
RETRY_AFTER = timedelta(minutes=30)
LABEL_PULL_EVERY = timedelta(hours=1)
PART_NAMES = {"calendar": "飞书日历", "bitable": "飞书多维表格"}

ErrorHook = Callable[[str], Awaitable[None]]


def describe_error(exc: BaseException) -> str:
    return exc.guidance() if isinstance(exc, FeishuError) else f"{type(exc).__name__}: {exc}"


class FeishuSync:
    def __init__(self, settings: Settings, storage: Storage, api: FeishuApi, *, on_error: ErrorHook | None = None):
        self.settings = settings
        self.storage = storage
        self.api = api
        self.on_error = on_error
        self.calendar = CalendarSync(settings, storage, api)
        self.bitable = BitableSync(settings, storage, api)
        self._blocked_until: dict[str, datetime] = {}
        self._reported: set[str] = set()
        self._last_pull: datetime | None = None
        self._open_id_looked_up = False
        self._tasks: set[asyncio.Task] = set()
        self.closing = False  # 程序退出中：不再开始新的同步，也不再推送失败提醒

    @classmethod
    def from_settings(cls, settings: Settings, storage: Storage, *, api: FeishuApi | None = None,
                      on_error: ErrorHook | None = None) -> "FeishuSync":
        if api is None:
            sec = settings.secrets
            app_id, app_secret = sec.get("feishu_app_id"), sec.get("feishu_app_secret")
            if not app_id or not app_secret:
                raise ValueError("飞书同步需要在 .env 中填写 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
            api = FeishuApi(app_id, app_secret, settings.notify.feishu_domain)
        return cls(settings, storage, api, on_error=on_error)

    # ───────── 你的 open_id ─────────

    async def user_open_id(self, *, refresh: bool = False, override: str | None = None) -> str | None:
        """你的 open_id（日历成员、多维表格协作者都要用）。优先级：命令行 --open-id > 已保存的 > 从 FEISHU_CHAT_ID 会话里找。"""
        if override:
            self.storage.kv_set(USER_KEY, override.strip())
            return override.strip()
        cached = self.storage.kv_get(USER_KEY)
        if cached and not refresh:
            return cached
        if self._open_id_looked_up and not refresh:  # 本进程已经找过、没找到：不再每次都查
            return cached
        self._open_id_looked_up = True
        chat_id = self.settings.secrets.get("feishu_chat_id")
        if not chat_id:
            return cached
        found = await find_user_open_id(self.api, chat_id)
        if found:
            self.storage.kv_set(USER_KEY, found)
        return found or cached

    # ───────── 直接调用（命令行用：出错抛异常） ─────────

    async def sync_calendar(self, now: datetime | None = None) -> CalendarSyncResult:
        return await self.calendar.sync(now, open_id=await self.user_open_id())

    async def sync_alerts(self, since: datetime | None = None, *, pull_labels: bool = False,
                          now: datetime | None = None) -> tuple[BitableSyncResult, LabelPullResult | None]:
        res = await self.bitable.sync_alerts(since, open_id=await self.user_open_id())
        pulled = None
        if pull_labels:
            pulled = await self.bitable.pull_labels()
            self._last_pull = now or utcnow()
        return res, pulled

    # ───────── 常驻运行用：从不抛异常 ─────────

    async def run_calendar(self, now: datetime | None = None) -> CalendarSyncResult | None:
        return await self._guarded("calendar", lambda: self.sync_calendar(now), now)

    async def run_alerts(self, since: datetime | None = None, *, now: datetime | None = None,
                         pull_labels: bool = True) -> tuple[BitableSyncResult, LabelPullResult | None] | None:
        """pull_labels=True 时最多每 LABEL_PULL_EVERY 拉一次标注。"""
        now = now or utcnow()
        pull = pull_labels and (self._last_pull is None or now - self._last_pull >= LABEL_PULL_EVERY)
        return await self._guarded("bitable", lambda: self.sync_alerts(since, pull_labels=pull, now=now), now)

    def spawn_alerts(self, since: datetime | None = None) -> None:
        """盘中每次检测之后在后台同步最近的异动（不拖慢检测）；上一轮还没跑完就跳过。"""
        if self._tasks:
            return
        task = asyncio.create_task(self.run_alerts(since, pull_labels=False))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self, timeout: float = 30.0) -> None:
        """等后台同步跑完；超时的取消掉。退出前由 App.aclose 先设 closing=True 再调用（之后数据库就要关闭了）。"""
        if self._tasks:
            _, pending = await asyncio.wait(list(self._tasks), timeout=timeout)
            for t in pending:
                t.cancel()
            if pending:
                log.warning("飞书同步 %d 秒内没有完成，已取消（下次启动会重新对账）", timeout)
                await asyncio.gather(*pending, return_exceptions=True)

    async def _guarded(self, part: str, fn: Callable[[], Awaitable[Any]], now: datetime | None) -> Any | None:
        now = now or utcnow()
        if self.closing:
            return None
        until = self._blocked_until.get(part)
        if until is not None and now < until:
            return None
        # 直接调用（看门狗、事件刷新之后）也登记到 _tasks：退出时 drain 会等它写完映射再关数据库
        task = asyncio.current_task()
        tracked = task is not None and task not in self._tasks
        if tracked:
            self._tasks.add(task)
        try:
            out = await fn()
        except Exception as exc:  # noqa: BLE001 - 同步失败不能影响推送和监控
            await self._fail(part, describe_error(exc), now, exc_info=not isinstance(exc, FeishuError))
            return None
        finally:
            if tracked:
                self._tasks.discard(task)
        results = out if isinstance(out, tuple) else (out,)  # run_alerts 返回（写入结果, 拉回结果）
        errors = [e for r in results for e in (getattr(r, "errors", None) or [])]
        if errors:
            # 单条失败（例如有人把「时间」列改成了文本，每次写入都报 1254060）也算失败：提醒一次并退避，
            # 否则盘中每分钟重试一遍、新异动永远进不了表，用户却收不到任何提示（审查实测）
            await self._fail(part, f"{len(errors)} 条写入失败，例如：{errors[0]}", now)
            return out
        self._blocked_until.pop(part, None)
        self._reported.discard(part)
        return out

    async def _fail(self, part: str, text: str, now: datetime, *, exc_info: bool = False) -> None:
        """记一次失败：30 分钟内不再尝试；连续失败只提醒一次（成功一次后重新计）。"""
        self._blocked_until[part] = now + RETRY_AFTER
        if part in self._reported:
            log.info("%s同步仍然失败：%s", PART_NAMES[part], text)
            return
        self._reported.add(part)
        log.warning("%s同步失败（%d 分钟内不再尝试）：%s", PART_NAMES[part], RETRY_AFTER.seconds // 60, text,
                    exc_info=exc_info)
        if self.on_error is not None and not self.closing:
            try:
                await self.on_error(f"{PART_NAMES[part]}同步失败：{text}")
            except Exception:  # pragma: no cover - on_error 自己出错也不能往外抛
                log.debug("飞书同步的错误提醒发送失败", exc_info=True)
