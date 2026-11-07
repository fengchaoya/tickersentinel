"""对话：在飞书（以后还有 Telegram）里给机器人发消息时，用大模型回答（默认本机模型，见 llm.tasks.chat）。

上下文：持仓 / 关注列表（含 note）、你“回复”的那条推送原文、最近几轮对话（本机数据库 chat_messages 表）。
第二版起模型可以调用查询工具（原生 tool calling，见 agents/chat_tools.py）：
    get_macro 宏观变量｜get_intraday 盘中走势与板块 / 个股拆分｜search_news 新闻与公告
    recent_alerts 历史异动与归因｜upcoming_events 事件日历
依赖缺失的工具不提供（例如没传宏观数据源就没有 get_macro）；路由到不支持工具的供应商时不带工具直接回答。
新闻用编号 [n3] 引用，链接由程序替换。发 /scope 调监控范围（见 scope/chat.py）。发 /new 开始新对话（清空这个会话的记忆）；发 /macro（或“宏观”）
直接回一份宏观快照，不经过大模型（几秒钟）。收到消息先回的“收到”由推送渠道负责。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tickersentinel.agents.chat_tools import ChatTools, market_status
from tickersentinel.llm.base import LLMResult, LLMUnavailable, ToolSpec
from tickersentinel.llm.prompts import reader_text, role_text
from tickersentinel.textutil import truncate
from tickersentinel.timeutil import fmt_local, tz_abbr, utcnow, weekday_cn

if TYPE_CHECKING:
    from tickersentinel.config import Settings
    from tickersentinel.events.hub import EventHub
    from tickersentinel.llm.client import LLMClient
    from tickersentinel.market_calendar import MarketCalendar
    from tickersentinel.news.hub import NewsHub
    from tickersentinel.sources.base import PriceSource
    from tickersentinel.scope.chat import ScopeChat
    from tickersentinel.sources.macro import MacroSource
    from tickersentinel.storage import Storage

log = logging.getLogger(__name__)

RESET_COMMANDS = {"/new", "/reset", "新对话"}
MACRO_COMMANDS = {"/macro", "/m", "宏观", "宏观数据"}
_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:https?://[^)\s]+)\)")
MAX_TOOL_ROUNDS = 4  # 一次提问最多几轮工具调用（每轮可以并发调用多个工具）；可用 chat.max_tool_rounds 覆盖


@dataclass
class IncomingMessage:
    text: str
    chat_id: str
    reply_to_text: str | None = None  # 如果是回复某条推送，这里是那条推送的内容


def chat_system(settings: "Settings", tools: list[ToolSpec] | None = None) -> str:
    if tools:
        names = "、".join(t.name for t in tools)
        data_rules = f"""- 你可以调用工具查询数据（{names}）。凡是涉及最新情况的问题——现在的价格、今天涨跌多少及原因、
  最近的新闻和公告、异动记录、财报与议息 / 经济数据等日程——都先调用工具再回答，不要凭训练知识或印象作答；需要时可以一次调用多个工具。
- 价格、涨跌幅、日期等数字只能来自工具结果或读者提供的内容；工具失败或没查到时直接说明（例如“行情查询失败”“没找到相关新闻”），
  不要编造。工具结果里的时间都已换成读者的本地时间。
- 判断“板块带动还是个股因素”时用 get_intraday 的拆分：板块部分 = β × 板块基准涨跌，个股特异 = 原始涨跌 − 板块部分。
- 汇总新闻时只挑最重要的 3–5 条，合并同一事件的多篇报道，忽略只复述行情的报道；引用时在句末标注工具结果里的编号，
  例如 [n2]，系统会把编号换成链接，不要自己写网址，也不要沿用之前对话里的编号。"""
    else:
        data_rules = """- 你**没有**实时行情、新闻和公告的查询工具：凡是需要最新数据的问题（现在的股价、今天的新闻、刚发布的财报），
  说明你无法查询最新数据，只依据已有知识和读者提供的内容回答，并标明“截至你的知识”或“需要核实”；绝不编造价格、日期和数字。"""
    return f"""你是一名{role_text(settings)}，在手机聊天软件里回答读者的提问。
{reader_text(settings)}

回答要求：
- 简体中文，直接回答问题，先给结论；不超过 {settings.chat.max_chars} 字（读者明确要求展开时才可以更长）。
{data_rules}
- 读者回复某条推送提问时，优先围绕那条推送回答。
- 区分事实与推断；涉及投资决策时给出分析框架和需要关注的风险，不直接给出买卖指令。
- 你只能回答问题和调用上面的查询工具：不能设置价格提醒、不能下单、不能替读者盯盘或“下次再告诉你”，不要承诺做不到的事。
  （盘中异动由系统自动监控推送，阈值在配置文件里。）
- 格式：轻量 Markdown，只用 **粗体** 和 “- ” 列表；不要表格，不要 # 标题。"""


class ChatAgent:
    def __init__(self, settings: "Settings", llm: "LLMClient", storage: "Storage", *,
                 prices: "PriceSource | None" = None, news: "NewsHub | None" = None,
                 events: "EventHub | None" = None, calendar: "MarketCalendar | None" = None,
                 macro: "MacroSource | None" = None, tools: ChatTools | None = None, scope: "ScopeChat | None" = None):
        """prices / news / events / calendar 都是可选的：缺哪个，依赖它的工具就不提供。scope：/scope 命令（调监控范围）。"""
        self.settings = settings
        self.scope = scope
        self.llm = llm
        self.storage = storage
        self.calendar = calendar
        self.macro = macro
        self.tools = tools or ChatTools(settings, storage, prices=prices, news=news, events=events, calendar=calendar,
                                        macro=macro)
        self.last_result: LLMResult | None = None  # 最近一次回答的诊断信息（用了哪个模型、调了哪些工具）

    @property
    def max_tool_rounds(self) -> int:
        return int(getattr(self.settings.chat, "max_tool_rounds", MAX_TOOL_ROUNDS))

    def build_prompt(self, msg: IncomingMessage) -> str:
        tz = self.settings.tz
        now = utcnow()
        parts = [f"【现在】{fmt_local(now, tz, '%Y-%m-%d %H:%M')} {tz_abbr(now, tz)} {weekday_cn(now, tz)}"]
        if self.calendar is not None:
            try:
                parts.append(f"【美股】{market_status(self.calendar, now, tz)}")
            except Exception:  # pragma: no cover - 交易日历超出范围等
                log.debug("计算美股交易状态失败", exc_info=True)
        history = self.storage.recent_chat(msg.chat_id, 2 * self.settings.chat.history_turns)
        if history:
            parts.append("【之前的对话】（从早到晚）")
            for role, past in history:  # 历史回答里的链接对模型没用、又占篇幅：只保留链接文字
                who = "读者" if role == "user" else "你"
                parts.append(f"{who}：{truncate(_MD_LINK.sub(lambda m: m.group(1), past), 800)}")
        if msg.reply_to_text:
            parts += ["【读者回复的那条推送】", truncate(msg.reply_to_text, 2000)]
        parts += ["【读者现在的问题】", msg.text.strip()]
        return "\n".join(parts)

    async def macro_reply(self) -> str:
        """/macro：直接回一份宏观快照（不经过大模型）。"""
        if self.macro is None:
            return "宏观数据没有开启（settings.yaml 的 macro.enabled）。"
        from tickersentinel.agents.render import render_macro

        try:
            m = render_macro(await self.macro.snapshot(), self.settings)
        except Exception as exc:
            log.warning("宏观快照失败：%s", exc)
            return f"宏观数据暂时取不到：{truncate(str(exc), 200)}"
        return f"**{m.title}**\n{m.body}"

    async def handle(self, msg: IncomingMessage) -> str | None:
        text = msg.text.strip()
        if not text:
            return None
        if text in RESET_COMMANDS:
            self.storage.clear_chat(msg.chat_id)
            return "好的，已开始新对话（之前的对话不再作为上下文）。"
        if text.lower() in MACRO_COMMANDS:
            return await self.macro_reply()
        if self.scope is not None and self.scope.wants(msg.chat_id, text):  # /scope、以及待确认改动的“确认 / 取消 / 去掉 X”
            return await self.scope.handle(msg.chat_id, text)
        if not self.settings.chat.enabled:
            return "对话功能已关闭（settings.yaml 的 chat.enabled）。"
        if not self.llm.available("chat"):
            return "没有可用的大模型（检查 settings.yaml 的 llm.tasks.chat）。"
        tools = self.tools.specs()
        session = self.tools.session()
        t0 = time.monotonic()
        try:
            res = await self.llm.complete_with_tools(
                "chat", system=chat_system(self.settings, tools), prompt=self.build_prompt(msg),
                tools=tools, execute=session.execute, max_rounds=self.max_tool_rounds,
                system_no_tools=chat_system(self.settings, None))  # 路由到不支持工具的供应商时用
        except LLMUnavailable as exc:
            log.warning("对话调用大模型失败：%s", exc)
            return f"大模型暂时不可用，稍后再试。\n（{truncate(str(exc), 200)}）"
        self.last_result = res
        log.info("对话回答完成：%s，调用模型 %d 次，工具 %s，用时 %.1f 秒", res.provider, res.rounds,
                 "、".join(f"{c.name}{'(失败)' if c.is_error else ''}" for c in res.tool_calls) or "无",
                 time.monotonic() - t0)
        answer = session.link_refs(res.text.strip())
        self.storage.add_chat_message(msg.chat_id, "user", text)
        self.storage.add_chat_message(msg.chat_id, "assistant", answer)
        return answer
