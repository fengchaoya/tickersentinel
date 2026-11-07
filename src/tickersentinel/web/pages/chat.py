"""网页上提问（总览页左边的“对话”选项卡）：和飞书里同一套对话（agents/chat.py 的 ChatAgent，带查询工具）。
用飞书推送时和飞书是**同一个会话**（chat_id = FEISHU_CHAT_ID）：上下文共用，飞书里的问答也显示在网页上（2026-11-01 用户要求）；
没有飞书时会话 id = web。历史存在 chat_messages 里；/new 清空。本地大模型一次回答要 20–50 秒，前端先显示“思考中”。

路由：
- GET  /partials/chat     最近几轮问答（页面打开时显示）
- POST /api/chat          {"text": "...", "reply_to_text": 消息流里点“问一下”的那条推送（可选）} → {"ok", "answer_html", "question_html"}（写请求：要带 token）
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from tickersentinel.web.common import render
from tickersentinel.web.markdown import md_to_html

log = logging.getLogger(__name__)
router = APIRouter()

WEB_CHAT_ID = "web"


def chat_id(ctx) -> str:
    """网页提问用的会话：用飞书时和飞书同一个会话，否则单独一个 web 会话。"""
    s = ctx.settings
    if s.notify.provider == "feishu":
        cid = s.secrets.get("feishu_chat_id")
        if cid:
            return cid
    return WEB_CHAT_ID
ANSWER_TIMEOUT = 300.0  # 本地模型带工具最多答几轮，正常 20–50 秒
MAX_TEXT = 2000
MAX_CONTEXT = 6000  # “问一下”带上的推送正文最多这么多字
HISTORY_TURNS = 6


def history(ctx) -> list[dict[str, str]]:
    """最近几轮问答（旧的在前），给页面直接渲染。"""
    try:
        rows = ctx.storage.recent_chat(chat_id(ctx), HISTORY_TURNS * 2)
    except Exception:  # pragma: no cover - 旧库没有表等
        return []
    return [{"role": role, "html": md_to_html(text) if role == "assistant" else text} for role, text in rows]


@router.get("/partials/chat")
async def chat_partial(request: Request):
    ctx = request.app.state.ctx
    return render(request, "overview/_chat_log.html", turns=history(ctx), chat_ready=ctx.chat is not None)


@router.post("/api/chat")
async def chat_api(request: Request):
    ctx = request.app.state.ctx
    if ctx.chat is None:
        return JSONResponse({"ok": False, "error": "这个网页没有接上对话功能（请用 tsen run 或 tsen web 打开）"}, status_code=503)
    try:
        body = await request.json()
    except ValueError:
        body = {}
    text = str((body or {}).get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "问题是空的"}, status_code=422)
    if len(text) > MAX_TEXT:
        return JSONResponse({"ok": False, "error": f"问题太长：最多 {MAX_TEXT} 字"}, status_code=422)
    from tickersentinel.agents.chat import IncomingMessage

    try:
        ref = str((body or {}).get("reply_to_text") or "").strip()[:MAX_CONTEXT] or None  # 像在飞书里回复那条推送
        msg = IncomingMessage(text=text, chat_id=chat_id(ctx), reply_to_text=ref)
        answer = await asyncio.wait_for(ctx.chat.handle(msg), ANSWER_TIMEOUT)
    except TimeoutError:
        return JSONResponse({"ok": False, "error": f"等了 {ANSWER_TIMEOUT / 60:.0f} 分钟还没有回答，大模型可能没在运行"})
    except Exception as exc:  # 和飞书一样：出错只回一句，详情在日志
        log.exception("网页提问处理失败")
        return JSONResponse({"ok": False, "error": f"处理这个问题时出错了：{type(exc).__name__}: {exc}"[:300]})
    return JSONResponse({"ok": True, "answer_html": str(md_to_html(answer or "（没有回答）"))})
