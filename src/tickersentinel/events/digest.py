"""事件汇总推送与排版。

每晚（events.digest.time，默认 20:00）汇总一次：
  ① 明天的事件（按你的本地时区算“明天”：SK海力士 10-28 09:00 KST 发布业绩 = 10-27 19:00 CT）；
  ② 上次汇总以来新增 / 改期 / 由估计变为确认的事件（包括刚提交的 Form 144 拟减持）；
  ③ 未来 N 天的完整日历，分“宏观”（议息、经济数据、国债拍卖、休市……）与“公司”两部分。
①②都没有时不推送（日历本身没有变化就不打扰）。第一次运行时推送一份完整日历作为起点。
有可用的大模型时，在开头加两三句“看点”；模型不可用就只发列表。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from tickersentinel.agents.render import link
from tickersentinel.config import Settings
from tickersentinel.events.hub import EventHub
from tickersentinel.llm.client import LLMClient
from tickersentinel.llm.prompts import role_text
from tickersentinel.models import Event, Message
from tickersentinel.storage import Storage
from tickersentinel.timeutil import fmt_local, tz_abbr, utcnow

log = logging.getLogger(__name__)

LAST_DIGEST_KEY = "events:last_digest"
_WEEKDAYS = "一二三四五六日"
CHANGE_NAMES = {"new": "新增", "rescheduled": "改期", "retimed": "时间变更", "confirmed": "已确认"}


def local_day(e: Event, tz: ZoneInfo) -> date:
    return e.at.astimezone(tz).date() if e.at else e.day


def format_when(e: Event, tz: ZoneInfo) -> str:
    if e.at:
        return f"{fmt_local(e.at, tz, '%m-%d')} 周{_WEEKDAYS[e.at.astimezone(tz).weekday()]} " \
               f"{fmt_local(e.at, tz, '%H:%M')} {tz_abbr(e.at, tz)}"
    return f"{e.day:%m-%d} 周{_WEEKDAYS[e.day.weekday()]}" + (f" {e.time_hint}" if e.time_hint else "")


def event_line(e: Event, tz: ZoneInfo) -> str:
    title = link(e.title, e.url) if e.url else e.title
    return f"- {format_when(e, tz)}｜{title}" + ("" if e.confirmed else "（预计）")


def change_line(e: Event, change: str, prev_day: date | None, tz: ZoneInfo) -> str:
    tag = CHANGE_NAMES.get(change, change)
    if change == "rescheduled" and prev_day:
        tag = f"改期（原 {prev_day:%m-%d}）"
    return f"- {tag}：{event_line(e, tz)[2:]}"


def events_prompt_lines(events: list[Event], tz: ZoneInfo) -> list[str]:
    """给大模型（简报 / 汇总）用的纯文本日历行。"""
    return [f"- {format_when(e, tz)} {e.title}" + ("" if e.confirmed else "（预计日期）") for e in events]


CHANGE_HORIZON_DAYS = 45  # 每晚汇总只报这么多天以内的估计日期变化（已确认的不限）


class EventDigest:
    def __init__(self, settings: Settings, hub: EventHub, storage: Storage, llm: LLMClient):
        self.settings = settings
        self.hub = hub
        self.storage = storage
        self.llm = llm
        self._built_at: datetime | None = None

    def window(self, today: date, days: int) -> list[Event]:
        tz = self.settings.tz
        evs = self.hub.upcoming(today - timedelta(days=1), today + timedelta(days=days + 1))
        return [e for e in evs if today <= local_day(e, tz) <= today + timedelta(days=days)]

    async def build(self, now: datetime | None = None) -> Message | None:
        now = now or utcnow()
        self._built_at = now  # 记为“已汇总到此刻”：生成期间（大模型要几十秒）新写入的变化留给下一次
        tz = self.settings.tz
        today = now.astimezone(tz).date()
        days = self.settings.events.digest.days_ahead
        upcoming = self.window(today, days)
        tomorrow = [e for e in upcoming if local_day(e, tz) == today + timedelta(days=1)]
        last_raw = self.storage.kv_get(LAST_DIGEST_KEY)
        first_run = last_raw is None
        if first_run:
            # 第一次汇总：变化无从比较，但最近一周提交的拟减持通知（日期已过、不在“未来”里）仍要列出来
            since = datetime(1970, 1, 1, tzinfo=now.tzinfo)
            changes = [(e, c, prev) for e, c, prev in self.hub.changed_since(since)
                       if e.kind == "insider_sale" and local_day(e, tz) >= today - timedelta(days=7)]
        else:
            # 远期（超过 CHANGE_HORIZON_DAYS 天）的估计日期只入库、不报变化：日历收集到 100 天以外是为了让对话
            # 能回答“下次财报是哪天”，但 Yahoo 对远期估计改得勤，逐条报“新增 / 改期”只会刷屏
            horizon = today + timedelta(days=CHANGE_HORIZON_DAYS)
            # 宏观日程（议息、数据发布、国债拍卖……）提前很久就排好了，“新增 / 确认”没有信息量，到期前会出现在日历里；
            # 只报改期和时间变更（例如政府停摆推迟数据发布）
            changes = [(e, c, prev) for e, c, prev in self.hub.changed_since(datetime.fromisoformat(last_raw))
                       if local_day(e, tz) >= today - timedelta(days=7) and (e.confirmed or local_day(e, tz) <= horizon)
                       and not (e.kind == "macro" and c in ("new", "confirmed"))]
        if not first_run and not tomorrow and not changes:
            return None

        parts: list[str] = []
        overview = await self._overview(upcoming, changes, today)
        if overview:
            parts += [overview, ""]
        if tomorrow:
            parts += ["**明天**", *[event_line(e, tz) for e in tomorrow], ""]
        if changes:
            parts += ["**新增 / 变更**", *[change_line(e, c, p, tz) for e, c, p in changes], ""]
        macro = [e for e in upcoming if e.kind == "macro"]
        company = [e for e in upcoming if e.kind != "macro"]
        if macro:
            parts += [f"**未来 {days} 天 · 宏观**", *[event_line(e, tz) for e in macro], ""]
        parts.append(f"**未来 {days} 天 · 公司**" if macro else f"**未来 {days} 天**")
        parts += [event_line(e, tz) for e in company] or ["- 暂无已知事件"]
        if first_run:
            parts += ["", "（首次汇总：以后只在有明天的事件或新增 / 改期时推送）"]
        title = f"📅 事件日历 · {today:%m-%d} 周{_WEEKDAYS[today.weekday()]}"
        return Message(title=title, body="\n".join(parts).strip(), kind="events")

    def mark_sent(self, now: datetime | None = None) -> None:
        self.storage.kv_set(LAST_DIGEST_KEY, (now or self._built_at or utcnow()).isoformat())

    def sent_today(self, now: datetime | None = None) -> bool:
        raw = self.storage.kv_get(LAST_DIGEST_KEY)
        if raw is None:
            return False
        tz = self.settings.tz
        return datetime.fromisoformat(raw).astimezone(tz).date() == (now or utcnow()).astimezone(tz).date()

    async def _overview(self, upcoming: list[Event], changes: list, today: date) -> str:
        if (not upcoming and not changes) or not self.llm.available("events"):
            return ""
        tz = self.settings.tz
        lines = [f"今天是 {today:%Y-%m-%d}（{self.settings.timezone}）。", "【未来日历】", *events_prompt_lines(upcoming, tz)]
        if changes:
            lines += ["【新增 / 变更】", *[f"- {CHANGE_NAMES.get(c, c)}：{e.title}，{format_when(e, tz)}" for e, c, _ in changes]]
        system = (f"你是{role_text(self.settings)}。读者持有 / 关注：" + "、".join(self.settings.universe.symbols)
                  + "。请用 2–3 句中文（不超过 120 字）点出接下来最值得关注的事件以及它对持仓的意义"
                  "（例如大客户与同业财报对行业需求和价格的指引、议息会议与通胀数据对利率和估值的影响、"
                  "内部人减持的规模）。"
                  "只依据给定列表，不编造日期和数字，不给买卖建议，不要标题和列表，直接输出正文。")
        try:
            res = await self.llm.complete("events", system=system, prompt="\n".join(lines))
        except Exception as exc:
            log.info("事件汇总的大模型看点生成失败，只发列表：%s", exc)
            return ""
        return res.text.strip()
