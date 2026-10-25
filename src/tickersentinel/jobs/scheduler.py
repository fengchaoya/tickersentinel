"""日程规划：每天 00:05（本地时间）根据交易日历安排当天的任务。

- 盘前简报：本地固定时间（默认 07:30），休市日可跳过；
- 异动监控：全天候每 interval_seconds 检测一次（一个常驻任务，不按天规划；哪些资产此刻在交易由监控自己按时段判断，
  见 detect/clock.py：美股盘前 / 盘中 / 盘后、海外交易所、CME 期货、加密货币）；
- 收盘简报：收盘后 N 分钟（提前收盘日自动提前，例如感恩节次日）；
- 周报：每周固定时间。
- 事件日历：每天 events.collect_times 刷新（财报日、内部人拟减持……），events.digest.time 汇总推送；
- 看门狗（每 15 分钟，启动 10 秒后第一次）：补上错过的事件刷新、事件汇总和简报（Mac 休眠、程序重启晚于定时点、生成失败）。简报每种每天最多一份，所以看门狗和正常的定时任务不会重复发送。
说明：按本地日期规划，适用于美洲时区（本项目默认 America/Chicago）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from tickersentinel.timeutil import UTC, fmt_local, parse_hhmm, utcnow

if TYPE_CHECKING:
    from tickersentinel.app import App

log = logging.getLogger(__name__)

PLAN_MINUTE = 5  # 每天 00:05（本地时间）规划当天


class Planner:
    def __init__(self, app: "App", scheduler: AsyncIOScheduler):
        self.app = app
        self.sched = scheduler
        self.settings = app.settings

    def install(self) -> list[str]:
        tz = self.settings.tz
        # 错过了就补做（不设宽限上限）：Mac 休眠跨过 00:05、醒来已过 06:05 时，原来 6 小时的宽限会让当天整天没有
        # 盘中监控、却照常补发简报，用户察觉不到（2026-10-21 审查实测）。coalesce：睡了几天也只补一次
        self.sched.add_job(self._plan_job, CronTrigger(hour=0, minute=PLAN_MINUTE, timezone=tz), id="planner", replace_existing=True,
                           misfire_grace_time=None, coalesce=True)
        weekly = self.settings.briefs.weekly
        if weekly.enabled:
            h, m = parse_hhmm(weekly.time)
            self.sched.add_job(
                self.app.run_brief, CronTrigger(day_of_week=weekly.day_of_week, hour=h, minute=m, timezone=tz),
                args=["weekly"], id="brief:weekly", replace_existing=True, misfire_grace_time=1800, coalesce=True,
            )
        planned = self.plan_today()
        planned += self._install_monitor()
        planned += self._install_events()
        # 看门狗也不设宽限上限：macOS 上调度器的单调时钟休眠时不走，醒来后第一次触发几乎总被判为“错过”（默认宽限 1 秒）
        # 而丢掉，补发要再等 15 分钟（审查实测）
        self.sched.add_job(self.watchdog, IntervalTrigger(minutes=15, start_date=utcnow() + timedelta(seconds=10),
                                                         timezone=UTC),
                           id="catch_up", replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=None)
        return planned

    async def _plan_job(self) -> None:
        """00:05 的规划放在主循环里跑（同步函数会被放进线程池，和看门狗抢着规划、打出误导性的告警）。"""
        self.plan_today()

    def _install_monitor(self) -> list[str]:
        """异动监控是一个全天候的常驻任务：Mac 休眠醒来后接着跑（错过的那几次合并成一次），不依赖每天的规划。"""
        mon = self.settings.monitor
        if not mon.enabled:
            return []
        self.sched.add_job(self.app.monitor_tick, IntervalTrigger(seconds=mon.interval_seconds,
                                                                  start_date=utcnow() + timedelta(seconds=15), timezone=UTC),
                           id="monitor:tick", replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=None)
        monitor = getattr(self.app, "monitor", None)
        lines = monitor.describe() if monitor is not None else []
        return [f"异动监控（每 {mon.interval_seconds} 秒，各资产只在自己的交易时段检测）：" + "；".join(lines)]

    async def watchdog(self, now: datetime | None = None) -> None:
        """每 15 分钟：补错过的事件刷新、汇总和简报（App.catch_up）。"""
        await asyncio.sleep(0)  # 醒来时 planner 和看门狗同时到期：先让 00:05 的规划跑完
        await self.app.catch_up(now)

    def _install_events(self) -> list[str]:
        ev, tz = self.settings.events, self.settings.tz
        if not ev.enabled:
            return []
        planned: list[str] = []
        for t in ev.collect_times:
            h, m = parse_hhmm(t)
            self.sched.add_job(self.app.refresh_events, CronTrigger(hour=h, minute=m, timezone=tz), id=f"events:refresh:{t}",
                               replace_existing=True, misfire_grace_time=3600, coalesce=True)
        if ev.digest.enabled:
            h, m = parse_hhmm(ev.digest.time)
            self.sched.add_job(self.app.send_event_digest, CronTrigger(hour=h, minute=m, timezone=tz), id="events:digest",
                               replace_existing=True, misfire_grace_time=3600, coalesce=True)
            planned.append(f"每天 {ev.digest.time} 事件汇总（刷新日历：{'、'.join(ev.collect_times)}）")
        last = self.app.events.last_refresh()
        if last is None or utcnow() - last > timedelta(hours=12):
            planned.append("启动后补刷新一次事件日历")
        return planned

    def plan_today(self) -> list[str]:
        return self.plan(datetime.now(self.settings.tz).date())

    def _add_once(self, func, at: datetime, args: list, job_id: str, desc: str, planned: list[str]) -> None:
        self.sched.add_job(func, DateTrigger(run_date=at), args=args, id=job_id, replace_existing=True,
                           misfire_grace_time=1800)
        planned.append(f"{fmt_local(at, self.settings.tz, '%H:%M')} {desc}")

    def plan(self, day: date, now: datetime | None = None) -> list[str]:
        s, tz, now = self.settings, self.settings.tz, now or utcnow()
        planned: list[str] = []
        session = self.app.calendar.session(day)

        pm = s.briefs.premarket
        if pm.enabled and (session is not None or not pm.trading_days_only):
            h, m = parse_hhmm(pm.time)
            at = datetime.combine(day, time(h, m), tzinfo=tz)
            if at > now:
                self._add_once(self.app.run_brief, at, ["premarket"], f"brief:premarket:{day}", "盘前简报", planned)

        if session is None:
            log.info("%s 美股休市", day)
            return planned

        cb = s.briefs.close
        if cb.enabled:
            at = session.close + timedelta(minutes=cb.minutes_after_close)
            if at > now:
                self._add_once(self.app.run_brief, at, ["close"], f"brief:close:{day}", "收盘简报", planned)
        if session.early_close:
            planned.append(f"（今天提前收盘：{fmt_local(session.close, tz, '%H:%M')}）")
        log.info("%s 日程：%s", day, "；".join(planned) or "无")
        return planned
