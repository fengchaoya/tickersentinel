"""大模型供应商接口。新增供应商时实现 ChatProvider，并在 llm/client.py 的 build_provider 里注册。

两种调用：
- complete：一问一答。给定 system + prompt，返回文本（可选：按 JSON Schema 约束输出、联网检索）。简报、归因、事件汇总用它。
- complete_with_tools：带工具的多轮调用（对话用）。模型请求工具 → 我们执行 → 把结果发回 → 直到模型作答，
  最多 max_rounds 轮工具调用。各供应商自己实现协议格式（OpenAI 的 tool_calls / Claude 的 tool_use 块），
  工具的执行、出错处理、结果截断统一由本文件的 run_tool_calls 负责。不支持工具的供应商用默认实现：不带工具直接回答。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

MAX_TOOL_RESULT_CHARS = 6000  # 单个工具结果的上限：本地模型上下文有限，处理长提示词也慢
MAX_CALLS_PER_ROUND = 6  # 一轮里最多执行几个工具调用（防止模型一次请求几十个）
NO_TOOLS_NOTE = "（注意：这次没有可用的查询工具，无法查询实时数据；需要最新数据的问题请直接说明无法查询，不要编造数字。）"
LAST_ROUND_NOTE = "（工具调用次数已用完：请根据上面已有的工具结果直接回答，不要再调用工具。）"


class LLMError(RuntimeError):
    """调用失败（服务没开、超时、鉴权、返回格式不对等）。路由器据此换下一个供应商。"""


class LLMUnavailable(LLMError):
    """该任务没有可用的供应商（未配置、缺密钥，或全部调用失败）。调用方应走降级逻辑。"""


@dataclass(frozen=True)
class ToolSpec:
    """一个可供模型调用的工具。parameters 是 JSON Schema（type=object）。"""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ToolOutput:
    """工具执行结果：content 原样交给模型（紧凑的 JSON 或短文本）；is_error 表示调用失败（参数不对、数据源出错等）。"""

    content: str
    is_error: bool = False


@dataclass
class ToolCallRecord:
    """一次工具调用的记录（日志与诊断用，不发给模型）。"""

    name: str
    arguments: dict[str, Any]
    is_error: bool
    seconds: float
    chars: int
    round: int


# (工具名, 参数) → 结果。返回 str 视为成功；工具自己处理出错并返回 ToolOutput(is_error=True)，抛异常也会被兜住
ToolExecutor = Callable[[str, dict[str, Any]], Awaitable["ToolOutput | str"]]


@dataclass
class LLMResult:
    text: str
    citations: list[dict[str, str]] = field(default_factory=list)  # [{"url", "title"}]，只有联网检索时才有
    searches: int = 0
    model: str = ""
    provider: str = ""  # settings.yaml 里的供应商名，如 local / anthropic
    stop_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)  # complete_with_tools 执行过的工具
    rounds: int = 1  # 调用模型的次数


@dataclass
class PendingCall:
    """模型请求的一次工具调用（协议无关）。arguments 解析失败时为 None，raw 保留原文。"""

    id: str
    name: str
    arguments: dict[str, Any] | None
    raw: str = ""


def parse_arguments(raw: Any) -> dict[str, Any] | None:
    """OpenAI 协议里 arguments 是 JSON 字符串；Claude 直接给对象。空字符串按无参数处理。"""
    if isinstance(raw, dict):
        return raw
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def clip_tool_text(text: str, limit: int = MAX_TOOL_RESULT_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 20] + "\n…（结果过长，已截断）"


async def run_tool_calls(calls: list[PendingCall], execute: ToolExecutor, *, round_no: int,
                         records: list[ToolCallRecord], tool_names: set[str]) -> list[ToolOutput]:
    """并发执行一轮里的所有工具调用，返回与 calls 一一对应的结果。任何错误都转成 is_error 的结果交给模型，不抛异常。"""

    async def one(i: int, call: PendingCall) -> tuple[ToolOutput, ToolCallRecord]:
        t0 = time.monotonic()
        if i >= MAX_CALLS_PER_ROUND:
            out = ToolOutput(f"错误：一次最多调用 {MAX_CALLS_PER_ROUND} 个工具，这个调用没有执行。", True)
        elif call.name not in tool_names:
            out = ToolOutput(f"错误：没有名为 {call.name!r} 的工具。可用：{', '.join(sorted(tool_names))}", True)
        elif call.arguments is None:
            out = ToolOutput(f"错误：参数不是合法的 JSON 对象：{call.raw[:200]}", True)
        else:
            try:
                res = await execute(call.name, call.arguments)
                out = res if isinstance(res, ToolOutput) else ToolOutput(str(res))
            except Exception as exc:  # 工具本应自己处理错误；这里兜底，绝不让一次调用打断对话
                log.exception("工具 %s 执行出错", call.name)
                out = ToolOutput(f"错误：工具执行失败（{type(exc).__name__}: {exc}）", True)
        out = ToolOutput(clip_tool_text(out.content or "（无结果）"), out.is_error)
        seconds = time.monotonic() - t0
        log.info("工具 %s(%s)：%s，%.1f 秒，%d 字", call.name,
                 json.dumps(call.arguments, ensure_ascii=False) if call.arguments is not None else call.raw[:80],
                 "失败" if out.is_error else "完成", seconds, len(out.content))
        return out, ToolCallRecord(call.name, call.arguments or {}, out.is_error, round(seconds, 2),
                                   len(out.content), round_no)

    done = await asyncio.gather(*(one(i, c) for i, c in enumerate(calls)))
    records.extend(rec for _, rec in done)  # 按调用顺序记录
    return [out for out, _ in done]


def add_usage(total: dict[str, Any], usage: dict[str, Any] | None) -> None:
    """多轮调用的 token 用量累加（只加数值字段）。"""
    for k, v in (usage or {}).items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            total[k] = total.get(k, 0) + v


class ChatProvider(ABC):
    kind: str = "provider"
    supports_tools: bool = False  # 实现了原生工具调用的供应商设为 True

    def __init__(self, name: str, model: str):
        self.name = name
        self.model = model

    @property
    def configured(self) -> bool:
        """配置是否齐全（例如云端供应商有没有密钥）。不联网判断；本地服务开没开要调用了才知道。"""
        return True

    @abstractmethod
    async def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int | None = None,
        json_schema: dict | None = None,
        web_search: bool = False,
        max_searches: int = 4,
    ) -> LLMResult:
        """json_schema：要求按该 JSON Schema 输出（支持的供应商会强制约束，否则只靠提示词）。
        web_search：允许联网检索；不支持的供应商（本地模型、OpenAI 兼容接口）忽略此参数。
        max_tokens：一般不传，用供应商档案里的上限——Claude、OpenAI 推理模型的思考也计入这个上限，
        调用方按“答案长度”给的小数值会让思考把预算用光、返回空内容。"""

    async def complete_with_tools(
        self,
        *,
        system: str,
        prompt: str,
        tools: list[ToolSpec],
        execute: ToolExecutor,
        max_rounds: int = 4,
        max_tokens: int | None = None,
        system_no_tools: str | None = None,
    ) -> LLMResult:
        """带工具的多轮调用：最多 max_rounds 轮工具调用，之后不再提供工具、要求模型直接作答。
        system_no_tools：不带工具回答时改用的 system（调用方可以给一份“查不了实时数据”的版本）。
        默认实现（不支持工具的供应商）：不带工具直接回答——用 system_no_tools，没给就在 system 后面说明查不了实时数据。"""
        return await self.complete(system=system_no_tools or f"{system}\n{NO_TOOLS_NOTE}", prompt=prompt,
                                   max_tokens=max_tokens)

    async def health(self) -> str:
        """连通性检查（tsen check 用）：成功返回一句说明，失败抛 LLMError。"""
        res = await self.complete(system="你是连通性测试。", prompt="只回复：OK")
        return f"{self.model}：{res.text.strip()[:20]}"

    async def aclose(self) -> None:  # noqa: B027
        pass
