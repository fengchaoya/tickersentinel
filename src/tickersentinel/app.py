"""组装所有模块，并提供常驻运行入口。"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import date, datetime, time, timedelta

from tickersentinel.agents.attribution import Attributor
from tickersentinel.agents.brief import BriefBuilder, BriefKind
from tickersentinel.agents.chat import ChatAgent, IncomingMessage
from tickersentinel.config import Settings
from tickersentinel.events.digest import EventDigest
from tickersentinel.events.hub import EventHub
from tickersentinel.jobs.monitor import IntradayMonitor
from tickersentinel.llm.client import LLMClient
from tickersentinel.market_calendar import MarketCalendar
from tickersentinel.models import Message
from tickersentinel.news.hub import NewsHub
from tickersentinel.notify import build_notifier
from tickersentinel.scope.chat import ScopeChat
from tickersentinel.sources.macro import MacroSource
from tickersentinel.sources import build_event_sources, build_news_sources, build_price_source, build_sec
from tickersentinel.storage import Storage
from tickersentinel.timeutil import parse_hhmm, utcnow

log = logging.getLogger(__name__)

DIGEST_SKIPPED_KEY = "events:digest_skipped"
CATCH_UP_GRACE = timedelta(minutes=10)  # 过了定时点这么久还没发，才算“错过”（给正常的定时任务留时间）
CATCH_UP_ATTEMPTS = 3  # 同一份简报当天最多补发几次（生成失败时不至于每 15 分钟报一次错）
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class App:
    TICK_FAILURE_ALERT = 5  # 盘中检测连续失败多少次后推送一条错误提醒

    def __init__(self, settings: Settings, *, dry_run: bool = False, copy_db: bool = False):
        """dry_run=True：只打印到终端、使用内存数据库，不影响正式运行的状态（已读新闻、冷却记录等）。
        copy_db=True（只在 dry_run 时有效）：内存数据库从正式库复制一份，能读到历史异动、事件日历（tsen ask 预览用），
        写入仍只在内存里。"""
        self.settings = settings
        self.dry_run = dry_run
        path = settings.resolve_path(settings.storage.sqlite_path)
        if not dry_run:
            self.storage = Storage(path)
        else:
            self.storage = Storage.in_memory_copy(path) if copy_db else Storage(":memory:")
        self.calendar = MarketCalendar("XNYS")
        self.prices = build_price_source(settings)
        sec = build_sec(settings)  # 新闻（公告）与事件（Form 144、6-K）共用一个 SEC 客户端，限速全局有效
        self.news = NewsHub(build_news_sources(settings, sec), settings.universe)
        self.events = EventHub(settings, build_event_sources(settings, sec), self.storage)
        self.llm = LLMClient(settings.llm)
        self.digest = EventDigest(settings, self.events, self.storage, self.llm)
        self.notifier = build_notifier(settings, force_console=dry_run, log=not dry_run)
        self.briefs = BriefBuilder(settings, self.calendar, self.prices, self.news, self.events, self.storage, self.llm)
        self.attributor = Attributor(settings, self.news, self.llm, events=self.events)
        self.macro = MacroSource(settings, self.calendar) if settings.macro.enabled else None
        self.monitor = IntradayMonitor(settings, self.calendar, self.prices, self.storage, self.notifier, self.attributor,
                                       macro=self.macro)
        # /scope：在飞书里调监控范围（确认后写入配置并立即生效）；预览（dry_run）只看不写
        self.scope = ScopeChat(settings, self.storage, self.prices, self.llm, on_change=self.apply_scope, readonly=dry_run)
        self.chat = ChatAgent(settings, self.llm, self.storage, prices=self.prices, news=self.news,
                              events=self.events, calendar=self.calendar, macro=self.macro, scope=self.scope)
        self._scope_sig = self.scope_signature()  # 配置文件的修改时间：tsen scope / 手工改了文件，一分钟内自动读到
        self._bg: set[asyncio.Task] = set()
        self._tick_failures = 0
        self._briefs_running: set[str] = set()
        self.feishu = self._build_feishu_sync()  # 飞书日历 / 多维表格同步；未开启时为 None

    async def __aenter__(self) -> "App":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # ───────── 被调度的任务 ─────────

    def brief_sent_today(self, kind: str, now: datetime | None = None) -> bool:
        last = self.storage.last_brief_at((kind,))
        tz = self.settings.tz
        return last is not None and last.astimezone(tz).date() == (now or utcnow()).astimezone(tz).date()

    async def run_brief(self, kind: BriefKind, *, force: bool = False, now: datetime | None = None) -> None:
        """定时任务和看门狗都会调用：同一种简报每天最多发一份、不会同时生成两份。
        force=True（命令行 tsen brief --send）时照发。"""
        now = now or utcnow()
        if not force and (kind in self._briefs_running or self.brief_sent_today(kind, now)):
            log.info("%s 简报今天已经发过（或正在生成），跳过", kind)
            return
        self._briefs_running.add(kind)
        try:
            res = await self.briefs.build(kind, now=now)
            mid = await self.notifier.send(res.message)
            self.storage.mark_seen(res.items, context=kind)
            self.storage.record_brief(kind, res.message.title, res.message.body, mid, at=now)
        except Exception as exc:
            log.exception("生成或发送 %s 简报失败", kind)
            await self._notify_error(f"{kind} 简报失败：{type(exc).__name__}: {exc}")
        finally:
            self._briefs_running.discard(kind)

    async def catch_up(self, now: datetime | None = None) -> None:
        """看门狗（每 15 分钟）：补上因为休眠、重启、生成失败而错过的事件刷新、事件汇总和简报。"""
        now = now or utcnow()
        await self.catch_up_events(now)
        await self.catch_up_briefs(now)
        await self.sync_feishu_alerts(now)  # 收盘后完成的归因、之前失败的写入在这里补上；顺便拉回标注

    def brief_windows(self, now: datetime) -> list[tuple[str, datetime, datetime]]:
        """今天每种简报的（定时点，补发截止时间）：盘前简报只在开盘前补，收盘简报收盘后 6 小时内，周报当天 12 小时内。"""
        b, tz = self.settings.briefs, self.settings.tz
        local = now.astimezone(tz)
        today = local.date()
        session = self.calendar.session(today)
        out: list[tuple[str, datetime, datetime]] = []
        if b.premarket.enabled and (session is not None or not b.premarket.trading_days_only):
            due = datetime.combine(today, time(*parse_hhmm(b.premarket.time)), tzinfo=tz)
            out.append(("premarket", due, session.open if session else due + timedelta(hours=4)))
        if b.close.enabled and session is not None:
            due = session.close + timedelta(minutes=b.close.minutes_after_close)
            out.append(("close", due, due + timedelta(hours=6)))
        if b.weekly.enabled and _WEEKDAYS[local.weekday()] == b.weekly.day_of_week:
            due = datetime.combine(today, time(*parse_hhmm(b.weekly.time)), tzinfo=tz)
            out.append(("weekly", due, due + timedelta(hours=12)))
        return out

    async def catch_up_briefs(self, now: datetime | None = None) -> None:
        now = now or utcnow()
        today = now.astimezone(self.settings.tz).date().isoformat()
        for kind, due, deadline in self.brief_windows(now):
            if not (due + CATCH_UP_GRACE <= now < deadline):
                continue
            if kind in self._briefs_running or self.brief_sent_today(kind, now):
                continue
            key = f"brief_attempts:{kind}:{today}"
            attempts = int(self.storage.kv_get(key) or 0)
            if attempts >= CATCH_UP_ATTEMPTS:
                continue
            self.storage.kv_set(key, str(attempts + 1))
            log.info("补发错过的 %s 简报（原定 %s）", kind, due.astimezone(self.settings.tz).strftime("%H:%M"))
            await self.run_brief(kind, now=now)  # type: ignore[arg-type]

    async def refresh_events(self, now: datetime | None = None) -> None:
        try:
            res = await self.events.refresh(now)
            if res.errors:
                log.warning("部分事件来源失败：%s", res.errors)
            await self.sync_feishu_calendar(now)
        except Exception as exc:
            log.exception("刷新事件日历失败")
            await self._notify_error(f"事件日历刷新失败：{type(exc).__name__}: {exc}")

    async def send_event_digest(self, now: datetime | None = None) -> None:
        """每晚的事件汇总：先刷新一次日历，再看明天有没有事件、有没有新增 / 改期，有才推送。"""
        try:
            await self.events.refresh(now)
            await self.sync_feishu_calendar(now)
            msg = await self.digest.build(now)
            if msg is None:
                log.info("事件日历没有新内容，今晚不推送")
                # 记下“今天已经检查过、无须推送”，看门狗就不会反复补跑
                self.storage.kv_set(DIGEST_SKIPPED_KEY, (now or utcnow()).astimezone(self.settings.tz).date().isoformat())
                return
            await self.notifier.send(msg)
            self.digest.mark_sent()
        except Exception as exc:
            log.exception("事件汇总失败")
            await self._notify_error(f"事件汇总失败：{type(exc).__name__}: {exc}")

    async def catch_up_events(self, now: datetime | None = None) -> None:
        """看门狗（每 15 分钟）：日历超过 12 小时没刷新就刷新；过了汇总时间 4 小时内、今天还没汇总过就补发。
        Mac 休眠时定时任务会整体顺延，错过 1 小时宽限就被跳过，靠这里补上。"""
        ev, tz = self.settings.events, self.settings.tz
        now = now or utcnow()
        last = self.events.last_refresh()
        if last is None or now - last > timedelta(hours=12):
            await self.refresh_events(now)
        if not ev.digest.enabled:
            return
        local = now.astimezone(tz)
        h, m = parse_hhmm(ev.digest.time)
        due = datetime.combine(local.date(), time(h, m), tzinfo=tz)
        handled = self.digest.sent_today(now) or self.storage.kv_get(DIGEST_SKIPPED_KEY) == local.date().isoformat()
        if due <= local < due + timedelta(hours=4) and not handled:
            log.info("补发错过的事件汇总（原定 %s）", ev.digest.time)
            await self.send_event_digest(now)

    # ───────── 监控范围热更新（飞书 /scope 确认后立即生效；命令行 / 手工改了配置文件，一分钟内读到） ─────────

    def scope_signature(self) -> tuple:
        cfg, assets, themes = self.settings.paths
        files = [cfg, assets, *(sorted(themes.glob("*.yaml")) if themes.is_dir() else [])]
        out = []
        for f in files:
            try:
                st = f.stat()
                out.append((str(f), st.st_mtime_ns, st.st_size))
            except OSError:
                continue
        return tuple(out)

    async def apply_scope(self, new: Settings) -> list[str]:
        """换成新的监控范围（只换 universe：assets.yaml 与 config/themes/ 的内容；settings.yaml 里其他设置的改动要重启才生效）。
        返回范围的变化，给读者看。"""
        from tickersentinel.scope.edit import describe_change_effects

        # 只换 universe：阈值变化按“现在的 monitor.price + 新的 assets / 主题”算，和实际生效的一致
        effects = describe_change_effects(self.settings, self.settings.model_copy(update={"universe": new.universe}))
        self.settings.universe = new.universe
        self.news.universe = new.universe
        self.monitor.reload()
        self.events.update_scope()
        if self.macro is not None:
            self.macro._cache = None  # 板块指标跟着主题变
        self._scope_sig = self.scope_signature()
        log.info("监控范围已更新：%s", "；".join(effects) or "没有变化")
        if not self.dry_run:  # 新加的公司马上收集一次事件（后台进行）
            task = asyncio.create_task(self.refresh_events())
            self._bg.add(task)
            task.add_done_callback(self._bg.discard)
        return effects

    async def maybe_reload_scope(self) -> None:
        """配置文件变了就重新读。读不通（正在编辑、写错了）只提醒一次，继续按原来的范围运行，改好后自动读到。"""
        sig = self.scope_signature()
        if sig == self._scope_sig:
            return
        self._scope_sig = sig
        from tickersentinel.config import load_settings

        try:
            new = load_settings(self.settings.paths[0], load_env=False)
        except Exception as exc:
            log.warning("配置文件读不通，还按原来的范围运行：%s", exc)
            await self._notify_error(f"配置文件读不通，还按原来的监控范围运行；改好后会自动重新读取：{type(exc).__name__}: {exc}")
            return
        new.secrets = self.settings.secrets
        others = (self.settings.model_dump(exclude={"universe", "secrets"}) != new.model_dump(exclude={"universe", "secrets"}))
        restart = "settings.yaml 里的改动（除了监控范围）要重启 tsen run 才生效"
        if self.settings.universe.model_dump() == new.universe.model_dump():  # 只改了 settings.yaml：不重载
            if others:
                log.warning(restart)
                await self._notify_error(restart)
            return
        effects = await self.apply_scope(new)
        if effects or others:
            body = "\n".join([*(f"- {x}" for x in effects), *([f"（{restart}）"] if others else [])])
            try:
                await self.notifier.send(Message(title="🔧 监控范围已更新", body=body, kind="info"))
            except Exception:
                log.exception("监控范围更新的提醒没发出去")

    async def monitor_tick(self) -> None:
        try:
            await self.maybe_reload_scope()
        except Exception:  # 热更新出错不能耽误检测
            log.exception("检查配置文件变化失败")
        try:
            await self.monitor.tick()
            self._tick_failures = 0
        except Exception as exc:
            self._tick_failures += 1
            log.exception("盘中检测失败（连续第 %d 次）", self._tick_failures)
            if self._tick_failures == self.TICK_FAILURE_ALERT:
                await self._notify_error(f"盘中检测已连续失败 {self._tick_failures} 次：{type(exc).__name__}: {exc}")
        finally:  # 一组出错时别的组照样推送了：新异动、刚完成的归因照样写进飞书多维表格（后台进行，内容没变不写）
            self.spawn_feishu_alerts()

    # ───────── 飞书日历 / 多维表格同步（notify.feishu_sync，默认关闭；失败只记日志，首次失败推送一条提醒） ─────────

    def _build_feishu_sync(self):
        cfg = self.settings.notify.feishu_sync
        if self.dry_run or not (cfg.calendar or cfg.bitable):  # 预览用的内存数据库没有映射，同步会重复建日程
            return None
        try:
            from tickersentinel.feishu import FeishuSync

            return FeishuSync.from_settings(self.settings, self.storage, on_error=self._notify_error)
        except Exception as exc:  # 缺密钥、没装 SDK：都是意料之中的配置问题，不打 traceback
            log.warning("飞书同步没有生效，本次运行不同步飞书日历 / 多维表格：%s: %s", type(exc).__name__, exc)
            return None

    async def sync_feishu_calendar(self, now: datetime | None = None) -> None:
        """事件日历刷新之后调用：把 events 表同步到飞书日历。"""
        if self.feishu is not None and self.settings.notify.feishu_sync.calendar:
            await self.feishu.run_calendar(now)

    def spawn_feishu_alerts(self) -> None:
        """盘中每次检测之后调用：在后台把最近 3 天的异动（新插入、归因 / 补发后更新）写进多维表格。"""
        if self.feishu is not None and self.settings.notify.feishu_sync.bitable:
            try:
                self.feishu.spawn_alerts(since=utcnow() - timedelta(days=3))
            except Exception:  # pragma: no cover - 不能让同步拖垮盘中检测
                log.exception("启动飞书多维表格同步失败")

    async def sync_feishu_alerts(self, now: datetime | None = None) -> None:
        """看门狗（每 15 分钟）：全部异动对一遍账，并每小时把你在飞书里填的 label / notes 拉回 alerts 表。"""
        if self.feishu is not None and self.settings.notify.feishu_sync.bitable:
            await self.feishu.run_alerts(now=now, pull_labels=True)

    async def _notify_error(self, text: str) -> None:
        try:
            await self.notifier.send(Message(title="⚠️ tickersentinel 出错", body=text, kind="error"))
        except Exception:
            log.exception("连错误提醒都发不出去")

    async def _on_message(self, msg: IncomingMessage) -> str | None:
        return await self.chat.handle(msg)

    # ───────── 常驻运行 ─────────

    async def run_forever(self) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        from tickersentinel.jobs.scheduler import Planner

        try:
            await self.notifier.start(self._on_message)
        except Exception:  # 收不到消息不影响推送：记日志，继续运行
            log.exception("推送渠道的消息接收（长连接）启动失败，只能推送、不能回复")
        sched = AsyncIOScheduler(timezone=self.settings.tz)
        planner = Planner(self, sched)
        planned = planner.install()
        sched.start()

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):  # Windows
                pass
        try:
            await self.notifier.send(Message(title="✅ tickersentinel 已启动", body=self._startup_text(planned), kind="info"))
        except Exception:  # 启动时网络抖动不能让整个进程退出
            log.exception("启动消息发送失败")
        log.info("已启动，按 Ctrl+C 退出")
        try:
            await stop.wait()
        finally:
            sched.shutdown(wait=False)
            await self.monitor.drain()

    def _startup_text(self, planned: list[str]) -> str:
        from datetime import timedelta

        from tickersentinel.timeutil import fmt_local, utcnow, weekday_cn

        now, tz = utcnow(), self.settings.tz
        lines = ["今日日程：", *(f"- {p}" for p in planned)] if planned else ["今天没有剩余任务。"]
        nxt = self.calendar.next_session(self.calendar.exchange_date(now))
        if nxt.open <= now:
            nxt = self.calendar.next_session(nxt.day + timedelta(days=1))
        lines.append(f"下一个交易日：{fmt_local(nxt.open, tz, '%m-%d')} {weekday_cn(nxt.open, tz)}，"
                     f"{fmt_local(nxt.open, tz, '%H:%M')} 开盘")
        channel = getattr(self.notifier, "name", self.settings.notify.provider)
        if channel != self.settings.notify.provider and not self.dry_run:  # 缺密钥时退回了终端打印（预览本来就只打印）
            channel += f"（配置的 {self.settings.notify.provider} 不可用，见日志）"
        lines.append(f"推送渠道：{channel}｜行情源：{self.prices.name}｜"
                     f"大模型：简报 {self.llm.describe('brief')}｜归因 {self.llm.describe('attribution')}｜"
                     f"对话 {self.llm.describe('chat')}")
        lines.append(self._feishu_sync_status())
        return "\n".join(lines)

    def _feishu_sync_status(self) -> str:
        """启动消息里的一行：飞书同步开了没有、是否真的在运行（打开了但缺密钥 / 没装 SDK 时说明没生效）。"""
        cfg = self.settings.notify.feishu_sync
        wanted = [n for n, flag in (("日历", cfg.calendar), ("多维表格", cfg.bitable)) if flag]
        if not wanted:
            return "飞书同步：关闭"
        if self.feishu is None:
            return f"飞书同步：已打开（{'、'.join(wanted)}），但没有生效（缺密钥或没装 lark-oapi，见日志）"
        return f"飞书同步：{'、'.join(wanted)}"

    async def aclose(self) -> None:
        for t in list(self._bg):  # 热更新后启动的后台事件刷新：先停掉，不然会碰到已关闭的数据库，推一条误报
            t.cancel()
        if self._bg:
            await asyncio.gather(*list(self._bg), return_exceptions=True)
        if self.feishu is not None:  # 不再开始新的同步，等正在跑的同步写完映射再关数据库（超时的取消）
            self.feishu.closing = True
            await self.feishu.drain()
        for closer in (self.notifier.stop, self.prices.aclose, self.events.aclose, self.news.aclose, self.llm.aclose):
            try:
                await closer()
            except Exception:  # pragma: no cover
                log.debug("关闭资源出错", exc_info=True)
        self.storage.close()
