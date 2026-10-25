"""大模型路由：按 settings.yaml 的 llm.tasks 为每项任务依次尝试供应商，前一个失败就换下一个。

默认所有任务都走本机 LM Studio（llm.providers.local）。想切到云端，只要改 llm.tasks，例如：
    attribution: [anthropic]          # 归因改用 Claude（可联网检索）
    brief: [local, anthropic]         # 简报优先本地，本地服务没开时用 Claude
全部失败时抛 LLMUnavailable，调用方走降级逻辑（简报只列标题，归因只列新闻），不影响其他功能。
带工具的对话（complete_with_tools）同样按链路依次尝试：换供应商时从头再来（工具都是只读查询，重复执行无副作用）；
不支持工具的供应商不带工具直接回答。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping

from tickersentinel.config import LLMCfg, LLMProviderCfg
from tickersentinel.llm.base import (
    ChatProvider,
    LLMError,
    LLMResult,
    LLMUnavailable,
    ToolExecutor,
    ToolOutput,
    ToolSpec,
)
from tickersentinel.llm.openai_compat import OpenAICompatProvider, provider_api_key

log = logging.getLogger(__name__)

__all__ = ["LLMClient", "LLMError", "LLMResult", "LLMUnavailable", "ToolOutput", "ToolSpec", "build_provider",
           "extract_json"]


def build_provider(name: str, cfg: LLMProviderCfg, env: Mapping[str, str] | None = None) -> ChatProvider:
    key = provider_api_key(cfg, dict(env) if env is not None else None)
    if cfg.kind == "anthropic":
        from tickersentinel.llm.anthropic_api import AnthropicProvider

        if key is None and not cfg.api_key_env:  # 没指定变量名时按 SDK 惯例读 ANTHROPIC_API_KEY
            key = provider_api_key(cfg.model_copy(update={"api_key_env": "ANTHROPIC_API_KEY"}),
                                   dict(env) if env is not None else None)
        return AnthropicProvider(name, cfg, key)
    return OpenAICompatProvider(name, cfg, key)


class LLMClient:
    def __init__(self, cfg: LLMCfg, providers: Mapping[str, ChatProvider] | None = None, *,
                 env: Mapping[str, str] | None = None):
        self.cfg = cfg
        self.providers: dict[str, ChatProvider] = (
            dict(providers) if providers is not None
            else {name: build_provider(name, pcfg, env) for name, pcfg in cfg.providers.items()}
        )

    def route(self, task: str) -> list[ChatProvider]:
        names: list[str] = getattr(self.cfg.tasks, task, [])
        return [self.providers[n] for n in names if n in self.providers and self.providers[n].configured]

    def available(self, task: str) -> bool:
        return bool(self.route(task))

    def describe(self, task: str) -> str:
        chain = self.route(task)
        return " → ".join(f"{p.name}({p.model})" for p in chain) if chain else "未配置（降级模式）"

    async def complete(self, task: str, *, system: str, prompt: str, max_tokens: int | None = None,
                       json_schema: dict | None = None, web_search: bool = False, max_searches: int = 4) -> LLMResult:
        return await self._first_success(task, lambda p: p.complete(
            system=system, prompt=prompt, max_tokens=max_tokens, json_schema=json_schema, web_search=web_search,
            max_searches=max_searches))

    async def complete_with_tools(self, task: str, *, system: str, prompt: str, tools: list[ToolSpec],
                                  execute: ToolExecutor, max_rounds: int = 4, max_tokens: int | None = None,
                                  system_no_tools: str | None = None) -> LLMResult:
        """带工具的多轮调用（对话用）。tools 为空时等同 complete（用 system_no_tools，没给就用 system）。
        system_no_tools 也交给不支持工具的供应商，让它按“查不了实时数据”的口径回答。"""
        if not tools:
            return await self.complete(task, system=system_no_tools or system, prompt=prompt, max_tokens=max_tokens)
        return await self._first_success(task, lambda p: p.complete_with_tools(
            system=system, prompt=prompt, tools=tools, execute=execute, max_rounds=max_rounds, max_tokens=max_tokens,
            system_no_tools=system_no_tools))

    async def _first_success(self, task: str, call: Callable[[ChatProvider], Awaitable[LLMResult]]) -> LLMResult:
        chain = self.route(task)
        if not chain:
            raise LLMUnavailable(f"任务 {task} 没有可用的大模型（检查 llm.tasks.{task} 与密钥）")
        errors: list[str] = []
        for provider in chain:
            try:
                res = await call(provider)
            except LLMError as exc:
                log.warning("大模型 %s 调用失败（任务 %s）：%s", provider.name, task, exc)
                errors.append(str(exc))
                continue
            except Exception as exc:  # 意料之外的错误也换下一个供应商，但留下完整堆栈
                log.exception("大模型 %s 调用出错（任务 %s）", provider.name, task)
                errors.append(f"[{provider.name}] {type(exc).__name__}: {exc}")
                continue
            res.provider = provider.name
            return res
        raise LLMUnavailable("；".join(errors))

    async def aclose(self) -> None:
        for p in self.providers.values():
            try:
                await p.aclose()
            except Exception:  # pragma: no cover
                log.debug("关闭大模型客户端出错", exc_info=True)


def extract_json(text: str, required_key: str | None = None) -> dict | None:
    """从模型输出里取出最后一个合法的 JSON 对象（容忍前后有说明文字或 ```json 代码块）。"""
    if not text:
        return None
    decoder = json.JSONDecoder()
    found: dict | None = None
    i = text.find("{")
    while i != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
        except ValueError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict) and (required_key is None or required_key in obj):
            found = obj
        i = text.find("{", end)
    return found
