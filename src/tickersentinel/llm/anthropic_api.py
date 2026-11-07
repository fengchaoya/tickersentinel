"""Claude API（官方 Python SDK，anthropic 1.x）。可选的服务器端 web search，处理 pause_turn 续跑。

要点（据 2026-10 的 Claude API 文档，Sonnet 5.5）：
- 服务器端检索工具版本是 web_search_20260209（不是 web_search_20260318），按次计费（$10 / 1000 次，另计 token）。
- Sonnet 5.5 默认开自适应思考，不能用 {"type": "disabled"} 关掉；用 output_config.effort（low / medium / high）
  控制思考深度和花费。不接受非默认的 temperature 等采样参数，所以这里从不发送。
- 思考会占用输出 token，max_tokens 默认给 16000，避免答案被截断。
- 不需要联网时用 output_config.format 按 JSON Schema 约束输出；联网检索时只靠提示词 + extract_json。
- 安全分类器可能拒绝（HTTP 200、stop_reason="refusal"）；默认开启服务器端 fallbacks="default"
  （beta server-side-fallback-2026-07-01），被拒时由 API 自动换模型重试；仍被拒则抛 LLMError，交给路由器降级。
- 长时间的服务器端工具调用会以 stop_reason="pause_turn" 暂停：把这一轮 assistant 内容原样发回即可续跑。

自定义工具（对话用，complete_with_tools，手写循环）：
- tools = [{"name", "description", "input_schema"}]；tool_choice 只用默认的 auto——Sonnet 5.5 对强制调用
  （{"type": "any"} / {"type": "tool"}）直接返回 400。工具轮数用完的最后一轮发 tool_choice={"type": "none"}。
- stop_reason="tool_use" 时：把整条 assistant content（含 thinking 块、工具调用之间的说明）原样追加，
  再用一条 user 消息发回本轮全部 tool_result（tool_use_id 对应；失败的带 is_error=true），继续请求。
  thinking 块必须原样发回、不能删改（Sonnet 5.5 的思考块与模型、会话绑定）。
- 最终答案只取最后一轮的 text 块；工具调用之间的说明不算答案。
- max_tokens 截断在工具调用中间时不执行工具（输入可能不完整），抛 LLMError 交给路由器；refusal 照旧抛 LLMError，由路由器换下一个供应商。
"""

from __future__ import annotations

import logging
from typing import Any

from tickersentinel.config import LLMProviderCfg
from tickersentinel.llm.base import (
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

log = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 16000
DEFAULT_WEB_SEARCH_TOOL = "web_search_20260209"
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _usage(resp: Any) -> dict[str, Any]:
    usage = getattr(resp, "usage", None)
    return {k: getattr(usage, k) for k in ("input_tokens", "output_tokens") if usage is not None and hasattr(usage, k)}


class AnthropicProvider(ChatProvider):
    kind = "anthropic"
    supports_tools = True
    MAX_CONTINUATIONS = 4

    def __init__(self, name: str, cfg: LLMProviderCfg, api_key: str | None, *, client: Any | None = None):
        super().__init__(name, cfg.model)
        self.cfg = cfg
        if client is not None:
            self._client = client
        elif api_key:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=api_key, base_url=cfg.base_url, max_retries=2,
                                          timeout=cfg.timeout_seconds)
        else:
            self._client = None

    @property
    def configured(self) -> bool:
        return self._client is not None

    def build_kwargs(self, system: str, prompt: str, max_tokens: int | None, json_schema: dict | None,
                     web_search: bool, max_searches: int) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or self.cfg.max_tokens or DEFAULT_MAX_TOKENS,
            "system": system,
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
        }
        output_config: dict[str, Any] = {}
        if self.cfg.effort:
            output_config["effort"] = self.cfg.effort
        if json_schema is not None and not web_search:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
        if output_config:
            kwargs["output_config"] = output_config
        if web_search:
            kwargs["tools"] = [{"type": self.cfg.web_search_tool or DEFAULT_WEB_SEARCH_TOOL, "name": "web_search",
                                "max_uses": max_searches}]
        if self.cfg.extra_body:  # SDK 不认识的新参数也能原样并入请求体
            kwargs["extra_body"] = dict(self.cfg.extra_body)
        return kwargs

    async def _create(self, messages: list[dict[str, Any]], kwargs: dict[str, Any]) -> Any:
        try:
            resp = await self._client.beta.messages.create(messages=messages, **kwargs)
        except Exception as exc:  # anthropic.APIError 及网络错误：交给路由器换下一个供应商
            raise LLMError(f"[{self.name}] 调用失败：{type(exc).__name__}: {exc}") from exc
        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            raise LLMError(f"[{self.name}] 模型拒绝回答（{getattr(details, 'category', None) or '未说明类别'}）")
        return resp

    async def complete(self, *, system: str, prompt: str, max_tokens: int | None = None, json_schema: dict | None = None,
                       web_search: bool = False, max_searches: int = 4) -> LLMResult:
        if self._client is None:
            raise LLMError(f"[{self.name}] 未配置密钥（{self.cfg.api_key_env or 'ANTHROPIC_API_KEY'}）")
        kwargs = self.build_kwargs(system, prompt, max_tokens, json_schema, web_search, max_searches)
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        texts: list[str] = []
        citations: list[dict[str, str]] = []
        searches = 0
        resp = None
        for _ in range(self.MAX_CONTINUATIONS + 1):
            resp = await self._create(messages, kwargs)
            for block in resp.content:
                btype = getattr(block, "type", "")
                if btype == "text":
                    texts.append(block.text)
                    for c in getattr(block, "citations", None) or []:
                        url = getattr(c, "url", None)
                        if url and all(x["url"] != url for x in citations):
                            citations.append({"url": url, "title": getattr(c, "title", "") or ""})
                elif btype == "server_tool_use":
                    searches += 1
            if resp.stop_reason != "pause_turn":
                break
            messages = [*messages, {"role": "assistant", "content": resp.content}]
        if resp is not None and resp.stop_reason == "max_tokens":
            log.warning("[%s] 输出被 max_tokens=%s 截断", self.name, kwargs["max_tokens"])
        text = "".join(texts).strip()
        if not text:
            raise LLMError(f"[{self.name}] 返回了空内容（stop_reason={getattr(resp, 'stop_reason', None)}）")
        return LLMResult(
            text=text,
            citations=citations,
            searches=searches,
            model=getattr(resp, "model", None) or self.model,
            provider=self.name,
            stop_reason=getattr(resp, "stop_reason", None),
            usage=_usage(resp),
        )

    async def complete_with_tools(self, *, system: str, prompt: str, tools: list[ToolSpec], execute: ToolExecutor,
                                  max_rounds: int = 4, max_tokens: int | None = None,
                                  system_no_tools: str | None = None) -> LLMResult:
        if not tools:
            return await self.complete(system=system_no_tools or system, prompt=prompt, max_tokens=max_tokens)
        if self._client is None:
            raise LLMError(f"[{self.name}] 未配置密钥（{self.cfg.api_key_env or 'ANTHROPIC_API_KEY'}）")
        kwargs = self.build_kwargs(system, prompt, max_tokens, None, False, 0)
        kwargs["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
        names = {t.name for t in tools}
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        records: list[ToolCallRecord] = []
        usage: dict[str, Any] = {}
        texts: list[str] = []  # 只收集最后一轮（含其 pause_turn 续跑）的文字
        rounds = calls_made = continuations = 0
        resp = None
        while True:
            last = rounds >= max_rounds
            req = {**kwargs, "tool_choice": {"type": "none"}} if last else kwargs  # 平时不传 tool_choice（即 auto）
            resp = await self._create(messages, req)
            calls_made += 1
            add_usage(usage, _usage(resp))
            texts += [b.text for b in resp.content if getattr(b, "type", "") == "text"]
            if resp.stop_reason == "pause_turn" and continuations < self.MAX_CONTINUATIONS:
                continuations += 1
                messages = [*messages, {"role": "assistant", "content": resp.content}]
                continue
            uses = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
            if resp.stop_reason != "tool_use" or not uses or last:
                break
            calls = [PendingCall(id=b.id, name=b.name, arguments=parse_arguments(getattr(b, "input", None)),
                                 raw=str(getattr(b, "input", ""))[:500]) for b in uses]
            outputs = await run_tool_calls(calls, execute, round_no=rounds + 1, records=records, tool_names=names)
            results = [{"type": "tool_result", "tool_use_id": c.id, "content": o.content,
                        **({"is_error": True} if o.is_error else {})} for c, o in zip(calls, outputs)]
            # 整条 assistant content 原样发回（thinking 块不能删改），本轮所有结果放在同一条 user 消息里
            messages = [*messages, {"role": "assistant", "content": resp.content}, {"role": "user", "content": results}]
            rounds += 1
            texts = []
        if resp.stop_reason == "max_tokens":
            log.warning("[%s] 输出被 max_tokens=%s 截断", self.name, kwargs["max_tokens"])
            if any(getattr(b, "type", "") == "tool_use" for b in resp.content):
                # 截断在工具调用中间：剩下的只是“让我查一下……”之类的开场白，不能当回答，交给路由器换供应商
                raise LLMError(f"[{self.name}] 输出在工具调用中被 max_tokens 截断")
        text = "".join(texts).strip()
        if not text:
            raise LLMError(f"[{self.name}] 返回了空内容（stop_reason={resp.stop_reason}，第 {calls_made} 次调用）")
        return LLMResult(text=text, model=getattr(resp, "model", None) or self.model, provider=self.name,
                         stop_reason=resp.stop_reason, usage=usage, tool_calls=records, rounds=calls_made)

    async def aclose(self) -> None:
        if self._client is not None and hasattr(self._client, "close"):
            await self._client.close()
