"""推送正文的轻量 Markdown → 安全的 HTML。

推送正文（models.Message.body）只用到：**粗体**、行首 “- ” 的列表（缩进的是子项）、[文字](链接)、空行分段。
先整体转义（正文里有新闻标题、大模型的回答，都不可信），再把这几种写法换成标签；链接只认 http(s)，
所以 [x](javascript:...) 原样留成文字。外链一律新窗口打开、不带 referrer。
"""

from __future__ import annotations

import html
import re

from markupsafe import Markup

_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BOLD = re.compile(r"\*\*([^*\n]+?)\*\*")
_ITEM = re.compile(r"^(\s*)[-•]\s+(.*)$")


def inline(text: str) -> str:
    """一行里的链接和粗体（text 已经转义过）。"""
    out = _LINK.sub(lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener noreferrer">{m.group(1)}</a>', text)
    return _BOLD.sub(r"<strong>\1</strong>", out)


def md_to_html(text: str | None) -> Markup:
    if not text:
        return Markup("")
    lines = html.escape(text, quote=True).splitlines()
    parts: list[str] = []
    in_list = False
    para: list[str] = []

    def flush_para() -> None:
        if para:
            parts.append("<p>" + "<br>".join(para) + "</p>")
            para.clear()

    for raw in lines:
        m = _ITEM.match(raw)
        if m:
            flush_para()
            if not in_list:
                parts.append("<ul>")
                in_list = True
            cls = ' class="sub"' if len(m.group(1).replace("\t", "  ")) >= 2 else ""
            parts.append(f"<li{cls}>{inline(m.group(2))}</li>")
            continue
        if in_list:
            parts.append("</ul>")
            in_list = False
        if raw.strip():
            para.append(inline(raw.strip()))
        else:
            flush_para()
    if in_list:
        parts.append("</ul>")
    flush_para()
    return Markup("".join(parts))


def strip_md(text: str | None) -> str:
    """去掉 Markdown 记号，只留文字（列表摘要、title 属性用）。"""
    if not text:
        return ""
    out = _LINK.sub(lambda m: m.group(1), text)
    out = _BOLD.sub(r"\1", out)
    return re.sub(r"^\s*[-•]\s+", "", out, flags=re.M)
