"""对话：提示词里有持仓背景、回复的推送、最近几轮对话；/new 清空；大模型不可用时给出说明；
第二版：带查询工具（行情 / 新闻 / 异动 / 日历），新闻编号换成链接，不支持工具的供应商按“查不了实时数据”的口径回答。不联网。"""

from datetime import timedelta

from tickersentinel.agents.chat import ChatAgent, IncomingMessage, chat_system
from tickersentinel.config import LLMTasks
from tickersentinel.llm.base import ChatProvider, LLMError, LLMResult, ToolCallRecord
from tickersentinel.llm.client import LLMClient
from tickersentinel.models import NewsItem
from tickersentinel.news.hub import NewsHub
from tickersentinel.storage import Storage
from tickersentinel.timeutil import utcnow

from conftest import FakeNewsSource


class Fake(ChatProvider):
    """不支持工具的供应商（第一版的行为）。"""

    def __init__(self, replies):
        super().__init__("fake", "fake-model")
        self.replies = list(replies)
        self.calls = []

    async def complete(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return LLMResult(text=r, model=self.model)


class ToolFake(ChatProvider):
    """支持工具：按脚本调用工具，再给出回答。"""

    supports_tools = True

    def __init__(self, script, answer):
        super().__init__("toolfake", "tool-model")
        self.script = script
        self.answer = answer
        self.calls = []
        self.outputs = []

    async def complete(self, **kw):  # pragma: no cover
        raise LLMError("只走工具路径")

    async def complete_with_tools(self, **kw):
        self.calls.append(kw)
        records = []
        for name, args in self.script:
            out = await kw["execute"](name, args)
            self.outputs.append(out)
            records.append(ToolCallRecord(name, args, out.is_error, 0.0, len(out.content), 1))
        return LLMResult(text=self.answer, model=self.model, tool_calls=records, rounds=2)


def llm_with(settings, provider):
    cfg = settings.llm.model_copy(update={"tasks": LLMTasks(brief=[], attribution=[], events=[], chat=[provider.name])})
    return LLMClient(cfg, providers={provider.name: provider})


def agent(settings, replies):
    fake = Fake(replies)
    return ChatAgent(settings, llm_with(settings, fake), Storage(":memory:")), fake


async def test_chat_context_history_and_reset(settings):
    settings.chat.history_turns = 1
    bot, fake = agent(settings, ["第一问的回答", "第二问的回答", "第三问的回答"])
    assert await bot.handle(IncomingMessage("MU 的 HBM 份额大概多少？", "oc_me")) == "第一问的回答"
    call = fake.calls[0]
    # 供应商不支持工具：改用“没有实时数据工具”的提示词
    assert "美光" in call["system"] and "没有**实时行情" in call["system"] and call["prompt"].endswith("MU 的 HBM 份额大概多少？")

    await bot.handle(IncomingMessage("为什么跌", "oc_me", reply_to_text="MU 15分钟内急跌 -3.2%"))
    p2 = fake.calls[1]["prompt"]
    assert "读者：MU 的 HBM 份额大概多少？" in p2 and "你：第一问的回答" in p2
    assert "【读者回复的那条推送】\nMU 15分钟内急跌 -3.2%" in p2

    await bot.handle(IncomingMessage("再说说 SNDK", "oc_me"))
    p3 = fake.calls[2]["prompt"]
    assert "第一问" not in p3 and "读者：为什么跌" in p3  # 只带最近 1 轮

    assert "新对话" in await bot.handle(IncomingMessage("/new", "oc_me"))
    assert bot.storage.recent_chat("oc_me", 10) == []
    assert bot.storage.recent_chat("other", 10) == []


async def test_chat_when_model_unavailable(settings):
    bot, _ = agent(settings, [LLMError("连不上 LM Studio")])
    answer = await bot.handle(IncomingMessage("你好", "oc_me"))
    assert "暂时不可用" in answer and "连不上" in answer
    assert bot.storage.recent_chat("oc_me", 10) == []  # 失败的问答不进记忆
    assert await bot.handle(IncomingMessage("   ", "oc_me")) is None


async def test_chat_with_tools_links_and_history(settings, calendar):
    now = utcnow()
    item = NewsItem(title="Micron raises guidance", url="https://reuters.com/mu1", published_at=now - timedelta(hours=1),
                    source="Reuters", provider="fake", symbols=("MU",))
    hub = NewsHub([FakeNewsSource([item])], settings.universe)
    provider = ToolFake([("search_news", {"query": "MU"}), ("upcoming_events", {})], "指引上调 [n1]，未来两周没有财报。")
    storage = Storage(":memory:")
    bot = ChatAgent(settings, llm_with(settings, provider), storage, news=hub, calendar=calendar)
    answer = await bot.handle(IncomingMessage("MU 最近有什么新闻？", "oc_me"))
    assert answer == "指引上调 [Reuters](https://reuters.com/mu1)，未来两周没有财报。"
    call = provider.calls[0]
    assert [t.name for t in call["tools"]] == ["search_news", "recent_alerts", "upcoming_events"]  # 没有行情源就没有行情工具
    assert "search_news、recent_alerts、upcoming_events" in call["system"] and "[n2]" in call["system"]
    assert "没有**实时行情" in call["system_no_tools"] and call["max_rounds"] == 4
    assert "【美股】" in call["prompt"]
    assert "[n1]" in provider.outputs[0].content and not provider.outputs[1].is_error
    assert bot.last_result is not None and [c.name for c in bot.last_result.tool_calls] == ["search_news", "upcoming_events"]

    # 存进记忆的是带链接的原文；下一轮提示词里只保留链接文字
    assert storage.recent_chat("oc_me", 10)[-1] == ("assistant", answer)
    provider.script = []
    await bot.handle(IncomingMessage("展开说说", "oc_me"))
    p2 = provider.calls[1]["prompt"]
    assert "你：指引上调 Reuters，未来两周没有财报。" in p2 and "https://" not in p2


def test_chat_system_variants(settings):
    assert "没有**实时行情" in chat_system(settings) and "调用工具" not in chat_system(settings)
    from tickersentinel.llm.base import ToolSpec

    text = chat_system(settings, [ToolSpec("get_quotes", "d", {"type": "object", "properties": {}})])
    assert "get_quotes" in text and "不要编造" in text and "没有**实时行情" not in text
    assert f"不超过 {settings.chat.max_chars} 字" in text
