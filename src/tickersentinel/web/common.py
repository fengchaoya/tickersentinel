"""各页面共用：模板环境、过滤器、页面渲染、运行状态（tsen run 心跳 + 各组现在所处的时段）。

模板里能用的：
- 变量：ctx（WebContext）、settings、u（universe）、token、mode、mode_name、readonly、up_color、nav（当前导航项）、now
- 过滤器：local(ts, fmt)（本地时间；接受 datetime 或 ISO 字符串）、tzabbr、pct(x, digits)（带正负号）、ud(x)（up / down / flat，
  配合 CSS 的 .up / .down 着色，红涨绿跌还是绿涨红跌由 html[data-up] 决定）、md（推送正文 → 安全 HTML）、plain（去掉 Markdown）、
  display(symbol)（代码 + 名称）、label(symbol)（名称）、ago(ts)（“3 分钟前”）、weekday(ts)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from tickersentinel.timeutil import UTC, fmt_local, tz_abbr, weekday_cn
from tickersentinel.web.context import MODE_NAMES, WebContext
from tickersentinel.web.markdown import md_to_html, strip_md

HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = HERE / "templates"
STATIC_DIR = HERE / "static"

NAV = [("overview", "总览", "/"), ("alerts", "异动", "/alerts"), ("calendar", "日历", "/calendar"),
       ("macro", "宏观", "/macro")]

# 监控分组（jobs/monitor.build_groups 的 key）的中文名
GROUP_NAMES = {"us": "美股", "cme": "期货", "utc": "加密货币", "XKRX": "首尔", "XTKS": "东京", "XHKG": "香港",
               "XTAI": "台湾", "XSHG": "上海 / 深圳"}


def to_dt(ts: Any) -> datetime | None:
    """datetime / ISO 字符串 → 带时区的 datetime（无时区的按 UTC）。"""
    if ts is None or ts == "":
        return None
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return None
    if not isinstance(ts, datetime):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def fmt_pct(x: Any, digits: int = 1) -> str:
    if x is None:
        return "—"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "—"
    return f"{v:+.{digits}f}%"


def up_down(x: Any) -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "flat"
    return "up" if v > 0 else "down" if v < 0 else "flat"


def ago(ts: Any, now: datetime) -> str:
    t = to_dt(ts)
    if t is None:
        return "—"
    s = (now - t).total_seconds()
    if s < 0:
        s = -s
        unit = "后"
    else:
        unit = "前"
    if s < 60:
        return "刚刚" if unit == "前" else "马上"
    if s < 3600:
        return f"{int(s // 60)} 分钟{unit}"
    if s < 86400:
        return f"{int(s // 3600)} 小时{unit}"
    return f"{int(s // 86400)} 天{unit}"


def static_url(path: str) -> str:
    """静态文件的网址带上版本号（文件修改时间）：改了 css / js 之后浏览器一定取新的。
    不带的话 Safari 会按 Last-Modified 自己判断“还新鲜”、继续用缓存里的旧脚本（2026-11-01：网页提问发不出去就是这个原因）。"""
    path = path.lstrip("/")
    try:
        v = int((STATIC_DIR / path).stat().st_mtime)
    except OSError:
        return f"/static/{path}"
    return f"/static/{path}?v={v}"


def build_templates(ctx: WebContext) -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    env = templates.env
    tz = ctx.settings.tz
    u = lambda: ctx.settings.universe  # noqa: E731  热更新会换掉 universe：每次现取

    def local(ts: Any, fmt: str = "%m-%d %H:%M") -> str:
        t = to_dt(ts)
        return fmt_local(t, tz, fmt) if t else "—"

    env.filters.update({
        "local": local,
        "tzabbr": lambda ts: tz_abbr(to_dt(ts), tz) if to_dt(ts) else "",
        "weekday": lambda ts: weekday_cn(to_dt(ts), tz) if to_dt(ts) else "",
        "pct": fmt_pct,
        "ud": up_down,
        "md": md_to_html,
        "plain": strip_md,
        "display": lambda s: u().display(s) if s else "",
        "label": lambda s: u().label(s) if s else "",
        "ago": lambda ts: ago(ts, ctx.clock()),
    })
    env.globals.update({"ctx": ctx, "nav_items": NAV, "group_names": GROUP_NAMES, "static": static_url})
    return templates


def render(request: Request, name: str, nav: str = "", status_code: int = 200, **data: Any):
    """渲染一个页面（或片段）。页面模板继承 base.html；片段（partials/）不继承。"""
    ctx: WebContext = request.app.state.ctx
    templates: Jinja2Templates = request.app.state.templates
    base = {
        "settings": ctx.settings, "u": ctx.settings.universe, "token": ctx.token, "mode": ctx.mode,
        "mode_name": MODE_NAMES.get(ctx.mode, ctx.mode), "readonly": ctx.readonly,
        "up_color": ctx.settings.web.up_color, "nav": nav, "now": ctx.clock(),
    }
    return templates.TemplateResponse(request, name, {**base, **data}, status_code=status_code)


def segments_now(ctx: WebContext, now: datetime | None = None) -> list[dict[str, Any]]:
    """各监控分组现在所处的时段：[{key, name, active, label, until, symbols}]。不依赖 tsen run，按交易日历现算。"""
    from tickersentinel.jobs.monitor import build_groups

    now = now or ctx.clock()
    out = []
    for g in build_groups(ctx.settings, ctx.calendar):
        try:
            seg = g.clock.segment_at(now)
        except Exception:  # 交易日历超出范围等
            seg = None
        active = seg is not None and bool(g.active(seg))
        out.append({
            "key": g.key, "name": GROUP_NAMES.get(g.key, g.key), "active": active,
            "label": (seg.label or "交易中") if active and seg is not None else "休市",
            "until": seg.close if active and seg is not None else None,
            "symbols": list(g.active(seg)) if active and seg is not None else list(g.symbols),
        })
    return out
