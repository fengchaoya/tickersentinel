"""大模型层测试：OpenAI 兼容接口（本地 LM Studio）、Claude、路由与降级、LM Studio 自动启动。全部离线。"""

import json
from types import SimpleNamespace as NS

import httpx
import pytest
from pydantic import ValidationError

from tickersentinel.config import LLMCfg, LLMProviderCfg, LLMTasks, LMStudioCfg
from tickersentinel.llm.anthropic_api import AnthropicProvider
from tickersentinel.llm.base import ChatProvider, LLMError, LLMResult, LLMUnavailable
from tickersentinel.llm.client import LLMClient, build_provider, extract_json
from tickersentinel.llm.lmstudio import LMStudioManager
from tickersentinel.llm.openai_compat import OpenAICompatProvider, strip_thinking

LOCAL = LLMProviderCfg(kind="openai_compat", base_url="http://127.0.0.1:1234/v1", model="qwen/qwen3.8-27b",
                       reasoning_effort="none", max_tokens=1500)
SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"],
          "additionalProperties": False}


def test_extract_json_variants():
    assert extract_json('{"causes": [], "summary": "x"}') == {"causes": [], "summary": "x"}
    fenced = '我先检索一下。\n```json\n{"summary": "s", "causes": [{"cause": "c"}]}\n```'
    assert extract_json(fenced, required_key="causes")["summary"] == "s"
    two = '{"a": 1} 然后 {"causes": [1], "summary": "last"}'
    assert extract_json(two, required_key="causes")["summary"] == "last"
    assert extract_json("没有 JSON") is None
    assert extract_json('{"a": 1}', required_key="causes") is None


def test_strip_thinking():
    assert strip_thinking("<think>先想想</think>\n\n答案") == "答案"
    assert strip_thinking("思考被模板吃掉了</think>答案") == "答案"
    assert strip_thinking("\n\n直接是答案") == "直接是答案"
    assert strip_thinking("<think>思考到一半被截断") == ""


def local_provider(handler, cfg=LOCAL, api_key=None):
    return OpenAICompatProvider("local", cfg, api_key, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_openai_compat_request_and_response():
    seen = []

    def handler(request: httpx.Request):
        seen.append((request.url.path, json.loads(request.content), request.headers.get("authorization")))
        return httpx.Response(200, json={
            "model": "qwen/qwen3.8-27b",
            "choices": [{"message": {"content": '\n\n{"summary": "板块拖累"}', "reasoning_content": "略"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 70, "completion_tokens": 9},
        })

    p = local_provider(handler)
    res = await p.complete(system="sys", prompt="p", json_schema=SCHEMA, web_search=True)
    assert res.text == '{"summary": "板块拖累"}' and res.provider == "local" and res.usage["completion_tokens"] == 9
    path, body, auth = seen[0]
    assert path == "/v1/chat/completions" and auth is None
    assert body["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "p"}]
    assert body["max_tokens"] == 1500 and body["reasoning_effort"] == "none" and "temperature" not in body
    assert body["response_format"] == {"type": "json_schema", "json_schema": {"name": "output", "strict": True, "schema": SCHEMA}}
    assert "tools" not in body  # 本地模型没有联网检索，web_search 被忽略

    cloud = LLMProviderCfg(kind="openai_compat", base_url="https://api.openai.com/v1", model="gpt-5-mini",
                           api_key_env="OPENAI_API_KEY", max_tokens_param="max_completion_tokens")
    p = local_provider(handler, cloud, api_key="sk-test")
    await p.complete(system="s", prompt="p", max_tokens=300)
    _, body, auth = seen[1]
    assert auth == "Bearer sk-test" and body["max_completion_tokens"] == 300 and "max_tokens" not in body


async def test_openai_compat_errors_become_llm_error():
    def down(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMError, match="连接"):
        await local_provider(down).complete(system="s", prompt="p")

    def bad(request):
        return httpx.Response(400, json={"error": "'response_format.type' must be 'json_schema' or 'text'"})

    with pytest.raises(LLMError, match="HTTP 400"):
        await local_provider(bad).complete(system="s", prompt="p")

    def empty(request):  # 思考开着又要求 JSON 时实测会这样：content 为空、finish_reason=length
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})

    with pytest.raises(LLMError, match="空内容"):
        await local_provider(empty).complete(system="s", prompt="p")

    cloud = LLMProviderCfg(kind="openai_compat", base_url="https://api.openai.com/v1", model="m", api_key_env="OPENAI_API_KEY")
    assert not build_provider("openai", cloud, env={}).configured
    assert build_provider("openai", cloud, env={"OPENAI_API_KEY": "sk"}).configured


class FakeAnthropic:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = NS(messages=self)

    async def create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


CLAUDE = LLMProviderCfg(kind="anthropic", model="claude-sonnet-5-5", api_key_env="ANTHROPIC_API_KEY", effort="low")


async def test_anthropic_pause_turn_citations_and_request_shape():
    cite = NS(url="https://reuters.com/a", title="Reuters A")
    r1 = NS(stop_reason="pause_turn", content=[NS(type="text", text="检索中。", citations=None),
                                               NS(type="server_tool_use", name="web_search")])
    r2 = NS(stop_reason="end_turn", model="claude-sonnet-5-5", usage=NS(input_tokens=10, output_tokens=5), content=[
        NS(type="thinking", thinking=""),
        NS(type="server_tool_use", name="web_search"),
        NS(type="text", text='{"summary": "s", "causes": []}', citations=[cite, cite]),
    ])
    fake = FakeAnthropic([r1, r2])
    p = AnthropicProvider("anthropic", CLAUDE, None, client=fake)
    res = await p.complete(system="sys", prompt="p", json_schema=SCHEMA, web_search=True, max_searches=3)
    assert res.searches == 2 and res.citations == [{"url": "https://reuters.com/a", "title": "Reuters A"}]
    assert extract_json(res.text, "causes") == {"summary": "s", "causes": []}
    first, second = fake.calls
    assert first["tools"][0] == {"type": "web_search_20260209", "name": "web_search", "max_uses": 3}
    assert first["fallbacks"] == "default" and first["betas"] == ["server-side-fallback-2026-07-01"]
    assert first["output_config"] == {"effort": "low"}  # 联网检索时不加 format，只靠提示词
    assert first["max_tokens"] == 16000 and "temperature" not in first
    assert second["messages"][-1]["role"] == "assistant"  # pause_turn 时把上一轮原样发回

    fake = FakeAnthropic([NS(stop_reason="end_turn", content=[NS(type="text", text='{"summary": "x"}', citations=None)])])
    await AnthropicProvider("anthropic", CLAUDE, None, client=fake).complete(system="s", prompt="p", json_schema=SCHEMA)
    assert fake.calls[0]["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}}
    assert "tools" not in fake.calls[0]


async def test_anthropic_refusal_and_missing_key():
    fake = FakeAnthropic([NS(stop_reason="refusal", stop_details=NS(category="cyber"), content=[])])
    with pytest.raises(LLMError, match="拒绝"):
        await AnthropicProvider("anthropic", CLAUDE, None, client=fake).complete(system="s", prompt="p")
    p = build_provider("anthropic", CLAUDE, env={})
    assert not p.configured
    with pytest.raises(LLMError):
        await p.complete(system="s", prompt="p")


class Scripted(ChatProvider):
    def __init__(self, name, outcome, configured=True):
        super().__init__(name, f"{name}-model")
        self.outcome = outcome
        self._configured = configured
        self.calls = 0

    @property
    def configured(self):
        return self._configured

    async def complete(self, **kw):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return LLMResult(text=self.outcome, model=self.model)


async def test_router_falls_back_in_order_and_degrades():
    cfg = LLMCfg().model_copy(update={"tasks": LLMTasks(brief=["local", "nokey", "cloud"], attribution=[], events=["local"])})
    local, nokey, cloud = Scripted("local", LLMError("连不上")), Scripted("nokey", "x", configured=False), Scripted("cloud", "云端答案")
    llm = LLMClient(cfg, providers={"local": local, "nokey": nokey, "cloud": cloud})
    res = await llm.complete("brief", system="s", prompt="p")
    assert res.text == "云端答案" and res.provider == "cloud" and local.calls == 1 and nokey.calls == 0
    assert llm.describe("brief") == "local(local-model) → cloud(cloud-model)"
    assert not llm.available("attribution") and llm.describe("attribution") == "未配置（降级模式）"
    with pytest.raises(LLMUnavailable):
        await llm.complete("attribution", system="s", prompt="p")
    with pytest.raises(LLMUnavailable, match="连不上"):
        await llm.complete("events", system="s", prompt="p")
    local.outcome = RuntimeError("bug")  # 意料之外的异常也不会让任务崩掉
    with pytest.raises(LLMUnavailable, match="RuntimeError"):
        await llm.complete("events", system="s", prompt="p")


def test_llm_config_validation():
    with pytest.raises(ValidationError, match="未定义的供应商"):
        LLMCfg(tasks=LLMTasks(brief=["nope"]))
    with pytest.raises(ValidationError, match="base_url"):
        LLMProviderCfg(kind="openai_compat", model="m")
    assert LLMCfg().tasks.brief == ["local"] and LLMCfg().providers["local"].reasoning_effort == "none"


async def test_lmstudio_starts_server_and_loads_model():
    state = {"up": False, "loaded": False}
    commands = []

    async def runner(args, timeout):
        commands.append(args[1:])
        if args[1:3] == ["server", "start"]:
            state["up"] = True
        if args[1] == "load":
            state["loaded"] = True
        return 0, "ok"

    def handler(request: httpx.Request):
        if not state["up"]:
            raise httpx.ConnectError("refused")
        row = {"id": "qwen/qwen3.8-27b", "state": "loaded" if state["loaded"] else "not-loaded"}
        if state["loaded"]:
            row["loaded_context_length"] = 32768
        return httpx.Response(200, json={"data": [row]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    mgr = LMStudioManager(LMStudioCfg(lms_path="/x/lms", context_length=32768, ttl_minutes=60), "qwen/qwen3.8-27b",
                          "http://127.0.0.1:1234/v1", runner=runner)
    await mgr.ensure(client)
    assert commands == [["server", "start"], ["load", "qwen/qwen3.8-27b", "-c", "32768", "--ttl", "3600", "-y"]]
    await mgr.ensure(client)  # 刚检查过：不重复查询、不重复加载
    mgr._ready_at = 0
    await mgr.ensure(client)  # 已加载：不再执行命令
    assert len(commands) == 2


async def test_lmstudio_missing_cli_and_backoff():
    # 找不到 lms：不抛异常，交给真实请求报错；失败后一段时间内并发调用不再逐个重试
    from tickersentinel.llm.lmstudio import run_command

    code, out = await run_command(["/nonexistent/lms", "server", "start"], 5)
    assert code == 127 and "FileNotFoundError" in out

    calls = []

    async def failing(args, timeout):
        calls.append(args[1])
        return 1, "boom"

    def down(request):
        raise httpx.ConnectError("refused")

    mgr = LMStudioManager(LMStudioCfg(), "m", "http://127.0.0.1:1234/v1", runner=failing)
    client = httpx.AsyncClient(transport=httpx.MockTransport(down))
    import asyncio

    await asyncio.gather(*(mgr.ensure(client) for _ in range(3)))
    assert calls == ["server"]


async def test_anthropic_extra_body_and_close():
    closed = []
    fake = FakeAnthropic([NS(stop_reason="end_turn", content=[NS(type="text", text="ok", citations=None)])])

    async def close():
        closed.append(True)

    fake.close = close
    cfg = CLAUDE.model_copy(update={"extra_body": {"new_param": 1}})
    p = AnthropicProvider("anthropic", cfg, None, client=fake)
    await p.complete(system="s", prompt="p")
    assert fake.calls[0]["extra_body"] == {"new_param": 1} and "new_param" not in fake.calls[0]
    await p.aclose()
    assert closed == [True]


def test_prompts_take_domain_from_config(settings):
    from tickersentinel.llm.prompts import brief_system

    assert "你是一名专注存储芯片（DRAM / NAND / HBM）的买方研究助理" in brief_system(settings)  # 来自主题 memory 的 focus
    settings.universe.themes["memory"].focus = ""
    settings.universe.themes["memory"].name = "某板块"
    text = brief_system(settings)
    assert "存储" not in text and "你是一名买方研究助理，关注读者持有和关注的股票" in text


# ───────── 工具调用（对话第二版） ─────────

from tickersentinel.llm.base import LAST_ROUND_NOTE, NO_TOOLS_NOTE, ToolOutput, ToolSpec  # noqa: E402

TOOLS = [ToolSpec("get_quotes", "查询最新价格", {"type": "object", "properties": {"symbols": {"type": "array", "items": {"type": "string"}}},
                                                  "required": []}),
         ToolSpec("search_news", "检索新闻", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})]


def recorder(results=None):
    seen = []

    async def execute(name, args):
        seen.append((name, args))
        r = (results or {}).get(name, f"{name} 的结果")
        if isinstance(r, Exception):
            raise r
        return r

    return seen, execute


async def test_openai_compat_tool_loop():
    bodies = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:  # 第一轮：三个调用——正常、参数不是 JSON、工具名不存在（且没有 id）
            return httpx.Response(200, json={"model": "qwen/qwen3.8-27b", "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                                             "choices": [{"finish_reason": "tool_calls", "message": {
                                                 "role": "assistant", "content": "", "reasoning_content": "",
                                                 "tool_calls": [
                                                     {"id": "111", "type": "function",
                                                      "function": {"name": "get_quotes", "arguments": '{"symbols":["MU"]}'}},
                                                     {"id": "222", "type": "function",
                                                      "function": {"name": "search_news", "arguments": "{bad"}},
                                                     {"type": "function", "function": {"name": "nope", "arguments": ""}},
                                                 ]}}]})
        return httpx.Response(200, json={"usage": {"prompt_tokens": 300, "completion_tokens": 40},
                                         "choices": [{"finish_reason": "stop",
                                                      "message": {"content": "<think>x</think>MU 现价 $100 [n1]"}}]})

    seen, execute = recorder({"get_quotes": ToolOutput('{"MU": 100}')})
    res = await local_provider(handler).complete_with_tools(system="sys", prompt="MU 多少钱", tools=TOOLS, execute=execute)
    assert res.text == "MU 现价 $100 [n1]" and res.rounds == 2 and res.stop_reason == "stop"
    assert res.usage == {"prompt_tokens": 400, "completion_tokens": 60}
    assert seen == [("get_quotes", {"symbols": ["MU"]})]  # 参数坏掉的、不存在的工具都不执行
    assert [(c.name, c.is_error, c.round) for c in res.tool_calls] == [("get_quotes", False, 1), ("search_news", True, 1),
                                                                         ("nope", True, 1)]
    first, second = bodies
    assert first["tools"][0] == {"type": "function", "function": {"name": "get_quotes", "description": "查询最新价格",
                                                                  "parameters": TOOLS[0].parameters}}
    assert first["reasoning_effort"] == "none" and len(first["messages"]) == 2 and "tool_choice" not in first
    msgs = second["messages"]
    assert msgs[2]["role"] == "assistant" and [c["id"] for c in msgs[2]["tool_calls"]] == ["111", "222", "call_0_2"]
    assert msgs[2]["tool_calls"][0]["function"] == {"name": "get_quotes", "arguments": '{"symbols": ["MU"]}'}
    # 坏 JSON / 空串发回时换成合法的对象：原样发回会让 LM Studio 报 500，整次对话失败
    assert [c["function"]["arguments"] for c in msgs[2]["tool_calls"][1:]] == ["{}", "{}"]
    assert "reasoning_content" not in msgs[2]
    assert msgs[3] == {"role": "tool", "tool_call_id": "111", "content": '{"MU": 100}'}
    assert msgs[4]["tool_call_id"] == "222" and msgs[4]["content"].startswith("错误：参数不是合法的 JSON")
    assert msgs[5]["tool_call_id"] == "call_0_2" and "没有名为 'nope' 的工具" in msgs[5]["content"]
    assert "tools" in second  # 还没到轮数上限，继续提供工具


async def test_openai_compat_tool_rounds_exhausted_and_empty_answer():
    bodies = []

    def greedy(request: httpx.Request):  # 只要提供工具就一直调用
        body = json.loads(request.content)
        bodies.append(body)
        if "tools" in body:
            return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {
                "content": None, "tool_calls": [{"id": f"c{len(bodies)}", "type": "function",
                                                 "function": {"name": "get_quotes", "arguments": "{}"}}]}}]})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "按已有结果作答"}}]})

    seen, execute = recorder()
    res = await local_provider(greedy).complete_with_tools(system="s", prompt="p", tools=TOOLS, execute=execute, max_rounds=2)
    assert res.text == "按已有结果作答" and res.rounds == 3 and len(seen) == 2
    assert "tools" not in bodies[-1] and bodies[-1]["messages"][-1] == {"role": "user", "content": LAST_ROUND_NOTE}
    assert bodies[1]["messages"][2]["content"] == ""  # content=null 的 assistant 消息按空字符串发回

    def empty(request):
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": ""}}]})

    with pytest.raises(LLMError, match="空内容"):
        await local_provider(empty).complete_with_tools(system="s", prompt="p", tools=TOOLS, execute=execute)

    def plain(request):  # 没有工具时等同 complete，并用 system_no_tools
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]})

    res = await local_provider(plain).complete_with_tools(system="s", prompt="p", tools=[], execute=execute,
                                                          system_no_tools="s0")
    assert res.text == "ok" and bodies[-1]["messages"][0] == {"role": "system", "content": "s0"} and "tools" not in bodies[-1]


async def test_anthropic_tool_loop_request_shape():
    r1 = NS(stop_reason="tool_use", model="claude-sonnet-5-5", usage=NS(input_tokens=50, output_tokens=10), content=[
        NS(type="thinking", thinking="", signature="sig"),
        NS(type="text", text="我先查一下。", citations=None),
        NS(type="tool_use", id="tu1", name="get_quotes", input={"symbols": ["MU"]}),
        NS(type="tool_use", id="tu2", name="search_news", input={"query": "MU"}),
    ])
    r2 = NS(stop_reason="end_turn", model="claude-sonnet-5-5", usage=NS(input_tokens=80, output_tokens=30),
            content=[NS(type="text", text="MU 现价 $100 [n1]", citations=None)])
    fake = FakeAnthropic([r1, r2])
    seen, execute = recorder({"get_quotes": "MU=100", "search_news": ToolOutput("数据源失败：HTTP 503", True)})
    res = await AnthropicProvider("anthropic", CLAUDE, None, client=fake).complete_with_tools(
        system="sys", prompt="MU 怎么样", tools=TOOLS, execute=execute)
    assert res.text == "MU 现价 $100 [n1]"  # 工具调用之间的说明不算答案
    assert res.rounds == 2 and res.usage == {"input_tokens": 130, "output_tokens": 40}
    assert seen == [("get_quotes", {"symbols": ["MU"]}), ("search_news", {"query": "MU"})]
    first, second = fake.calls
    assert first["tools"][1] == {"name": "search_news", "description": "检索新闻", "input_schema": TOOLS[1].parameters}
    assert "tool_choice" not in first  # 只用默认的 auto：Sonnet 5.5 不接受强制调用
    assert first["fallbacks"] == "default" and first["output_config"] == {"effort": "low"} and "temperature" not in first
    assert first["messages"] == [{"role": "user", "content": "MU 怎么样"}]
    assert second["messages"][1] == {"role": "assistant", "content": r1.content}  # 整条 content 原样发回（含 thinking）
    assert second["messages"][2] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu1", "content": "MU=100"},
        {"type": "tool_result", "tool_use_id": "tu2", "content": "数据源失败：HTTP 503", "is_error": True},
    ]}


async def test_anthropic_tool_loop_limits_refusal_and_truncation():
    use = lambda i: NS(type="tool_use", id=f"tu{i}", name="get_quotes", input={})  # noqa: E731
    fake = FakeAnthropic([NS(stop_reason="tool_use", content=[use(1)]),
                          NS(stop_reason="end_turn", content=[NS(type="text", text="最后作答", citations=None)])])
    seen, execute = recorder()
    res = await AnthropicProvider("anthropic", CLAUDE, None, client=fake).complete_with_tools(
        system="s", prompt="p", tools=TOOLS, execute=execute, max_rounds=1)
    assert res.text == "最后作答" and len(seen) == 1
    assert fake.calls[1]["tool_choice"] == {"type": "none"}  # 轮数用完：不再允许调用工具

    fake = FakeAnthropic([NS(stop_reason="tool_use", content=[use(1)]),
                          NS(stop_reason="refusal", stop_details=NS(category="cyber"), content=[])])
    with pytest.raises(LLMError, match="拒绝"):
        await AnthropicProvider("anthropic", CLAUDE, None, client=fake).complete_with_tools(
            system="s", prompt="p", tools=TOOLS, execute=execute)

    seen.clear()  # max_tokens 截断在工具调用中间：工具输入可能不完整，不执行；开场白不能当回答，抛错交给路由器
    fake = FakeAnthropic([NS(stop_reason="max_tokens", content=[NS(type="text", text="让我查一下", citations=None), use(2)])])
    with pytest.raises(LLMError, match="max_tokens"):
        await AnthropicProvider("anthropic", CLAUDE, None, client=fake).complete_with_tools(
            system="s", prompt="p", tools=TOOLS, execute=execute)
    assert seen == []
    fake = FakeAnthropic([NS(stop_reason="max_tokens", content=[NS(type="text", text="部分答案", citations=None)])])
    res = await AnthropicProvider("anthropic", CLAUDE, None, client=fake).complete_with_tools(
        system="s", prompt="p", tools=TOOLS, execute=execute)
    assert res.text == "部分答案"  # 纯文本被截断：照常返回（只记日志）

    pause = NS(stop_reason="pause_turn", content=[NS(type="text", text="前半", citations=None)])
    fake = FakeAnthropic([pause, NS(stop_reason="end_turn", content=[NS(type="text", text="后半", citations=None)])])
    res = await AnthropicProvider("anthropic", CLAUDE, None, client=fake).complete_with_tools(
        system="s", prompt="p", tools=TOOLS, execute=execute)
    assert res.text == "前半后半" and fake.calls[1]["messages"][-1] == {"role": "assistant", "content": pause.content}

    with pytest.raises(LLMError, match="未配置密钥"):
        await build_provider("anthropic", CLAUDE, env={}).complete_with_tools(system="s", prompt="p", tools=TOOLS,
                                                                              execute=execute)


async def test_tool_executor_exceptions_are_contained():
    def handler(request: httpx.Request):
        body = json.loads(request.content)
        if len(body["messages"]) == 2:
            return httpx.Response(200, json={"choices": [{"finish_reason": "tool_calls", "message": {"content": "", "tool_calls": [
                {"id": "a", "function": {"name": "get_quotes", "arguments": "{}"}}]}}]})
        assert body["messages"][-1]["content"].startswith("错误：工具执行失败（RuntimeError: boom")
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "查询失败了"}}]})

    _, execute = recorder({"get_quotes": RuntimeError("boom")})
    res = await local_provider(handler).complete_with_tools(system="s", prompt="p", tools=TOOLS, execute=execute)
    assert res.text == "查询失败了" and res.tool_calls[0].is_error


class ToolScripted(ChatProvider):
    """支持工具的假供应商：outcome 为异常时抛出，否则调用一次 get_quotes 后作答。"""

    supports_tools = True

    def __init__(self, name, outcome):
        super().__init__(name, f"{name}-model")
        self.outcome = outcome
        self.systems = []

    async def complete(self, **kw):  # 没有工具时路由器走 complete：这里模拟失败，交给下一个供应商
        raise LLMError(f"[{self.name}] 只测工具路径")

    async def complete_with_tools(self, *, system, prompt, tools, execute, max_rounds=4, max_tokens=None,
                                  system_no_tools=None):
        self.systems.append(system)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        await execute("get_quotes", {})
        return LLMResult(text=self.outcome, model=self.model)


class Plain(Scripted):
    def __init__(self, name, outcome):
        super().__init__(name, outcome)
        self.systems = []

    async def complete(self, **kw):
        self.systems.append(kw["system"])
        return await super().complete(**kw)


async def test_router_tool_fallback_and_providers_without_tools():
    cfg = LLMCfg().model_copy(update={"tasks": LLMTasks(brief=[], attribution=[], events=[], chat=["local", "cloud", "plain"])})
    local, cloud, plain = ToolScripted("local", LLMError("连不上")), ToolScripted("cloud", "云端答案"), Plain("plain", "无工具答案")
    llm = LLMClient(cfg, providers={"local": local, "cloud": cloud, "plain": plain})
    seen, execute = recorder()
    res = await llm.complete_with_tools("chat", system="S", prompt="p", tools=TOOLS, execute=execute, system_no_tools="S0")
    assert res.text == "云端答案" and res.provider == "cloud" and local.systems == ["S"] and seen == [("get_quotes", {})]

    cloud.outcome = LLMError("限流")  # 都失败时落到不支持工具的供应商：不带工具、用 system_no_tools
    res = await llm.complete_with_tools("chat", system="S", prompt="p", tools=TOOLS, execute=execute, system_no_tools="S0")
    assert res.text == "无工具答案" and res.provider == "plain" and plain.systems == ["S0"]
    await llm.complete_with_tools("chat", system="S", prompt="p", tools=TOOLS, execute=execute)
    assert plain.systems[-1] == f"S\n{NO_TOOLS_NOTE}"

    plain.outcome = LLMError("也挂了")
    with pytest.raises(LLMUnavailable, match="连不上.*限流.*也挂了"):
        await llm.complete_with_tools("chat", system="S", prompt="p", tools=TOOLS, execute=execute)

    plain.outcome = "直接回答"  # 没有工具时等同 complete
    res = await llm.complete_with_tools("chat", system="S", prompt="p", tools=[], execute=execute, system_no_tools="S0")
    assert res.text == "直接回答" and len(local.systems) == 4 and plain.systems[-1] == "S0"  # 没有走工具路径
