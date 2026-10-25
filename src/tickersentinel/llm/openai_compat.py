"""OpenAI 兼容的 Chat Completions 接口：本机 LM Studio（默认）、Ollama、llama.cpp、MLX，
以及 OpenAI、DeepSeek、OpenRouter 等云端服务。区别只在 base_url、model 和密钥。

POST {base_url}/chat/completions
  {"model", "messages": [{"role": "system"|"user", "content"}], "max_tokens",
   "response_format": {"type": "json_schema", "json_schema": {"name", "strict", "schema"}},   ← 按 Schema 约束输出
   "reasoning_effort": "none"}                                                             ← 关闭思考（见下）
→ {"choices": [{"message": {"content", "reasoning_content"?}, "finish_reason"}], "usage", "model"}

2026-10-20 在开发机（Apple Silicon M5 Pro / 64 GB，LM Studio，qwen/qwen3.8-27b 4bit MLX）实测：
- 默认开思考，思考内容放在 message.reasoning_content，content 是干净的答案；
  reasoning_effort="none" 可以关掉思考（reasoning_tokens=0），简单问答从约 6 秒降到 2 秒。
  思考开着时再要求 json_schema 会出问题：模型把 JSON 写进思考里、直到耗尽 max_tokens，content 为空。
- response_format 只接受 json_schema / text，不接受 json_object（HTTP 400）。
- 速度：生成约 17 token/秒；约 3.9k token 的简报提示词 + 540 token 输出共 41 秒。
有的服务（如 Ollama 的部分模型）会把思考写成 content 里的 <think>…</think>，这里统一去掉。

工具调用（对话用，complete_with_tools）：标准 OpenAI 格式
  请求加 "tools": [{"type": "function", "function": {"name", "description", "parameters"}}]
  → message.tool_calls = [{"id", "type": "function", "function": {"name", "arguments": "<JSON 字符串>"}}]，finish_reason=tool_calls
  → 把这条 assistant 消息原样追加，再为每个调用追加 {"role": "tool", "tool_call_id", "content"}，再请求一次。
2026-10-21 本机实测（qwen3.8-27b，reasoning_effort none）：第一轮约 2.6–3.6 秒返回 tool_calls，第二轮约 2 秒作答；
content 为 "" 或 null 都能发回；tool_choice="none" 有效；带着工具历史但不传 tools 也能正常作答。
工具轮数用完时最后一轮不再传 tools（不依赖各家对 tool_choice 的支持），并提示模型直接作答。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx

from tickersentinel.config import LLMProviderCfg
from tickersentinel.llm.base import (
    LAST_ROUND_NOTE,
    ChatProvider,
    LLMError,
    LLMResult,
    PendingCall,
    ToolCallRecord,
    ToolExecutor,
    ToolSpec,
    add_usage,
    parse_arguments,
    run_tool_calls,
)
from tickersentinel.llm.lmstudio import LMStudioManager

log = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 2000
_THINK = re.compile(r"<think>.*?</think>", re.S)


def strip_thinking(text: str) -> str:
    text = _THINK.sub("", text or "")
    if "</think>" in text:  # 只有结束标记（开头被模板吃掉）的情况
        text = text.split("</think>", 1)[1]
    if text.lstrip().startswith("<think>"):  # 思考没写完就被 max_tokens 截断：没有答案，按空内容处理
        return ""
    return text.strip()


def tool_schema(spec: ToolSpec) -> dict[str, Any]:
    return {"type": "function", "function": {"name": spec.name, "description": spec.description,
                                             "parameters": spec.parameters}}


class OpenAICompatProvider(ChatProvider):
    kind = "openai_compat"
    supports_tools = True

    def __init__(self, name: str, cfg: LLMProviderCfg, api_key: str | None, *,
                 client: httpx.AsyncClient | None = None, lmstudio: LMStudioManager | None = None):
        super().__init__(name, cfg.model)
        self.cfg = cfg
        self.base_url = (cfg.base_url or "").rstrip("/")
        self.api_key = api_key
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        # 连接超时设短：本地服务没开时尽快失败，路由器好换下一个供应商；读超时给足（本地生成慢）
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(cfg.timeout_seconds, connect=5.0))
        if lmstudio is None and cfg.lmstudio is not None:
            lmstudio = LMStudioManager(cfg.lmstudio, cfg.model, self.base_url)
        self.lmstudio = lmstudio

    @property
    def configured(self) -> bool:
        return not self.cfg.api_key_env or bool(self.api_key)

    def build_body(self, system: str, prompt: str, max_tokens: int | None, json_schema: dict | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            self.cfg.max_tokens_param: max_tokens or self.cfg.max_tokens or DEFAULT_MAX_TOKENS,
        }
        if self.cfg.temperature is not None:
            body["temperature"] = self.cfg.temperature
        if self.cfg.reasoning_effort:
            body["reasoning_effort"] = self.cfg.reasoning_effort
        if json_schema is not None:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "output", "strict": True, "schema": json_schema}}
        body.update(self.cfg.extra_body)
        return body

    async def _chat(self, body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """发一次 /chat/completions，返回（整个响应, choices[0]）。网络、HTTP、格式错误都转成 LLMError。"""
        try:
            resp = await self._client.post(f"{self.base_url}/chat/completions", json=body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise LLMError(f"[{self.name}] 连接 {self.base_url} 失败：{type(exc).__name__}: {exc}") from exc
        if resp.status_code != 200:
            raise LLMError(f"[{self.name}] HTTP {resp.status_code}：{resp.text[:300]}")
        try:
            data = resp.json()
            choice = data["choices"][0]
            if not isinstance(choice["message"], dict):
                raise TypeError("message 不是对象")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"[{self.name}] 返回格式不对：{resp.text[:300]}") from exc
        if choice.get("finish_reason") == "length":
            log.warning("[%s] 输出被 %s=%s 截断", self.name, self.cfg.max_tokens_param, body[self.cfg.max_tokens_param])
        return data, choice

    async def complete(self, *, system: str, prompt: str, max_tokens: int | None = None, json_schema: dict | None = None,
                       web_search: bool = False, max_searches: int = 4) -> LLMResult:
        if self.lmstudio is not None:
            await self.lmstudio.ensure(self._client)
        body = self.build_body(system, prompt, max_tokens, json_schema)
        data, choice = await self._chat(body)
        text = strip_thinking(choice["message"].get("content") or "")
        finish = choice.get("finish_reason")
        if not text:
            raise LLMError(f"[{self.name}] 返回了空内容（finish_reason={finish}）")
        return LLMResult(text=text, model=data.get("model") or self.model, provider=self.name,
                         stop_reason=finish, usage=data.get("usage") or {})

    async def complete_with_tools(self, *, system: str, prompt: str, tools: list[ToolSpec], execute: ToolExecutor,
                                  max_rounds: int = 4, max_tokens: int | None = None,
                                  system_no_tools: str | None = None) -> LLMResult:
        if not tools:
            return await self.complete(system=system_no_tools or system, prompt=prompt, max_tokens=max_tokens)
        if self.lmstudio is not None:
            await self.lmstudio.ensure(self._client)
        base = self.build_body(system, prompt, max_tokens, None)
        messages: list[dict[str, Any]] = list(base.pop("messages"))
        names = {t.name for t in tools}
        records: list[ToolCallRecord] = []
        usage: dict[str, Any] = {}
        data: dict[str, Any] = {}
        for rnd in range(max_rounds + 1):
            last = rnd == max_rounds
            body = {**base, "messages": messages}
            if last and rnd > 0:  # 工具轮数用完：不再提供工具，要求直接作答
                body["messages"] = [*messages, {"role": "user", "content": LAST_ROUND_NOTE}]
            elif not last:
                body["tools"] = [tool_schema(t) for t in tools]
            data, choice = await self._chat(body)
            add_usage(usage, data.get("usage"))
            msg = choice["message"]
            raw_calls = msg.get("tool_calls") or []
            if raw_calls and not last:
                calls = [self._pending(c, rnd, i) for i, c in enumerate(raw_calls)]
                # assistant 消息发回（只保留协议字段：content、tool_calls），id 补齐后与 tool 消息一一对应。
                # arguments 一律重新序列化成合法的 JSON 对象：模型偶尔写出坏 JSON（“{bad”、空串、数组），
                # 原样发回时 LM Studio 直接报 500 / 400，整次对话失败（2026-10-21 实测）；原文只放在报错的 tool 消息里
                messages.append({"role": "assistant", "content": msg.get("content") or "",
                                 "tool_calls": [{"id": c.id, "type": "function",
                                                 "function": {"name": c.name,
                                                              "arguments": json.dumps(c.arguments or {}, ensure_ascii=False)}}
                                                for c in calls]})
                outputs = await run_tool_calls(calls, execute, round_no=rnd + 1, records=records, tool_names=names)
                messages += [{"role": "tool", "tool_call_id": c.id, "content": o.content} for c, o in zip(calls, outputs)]
                continue
            text = strip_thinking(msg.get("content") or "")
            finish = choice.get("finish_reason")
            if not text:
                raise LLMError(f"[{self.name}] 返回了空内容（finish_reason={finish}，第 {rnd + 1} 轮）")
            return LLMResult(text=text, model=data.get("model") or self.model, provider=self.name, stop_reason=finish,
                             usage=usage, tool_calls=records, rounds=rnd + 1)
        raise LLMError(f"[{self.name}] 工具调用轮数用完仍未作答")  # pragma: no cover - 循环最后一轮必然返回或抛错

    @staticmethod
    def _pending(raw: Any, rnd: int, i: int) -> PendingCall:
        raw = raw if isinstance(raw, dict) else {}
        fn = raw.get("function") if isinstance(raw.get("function"), dict) else {}
        args = fn.get("arguments")
        text = args if isinstance(args, str) else json.dumps(args if args is not None else {}, ensure_ascii=False)
        return PendingCall(id=str(raw.get("id") or f"call_{rnd}_{i}"), name=str(fn.get("name") or ""),
                           arguments=parse_arguments(args), raw=text)

    async def health(self) -> str:
        if self.lmstudio is not None:
            await self.lmstudio.ensure(self._client)
        try:
            resp = await self._client.get(f"{self.base_url}/models", headers=self._headers)
        except httpx.HTTPError as exc:
            raise LLMError(f"连不上 {self.base_url}（{type(exc).__name__}）") from exc
        if resp.status_code == 200:
            ids = [m.get("id") for m in (resp.json().get("data") or []) if isinstance(m, dict)]
            if ids and self.model not in ids:
                raise LLMError(f"服务上没有模型 {self.model}；可用：{', '.join(map(str, ids[:8]))}")
        return await super().health()

    async def aclose(self) -> None:
        await self._client.aclose()


def provider_api_key(cfg: LLMProviderCfg, env: dict[str, str] | None = None) -> str | None:
    if not cfg.api_key_env:
        return None
    value = ((env if env is not None else os.environ).get(cfg.api_key_env) or "").strip()
    return value or None
