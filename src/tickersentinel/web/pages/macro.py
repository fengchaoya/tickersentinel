"""宏观页（决策 21）：宏观变量的趋势小图、“谁动了”排行、财政部收益率曲线，数据都来自本地分钟线库（barstore.py）。

- GET  /macro?window=today|24h|5d|1m&start=&end=&alert=<id>&live=1   页面
  休市（周末、节假日）时预设窗口截到最近收盘（macro_data.market_end），live=1 截到现在
- GET  /api/macro/series?（同上）    趋势小图：每个变量“较窗口起点的变化”，附窗口里的异动（画竖线）
- GET  /api/macro/movers?（同上）    “谁动了”：变化 ÷ 平常的波动（z 值），从大到小
- GET  /api/macro/curve?days=180     财政部收益率曲线（日线）
- POST /api/bars/refresh             现在补抓一轮（写请求：要带 token；预览模式下 409）
start / end：ISO 时间；不带时区的按本地时区（settings.timezone）理解（页面上的日期时间输入框就是本地时间）。
数据整理在 web/macro_data.py。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request

from tickersentinel.storage import _from_iso
from tickersentinel.timeutil import fmt_local
from tickersentinel.web import macro_data as md
from tickersentinel.web.alerts_chart import shift
from tickersentinel.web.common import render

router = APIRouter()
log = logging.getLogger(__name__)

REFRESH_TIMEOUT = 90.0  # 秒
MAX_CUSTOM_SPAN = timedelta(days=120)


def parse_time(s: str | None, tz) -> datetime | None:
    if not s:
        return None
    try:
        t = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(400, f"时间格式不对：{s!r}") from exc
    return t.replace(tzinfo=tz) if t.tzinfo is None else t


def window_of(ctx, window: str | None, start: str | None, end: str | None, alert: str | None,
              live: bool = False) -> dict[str, Any]:
    """解析时间窗口：{start, end, name, window, alert, anchored}。参数写错返回 400（不认识的预设窗口退回 24 小时）。
    预设窗口在休市时截到最近收盘（anchored = 截到的时刻），live 时截到现在。"""
    tz = ctx.settings.tz
    now = ctx.clock()
    alert_row = None
    alert_ts = None
    if alert:
        try:
            alert_row = ctx.storage.alert_row(int(alert))
        except (ValueError, OverflowError):  # 超出 SQLite 整数范围的编号也当“没有这条”
            alert_row = None
        if alert_row is None:
            raise HTTPException(404, f"没有这条异动：{alert}")
        alert_ts = _from_iso(alert_row["ts"])
    s, e = parse_time(start, tz), parse_time(end, tz)
    anchor = None
    if alert_ts is None and s is None and not live and ctx.bars is not None:
        anchor = md.market_end(ctx.bars, ctx.settings, now)
    try:
        a, b, name = md.resolve_window(window, now, tz, start=s, end=e, alert_ts=alert_ts, anchor=anchor)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if b - a > MAX_CUSTOM_SPAN:
        a = b - MAX_CUSTOM_SPAN
    return {"start": a, "end": b, "name": name, "window": window if window in md.WINDOWS else None,
            "alert": alert_row, "anchored": anchor}


def anchored_info(ctx, anchor: datetime | None, live: bool) -> dict[str, Any] | None:
    """页面上的说明：休市时截到了哪（weekend：现在是 CME 的周末休市，美东周五 17:00 到周日 18:00；否则是节假日或
    数据没更新），或者正“截到现在”。"""
    if anchor is None:
        return None
    et = ctx.clock().astimezone(ZoneInfo("America/New_York"))
    wd = et.weekday()
    weekend = wd == 5 or (wd == 4 and et.hour >= 17) or (wd == 6 and et.hour < 18)
    return {"live": live, "at": anchor, "weekend": weekend}


def alert_marks(ctx, start: datetime, end: datetime) -> list[dict[str, Any]]:
    tz = ctx.settings.tz
    out = []
    for r in ctx.storage.alert_rows_between(start, end + timedelta(seconds=1)):
        ts = _from_iso(r["ts"])
        out.append({"id": r["id"], "t": shift(ts, tz), "ts": ts.isoformat(), "symbol": r["symbol"],
                    "name": ctx.settings.universe.display(r["symbol"]), "direction": r["direction"],
                    "move_pct": r["move_pct"]})
    return out


def freshness(ctx) -> dict[str, Any]:
    """数据新不新：tsen run 在不在跑、采集器上一轮的情况。"""
    rt = ctx.runtime()
    hb = rt.get("heartbeat") or {}
    bars = hb.get("bars") if rt["running"] else None
    if ctx.collector is not None and ctx.mode == "embedded":
        bars = ctx.collector.status_json()
    return {"running": rt["running"], "collector": bars, "can_refresh": ctx.collector is not None and not ctx.readonly,
            "store": ctx.bars is not None}


def _store(ctx):
    if ctx.bars is None:
        raise HTTPException(503, "分钟线库没有打开")
    return ctx.bars


@router.get("/macro")
async def page(request: Request, window: str | None = None, start: str | None = None, end: str | None = None,
               alert: str | None = None, live: bool = False):
    """页面骨架：数据由 static/js/macro.js 从上面几个接口取。参数写错不报错：页面上说明，退回默认窗口。"""
    ctx = request.app.state.ctx
    tz = ctx.settings.tz
    warnings: list[str] = []
    if alert:
        try:
            w = window_of(ctx, None, None, None, alert)
        except HTTPException:
            warnings.append(f"没有这条异动（#{alert}），下面显示的是最近 24 小时。")
            alert = None
    if not alert and (start or end):
        try:
            if not start:
                raise HTTPException(400, "只给了结束时间，没给开始时间")
            w = window_of(ctx, None, start, end, None)
        except HTTPException as exc:
            warnings.append(f"自定义时间段不对（{exc.detail}），下面显示的是最近 24 小时。")
            start = end = None
    if not alert and not start:
        w = window_of(ctx, window, None, None, None, live)
    # 接口参数：和页面一样的口径（异动 > 自定义 > 预设窗口）
    if alert:
        params = {"alert": str(w["alert"]["id"])}
    elif start:
        params = {"start": start, **({"end": end} if end else {})}
    else:
        params = {"window": w["window"] or "24h", **({"live": "1"} if live else {})}
    alert_info = None
    if w["alert"]:
        r = w["alert"]
        alert_info = {"id": r["id"], "symbol": r["symbol"], "name": ctx.settings.universe.label(r["symbol"]),
                      "display": ctx.settings.universe.display(r["symbol"]), "ts": _from_iso(r["ts"]),
                      "direction": r.get("direction"), "move_pct": r.get("move_pct")}
    fmt = "%Y-%m-%dT%H:%M"
    cand = w["anchored"]  # 截到现在时也要知道“最近收盘”在哪，页面上才能给“回到最近收盘”
    if live and ctx.bars is not None:
        cand = md.market_end(ctx.bars, ctx.settings, ctx.clock())
    return render(
        request, "macro/index.html", nav="macro",
        windows=md.WINDOWS, current=params.get("window"), live=bool(params.get("live")), custom="start" in params, alert_info=alert_info,
        win_name=w["name"], win_start=w["start"], win_end=w["end"], warnings=warnings, query=urlencode(params),
        start_input=fmt_local(w["start"], tz, fmt), end_input=fmt_local(w["end"], tz, fmt),
        tzname=str(tz), has_store=ctx.bars is not None,
        anchored=anchored_info(ctx, cand, live) if not alert and not start else None,
        can_refresh=ctx.collector is not None and not ctx.readonly,
    )


@router.get("/api/macro/series")
async def api_series(request: Request, window: str | None = None, start: str | None = None, end: str | None = None,
                     alert: str | None = None, live: bool = False):
    ctx = request.app.state.ctx
    w = window_of(ctx, window, start, end, alert, live)
    store = _store(ctx)
    payload = await asyncio.to_thread(md.series_payload, store, ctx.settings, ctx.calendar, w["start"], w["end"])
    payload.update(name=w["name"], window=w["window"], alerts=alert_marks(ctx, w["start"], w["end"]),
                   anchored=w["anchored"].isoformat() if w["anchored"] else None,
                   alert_id=w["alert"]["id"] if w["alert"] else None, freshness=freshness(ctx))
    return payload


@router.get("/api/macro/movers")
async def api_movers(request: Request, window: str | None = None, start: str | None = None, end: str | None = None,
                     alert: str | None = None, live: bool = False):
    ctx = request.app.state.ctx
    w = window_of(ctx, window, start, end, alert, live)
    store = _store(ctx)
    payload = await asyncio.to_thread(md.movers, store, ctx.settings, ctx.calendar, w["start"], w["end"])
    payload["name"] = w["name"]
    return payload


@router.get("/api/macro/curve")
async def api_curve(request: Request, days: int = 180):
    ctx = request.app.state.ctx
    days = max(7, min(days, 3650))
    since = (ctx.clock() - timedelta(days=days)).astimezone(ctx.settings.tz).date()
    return await asyncio.to_thread(md.curve_payload, _store(ctx), since)


@router.post("/api/bars/refresh")
async def api_refresh(request: Request):
    ctx = request.app.state.ctx
    if ctx.collector is None or ctx.readonly:
        raise HTTPException(409, "预览模式（或分钟线库已关闭）不能补抓：数据只读")
    try:
        rows = await asyncio.wait_for(ctx.collector.collect(), REFRESH_TIMEOUT)
    except asyncio.TimeoutError as exc:
        raise HTTPException(504, "补抓超时：数据源响应太慢，稍后再试") from exc
    st = ctx.collector.status_json()
    return {"rows": rows, "status": st, "backfilling": st.get("backfilling", 0)}
