"""异动页：列表 /alerts（筛选、按日分组、导出 CSV）与详情 /alerts/{id}（走势图、拆分、宏观、归因、前后日程、标注）。

- GET  /alerts                     列表（筛选参数见 web/alerts_data.Filters）
- GET  /alerts.csv                 同样的筛选导出 CSV（UTF-8 带 BOM，Excel 直接打开）
- GET  /alerts/{id}                详情
- GET  /api/alerts/{id}/bars       走势图数据（web/alerts_chart.py）
- POST /api/alerts/{id}/label      保存标注（label 真实原因 / notes 备注；表单或 JSON）
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from tickersentinel.timeutil import fmt_local, tz_abbr
from tickersentinel.web import alerts_chart
from tickersentinel.web.alerts_data import (
    LABELED,
    MAX_ROWS,
    MAX_TEXT,
    RANGES,
    RULES,
    SEGMENTS,
    CONFIDENCE_BADGES,
    Filters,
    et_time,
    filter_rows,
    group_by_day,
    nearby_events,
    parse_attribution,
    parse_macro,
    split_bar,
    summarize,
    symbol_options,
    to_csv,
    to_view,
)
from tickersentinel.web.common import render
from tickersentinel.web.context import WebContext

router = APIRouter()
log = logging.getLogger(__name__)

MACRO_MOVE_DEFAULT = timedelta(minutes=60)  # 没有起点时：异动前 60 分钟
MACRO_MOVE_MAX = timedelta(hours=6)  # 起点太早时最多往前 6 小时
MACRO_MOVE_TOP = 8
MACRO_Z_STRONG = 2.0
MACRO_Z_BAR = 4.0  # 横条按 |z| 画，满格 = 4


def _ctx(request: Request) -> WebContext:
    return request.app.state.ctx


def _id(raw: str) -> int:
    """路径里的 id：不是正整数就当不存在（404，而不是 422）。"""
    if not raw.isdigit() or len(raw) > 12:
        raise HTTPException(404, "这条异动不存在")
    return int(raw)


@router.get("/alerts")
async def page(request: Request):
    ctx = _ctx(request)
    s = ctx.settings
    f = Filters.parse(request.query_params)
    now = ctx.clock()
    rows = ctx.storage.alert_rows()
    picked = filter_rows(rows, f, now, s.tz)
    picked.reverse()  # 新的在前
    shown = picked[:MAX_ROWS]
    views = [to_view(r, s) for r in shown]
    for v in views:
        v["bar"] = split_bar(v)
    return render(
        request, "alerts/index.html", nav="alerts",
        f=f, ranges=RANGES, segments=SEGMENTS, rules=RULES, labeled_opts=LABELED,
        symbols=symbol_options(rows, s, f.symbol), summary=summarize(picked, s), groups=group_by_day(views, s.tz),
        truncated=len(picked) > MAX_ROWS, max_rows=MAX_ROWS, total_all=len(rows),
        csv_href="/alerts.csv" + f.query(),
    )


@router.get("/alerts.csv")
async def export_csv(request: Request):
    ctx = _ctx(request)
    s = ctx.settings
    f = Filters.parse(request.query_params)
    now = ctx.clock()
    picked = filter_rows(ctx.storage.alert_rows(), f, now, s.tz)
    picked.reverse()
    body = to_csv([to_view(r, s) for r in picked], s)
    name = f"alerts-{fmt_local(now, s.tz, '%Y%m%d')}.csv"
    return Response(body.encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


def _neighbors(rows: list[dict], alert_id: int) -> tuple[dict | None, dict | None, list[dict]]:
    """按时间相邻的上一条（更早）/ 下一条（更晚），以及挂在这一条下面的跟帖（same_wave_as = 这一条）。"""
    idx = next((i for i, r in enumerate(rows) if r.get("id") == alert_id), None)
    prev = rows[idx - 1] if idx is not None and idx > 0 else None
    nxt = rows[idx + 1] if idx is not None and idx + 1 < len(rows) else None
    follows = []
    for r in rows:
        raw = r.get("attribution_json")
        if not raw or '"same_wave_as"' not in raw:
            continue
        try:
            lead = json.loads(raw).get("same_wave_as")
        except (ValueError, AttributeError):
            continue
        if lead is not None and str(lead) == str(alert_id) and r.get("id") != alert_id:
            follows.append(r)
    return prev, nxt, follows


@router.get("/alerts/{alert_id}")
async def detail(request: Request, alert_id: str):
    ctx = _ctx(request)
    s = ctx.settings
    alert_id = _id(alert_id)
    row = ctx.storage.alert_row(alert_id)
    if row is None:
        raise HTTPException(404, "这条异动不存在")
    v = to_view(row, s)
    rows = ctx.storage.alert_rows()
    prev, nxt, follows = _neighbors(rows, alert_id)
    attr = parse_attribution(row)
    lead = None
    if attr["same_wave_as"] is not None:
        lead_row = ctx.storage.alert_row(attr["same_wave_as"])
        lead = to_view(lead_row, s) if lead_row else {"id": attr["same_wave_as"], "missing": True}
        if lead_row:
            lead["lead_summary"] = ctx.storage.attribution_summary(attr["same_wave_as"])
    before, ahead = nearby_events(ctx, v["alert"]) if v.get("alert") is not None else ([], [])
    moves = await macro_moves(ctx, v.get("alert"), v.get("ts"))
    return render(
        request, "alerts/detail.html", nav="alerts",
        v=v, attr=attr, lead=lead, follows=[to_view(r, s) for r in follows],
        prev=to_view(prev, s) if prev else None, next=to_view(nxt, s) if nxt else None,
        macro=parse_macro(row.get("macro_json"), s), events_before=before, events_ahead=ahead,
        et_time=et_time(v["ts"]) if v.get("ts") else "", conf_badges=CONFIDENCE_BADGES, max_text=MAX_TEXT,
        has_prices=ctx.prices is not None or ctx.bars is not None, moves=moves, alert_id=alert_id,
        recent=v.get("ts") is not None and (ctx.clock() - v["ts"]).total_seconds() < 600,
    )


def macro_window(alert, ts: datetime) -> tuple[datetime, datetime]:
    """宏观同期变化的时间段：[这一波的起点, 异动时刻]。起点 = 触发器里最早的 anchor_ts，没有就用前 60 分钟；最多往前 6 小时。"""
    anchors = [t.anchor_ts for t in (getattr(alert, "triggers", None) or []) if t.anchor_ts is not None and t.anchor_ts < ts]
    start = min(anchors) if anchors else ts - MACRO_MOVE_DEFAULT
    return max(start, ts - MACRO_MOVE_MAX), ts


def _fmt_move(item: dict) -> str:
    chg = item["change"]
    if item.get("unit") == "bp":
        n = round(chg)
        return f"{n:+d}bp" if n else "±0bp"
    return f"{chg:+.2f}%"


async def macro_moves(ctx: WebContext, alert, ts: datetime | None) -> dict[str, Any]:
    """异动详情的“宏观同期变化”卡片：{state: ok | empty | no_store | error, start, end, items}。出错只影响这张卡片。
    movers 已按 |z| 排好（大涨大跌都在前面），取前 MACRO_MOVE_TOP 个；异动的这只持仓本身不列（其他持仓照列，标“持仓”）。"""
    if ts is None:
        return {"state": "empty", "start": None, "end": None, "items": []}
    start, end = macro_window(alert, ts)
    out: dict[str, Any] = {"state": "empty", "start": start, "end": end, "items": []}
    if ctx.bars is None:
        out["state"] = "no_store"
        return out
    from tickersentinel.web.macro_data import movers

    try:
        res = await asyncio.to_thread(movers, ctx.bars, ctx.settings, ctx.calendar, start, end)
    except Exception as exc:  # noqa: BLE001 - 读库或计算出错：卡片里写明，详情页照常
        log.warning("宏观同期变化算不出来（异动 %s）：%s", getattr(alert, "id", None), exc)
        out.update(state="error", reason=f"{type(exc).__name__}: {str(exc)[:120]}")
        return out
    items = []
    own = getattr(alert, "symbol", None)
    rows = [it for it in (res.get("items") or []) if it.get("symbol") != own]  # 持仓也是宏观变量（如黄金）时同样去掉
    for it in rows[:MACRO_MOVE_TOP]:
        z = it.get("z")
        tag = it.get("sub_tag") or ("期货" if it.get("futures") else None)
        name = it["name"] + (f"（{tag}）" if tag and tag not in it["name"] else "")
        if it.get("holding"):
            name += "（持仓）"
        items.append({"name": name, "symbol": it.get("symbol"), "group": it.get("group"), "change": it["change"],
                      "change_text": _fmt_move(it), "z": z, "z_text": f"{z:+.1f}" if z is not None else "—",
                      "strong": z is not None and abs(z) >= MACRO_Z_STRONG,
                      "bar": round(min(abs(z), MACRO_Z_BAR) / MACRO_Z_BAR * 100, 1) if z is not None else 0.0})
    if items:
        out.update(state="ok", items=items)
    return out


@router.get("/api/alerts/{alert_id}/bars")
async def bars(request: Request, alert_id: str) -> Any:
    ctx = _ctx(request)
    alert_id = _id(alert_id)
    row = ctx.storage.alert_row(alert_id)
    if row is None:
        raise HTTPException(404, "这条异动不存在")
    cache = getattr(request.app.state, "alerts_chart_cache", None)
    if cache is None:
        cache = request.app.state.alerts_chart_cache = alerts_chart.ChartCache()
    if (hit := cache.get(alert_id)) is not None:
        return JSONResponse(hit)
    v = to_view(row, ctx.settings)
    if v.get("alert") is None:
        return JSONResponse({"available": False, "reason": "这条记录不完整，画不了走势", "series": []})
    try:
        payload = await alerts_chart.build(ctx.settings, ctx.calendar, ctx.prices, v["alert"], ctx.clock(),
                                           store=ctx.bars)
    except Exception as exc:  # 兜底：图画不出来不能让详情页报错
        payload = {"available": False, "reason": f"整理走势数据出错：{type(exc).__name__}", "series": []}
    cache.put(alert_id, payload)
    return JSONResponse(payload)


async def _read_label_body(request: Request) -> dict[str, Any]:
    """只认两种格式：表单（application/x-www-form-urlencoded，htmx 默认）或 JSON。
    没装 python-multipart，表单自己解析；其他格式（multipart 等）读不懂，返回 415——
    不能把读不懂的请求当成“两项都清空”，否则会悄悄抹掉已有的标注。"""
    ctype = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if ctype not in ("application/json", "application/x-www-form-urlencoded"):
        raise HTTPException(415, "只接受表单（application/x-www-form-urlencoded）或 JSON")
    raw = await request.body()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "请求内容不是 UTF-8")
    if ctype == "application/json":
        try:
            data = json.loads(text)
        except ValueError:
            raise HTTPException(400, "请求内容不是有效的 JSON")
        if not isinstance(data, dict):
            raise HTTPException(400, "请求内容应当是 JSON 对象")
    else:
        data = {k: v[-1] for k, v in parse_qs(text, keep_blank_values=True).items()}
    if "label" not in data and "notes" not in data:
        raise HTTPException(400, "请求里没有 label 或 notes")
    return data


def _clean(v: Any, field: str) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        raise HTTPException(422, f"{field}应当是文字")
    v = v.replace("\r\n", "\n").strip()
    if len(v) > MAX_TEXT:
        raise HTTPException(422, f"{field}太长：最多 {MAX_TEXT} 字（现在 {len(v)} 字）")
    return v or None


@router.post("/api/alerts/{alert_id}/label")
async def save_label(request: Request, alert_id: str):
    ctx = _ctx(request)
    try:
        alert_id = _id(alert_id)
        current = ctx.storage.alert_row(alert_id)
        if current is None:
            raise HTTPException(404, "这条异动不存在")
        data = await _read_label_body(request)
        # 没带的那一项保持原样；明确给了空字符串或 null 才清空
        label = _clean(data["label"], "真实原因") if "label" in data else current.get("label")
        notes = _clean(data["notes"], "备注") if "notes" in data else current.get("notes")
    except HTTPException as exc:
        if request.headers.get("hx-request"):  # htmx：短文字，app.js 直接显示在提示里
            return PlainTextResponse(str(exc.detail), status_code=exc.status_code)
        raise
    ctx.storage.set_alert_label(alert_id, label, notes)
    now = ctx.clock()
    when = f"{fmt_local(now, ctx.settings.tz, '%H:%M')} {tz_abbr(now, ctx.settings.tz)}"
    if "application/json" in request.headers.get("accept", "") and "text/html" not in request.headers.get("accept", ""):
        return JSONResponse({"ok": True, "label": label, "notes": notes, "saved_at": now.isoformat(),
                             "persisted": not ctx.readonly})
    return render(request, "alerts/_saved.html", when=when, label=label, notes=notes)
