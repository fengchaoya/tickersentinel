"""异动页的数据整理：alerts 表的原始行 → 列表 / 详情 / CSV 要显示的字段。不碰网络，纯计算，方便单测。

- 旧记录可能缺 segment / benchmark / macro_json，规则可能是旧版的 level（关口）；JSON 损坏的行也不能让页面 500：
  to_view 出错时退回只有基本字段的一行（broken=True）。
- 筛选（时间范围、代码、时段、规则、标注）在服务器端做，参数原样保留在链接里（Filters.query）。
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any
from urllib.parse import urlencode

from tickersentinel.agents.render import alert_benchmark, fmt_price, sector_judgment
from tickersentinel.config import EXCHANGE_NAMES, NIGHT_PREFIX, Settings, exchange_of, is_pseudo
from tickersentinel.models import Alert, Event
from tickersentinel.sources.macro import MacroQuote
from tickersentinel.storage import Storage
from tickersentinel.timeutil import ET, UTC, fmt_local, tz_abbr, weekday_cn

# ───────── 筛选 ─────────

RANGES = [("today", "今天", 1), ("3d", "3天", 3), ("7d", "7天", 7), ("30d", "30天", 30), ("all", "全部", None)]
RANGE_DAYS = {k: n for k, _, n in RANGES}
DEFAULT_RANGE = "7d"
SEGMENTS = [("night", "夜盘"), ("pre", "盘前"), ("regular", "盘中"), ("post", "盘后"), ("cme", "期货"), ("utc", "加密"),
            ("exchange", "海外")]
SEGMENT_NAMES = dict(SEGMENTS)
SEGMENT_BADGES = {"night": "badge-purple", "pre": "badge-amber", "regular": "", "post": "badge-purple", "cme": "badge-teal",
                  "utc": "badge-teal", "exchange": "badge-accent"}
RULES = [("rapid", "急涨急跌"), ("day", "全天累计"), ("idio", "个股特异")]
RULE_NAMES = {"rapid": "急涨急跌", "day": "全天累计", "idio": "个股特异", "level": "关口（旧）"}
LABELED = [("yes", "已标注"), ("no", "未标注")]
DRIVER_BADGES = {"个股": "badge-purple", "板块": "badge-accent", "宏观": "badge-teal", "不明": "badge-outline"}
CONFIDENCE_BADGES = {"高": "badge-accent", "中": "badge-amber", "低": ""}
MAX_ROWS = 500  # 列表最多显示这么多条（CSV 不限）
MAX_TEXT = 2000  # 标注每一栏最多多少字


@dataclass
class Filters:
    range: str = DEFAULT_RANGE
    symbol: str = ""
    seg: str = ""
    rule: str = ""
    labeled: str = ""

    @classmethod
    def parse(cls, params: Any) -> "Filters":
        """查询参数 → 筛选条件；不认识的值当作没写（不报错）。"""
        get = (lambda k: (params.get(k) or "").strip()) if params is not None else (lambda k: "")
        rng = get("range")
        seg = get("seg")
        rule = get("rule")
        lab = get("labeled")
        return cls(range=rng if rng in RANGE_DAYS else DEFAULT_RANGE, symbol=get("symbol")[:64],
                   seg=seg if seg in SEGMENT_NAMES else "", rule=rule if rule in RULE_NAMES and rule != "level" else "",
                   labeled=lab if lab in ("yes", "no") else "")

    def as_dict(self) -> dict[str, str]:
        d = {"range": self.range, "symbol": self.symbol, "seg": self.seg, "rule": self.rule, "labeled": self.labeled}
        return {k: v for k, v in d.items() if v and not (k == "range" and v == DEFAULT_RANGE)}

    def query(self, **changes: str) -> str:
        """带上现有筛选的查询串（changes 覆盖其中几项，值为空串时去掉）："?range=30d&symbol=MU"。"""
        d = {**self.as_dict(), **changes}
        d = {k: v for k, v in d.items() if v and not (k == "range" and v == DEFAULT_RANGE)}
        return ("?" + urlencode(d)) if d else ""

    @property
    def active(self) -> bool:
        return bool(self.symbol or self.seg or self.rule or self.labeled)

    def since(self, now: datetime, tz) -> datetime | None:
        """时间范围的起点（按本地日期：“今天”= 本地今天 0 点起）。全部为 None。"""
        n = RANGE_DAYS.get(self.range)
        if n is None:
            return None
        today = now.astimezone(tz).date()
        start = datetime.combine(today - timedelta(days=n - 1), time(0), tzinfo=tz)
        return start.astimezone(UTC)


def _loads(raw: Any) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def row_rules(row: dict) -> set[str]:
    """这条异动触发了哪些规则（全部触发，不只是标题）。旧版 level 算进“全天累计”。"""
    rules = set()
    trs = _loads(row.get("triggers_json"))
    if isinstance(trs, list):
        rules = {t.get("rule") for t in trs if isinstance(t, dict)}
    if row.get("rule"):
        rules.add(row["rule"])
    if "level" in rules:
        rules.add("day")
    return {r for r in rules if r}


def has_label(row: dict) -> bool:
    return bool((row.get("label") or "").strip() or (row.get("notes") or "").strip())


def filter_rows(rows: list[dict], f: Filters, now: datetime, tz) -> list[dict]:
    """按筛选条件过滤（rows 按时间升序，返回同样的顺序）。"""
    since = f.since(now, tz)
    out = []
    for r in rows:
        if since is not None:
            t = _parse_ts(r.get("ts"))
            if t is None or t < since:
                continue
        if f.symbol and r.get("symbol") != f.symbol:
            continue
        if f.seg and (r.get("segment") or "regular") != f.seg:
            continue
        if f.rule and f.rule not in row_rules(r):
            continue
        if f.labeled == "yes" and not has_label(r):
            continue
        if f.labeled == "no" and has_label(r):
            continue
        out.append(r)
    return out


def _parse_ts(s: Any) -> datetime | None:
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=UTC)
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


# ───────── 一条异动的显示字段 ─────────


def segment_name(segment: str, symbol: str) -> str:
    """时段的叫法；海外交易所写交易所名（首尔、东京……）。"""
    if segment == "exchange":
        ex = exchange_of(symbol)
        return EXCHANGE_NAMES.get(ex or "", "海外")
    return SEGMENT_NAMES.get(segment, segment or "盘中")


def symbol_kind(settings: Settings, symbol: str) -> str | None:
    """不是持仓时的小标签：夜间篮子 / 夜间信号 / 盯盘 / 范围外。持仓为 None。"""
    u = settings.universe
    if symbol.startswith(NIGHT_PREFIX):
        return "夜间篮子"
    inst = u.find(symbol)
    if inst is None:
        return "范围外"
    if inst.holding:
        return None
    return "夜间信号" if inst.related_to else "盯盘"


def symbol_name(settings: Settings, symbol: str) -> str:
    """名称（副标题用）：持仓写 assets.yaml 里的 name；其他用 universe.label。和代码一样时为空。"""
    u = settings.universe
    inst = u.find(symbol)
    name = inst.name if inst is not None and inst.name else u.label(symbol)
    return "" if not name or name == u.display(symbol) else name


@dataclass
class Cause:
    text: str
    confidence: str
    timing: str
    links: list[tuple[str, str]] = field(default_factory=list)  # (文字, 网址)
    notes: list[str] = field(default_factory=list)  # 日程：…… / 无法还原的 #n


_MD_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_CAUSE_LINE = re.compile(r"^\s*(\d+)\.\s")


def _text_links_by_cause(text: str | None) -> dict[int, list[tuple[str, str]]]:
    """推送的归因原文里，第 i 条原因那一行的链接（render_attribution 已经把 #n 换成了链接；候选新闻本身没有入库）。"""
    out: dict[int, list[tuple[str, str]]] = {}
    for line in (text or "").splitlines():
        m = _CAUSE_LINE.match(line)
        if not m or "来源：" not in line:
            continue
        tail = line.split("来源：", 1)[1]
        out[int(m.group(1))] = [(t, u) for t, u in _MD_LINK.findall(tail)]
    return out


def parse_attribution(row: dict) -> dict[str, Any]:
    """归因：{summary, driver, causes[Cause], watch, same_wave_as, text, structured}。"""
    data = _loads(row.get("attribution_json"))
    text = row.get("attribution_text") or ""
    out: dict[str, Any] = {"summary": None, "driver": None, "causes": [], "watch": None, "same_wave_as": None,
                           "text": text, "structured": False}
    if isinstance(data, dict):
        lead = data.get("same_wave_as")
        try:
            out["same_wave_as"] = int(lead) if lead is not None else None
        except (TypeError, ValueError):
            out["same_wave_as"] = None
        out["summary"] = str(data.get("summary") or "").strip() or None
        driver = str(data.get("driver") or "").strip()
        out["driver"] = driver or None
        out["watch"] = str(data.get("watch") or "").strip() or None
        links = _text_links_by_cause(text)
        causes = data.get("causes") if isinstance(data.get("causes"), list) else []
        for i, c in enumerate(causes, 1):
            if not isinstance(c, dict):
                continue
            cause = Cause(text=str(c.get("cause") or "").strip(), confidence=str(c.get("confidence") or ""),
                          timing=str(c.get("timing") or "").strip(), links=list(links.get(i, [])))
            seen = {u for _, u in cause.links}
            for ev in c.get("evidence") or []:
                ev = str(ev).strip()
                if not ev:
                    continue
                if ev.startswith(("http://", "https://")):
                    if ev not in seen:
                        cause.links.append(("检索结果", ev))
                        seen.add(ev)
                elif ev.startswith("日程"):
                    cause.notes.append(ev.replace(":", "：", 1))
                elif re.fullmatch(r"#?\d{1,3}", ev) and not links.get(i):
                    cause.notes.append(f"候选新闻 #{ev.lstrip('#')}")
            out["causes"].append(cause)
        out["structured"] = bool(out["summary"] or out["causes"])
    if out["summary"] is None and text.strip():
        first = next((ln for ln in text.splitlines() if ln.strip()), "")
        out["summary"] = re.sub(r"^\s*结论\s*[：:]\s*", "", first.replace("**", "")).strip() or None
    return out


def _safe_alert(row: dict) -> Alert | None:
    try:
        return Storage._row_to_alert(row)  # type: ignore[arg-type]
    except Exception:
        return None


def to_view(row: dict, settings: Settings) -> dict[str, Any]:
    """一行 → 模板用的字段。坏数据退回 broken 的最小版本。"""
    u = settings.universe
    sym = str(row.get("symbol") or "?")
    ts = _parse_ts(row.get("ts"))
    seg = row.get("segment") or "regular"
    base: dict[str, Any] = {
        "id": row.get("id"), "symbol": sym, "display": u.display(sym), "name": symbol_name(settings, sym),
        "kind": symbol_kind(settings, sym), "ts": ts, "segment": seg, "seg_name": segment_name(seg, sym),
        "seg_badge": SEGMENT_BADGES.get(seg, ""), "label": (row.get("label") or "").strip() or None,
        "notes": (row.get("notes") or "").strip() or None, "delivered": bool(row.get("message_id")),
        "broken": False,
    }
    attr = parse_attribution(row)
    base.update(summary=attr["summary"], driver=attr["driver"], same_wave_as=attr["same_wave_as"],
                driver_badge=DRIVER_BADGES.get(attr["driver"] or "", "badge-outline"))
    alert = _safe_alert(row)
    if alert is None or ts is None:
        direction = row.get("direction") or 0
        base.update(broken=True, alert=None, rule=row.get("rule") or "?", rule_name=RULE_NAMES.get(row.get("rule"), "?"),
                    direction=direction, headline="（记录不完整，无法还原触发条件）", describes=[],
                    move_pct=row.get("move_pct"), day_change_pct=None, ref_label="较昨收", price_text="—",
                    bench=row.get("benchmark"), bench_display=u.display(row["benchmark"]) if row.get("benchmark") else "",
                    beta=row.get("beta"), sector_pct=None, idio_pct=row.get("abnormal_move_pct"), judgment=None,
                    volume_ratio=row.get("volume_ratio"), peers=[], is_escalation=bool(row.get("is_escalation")),
                    window_minutes=row.get("window_minutes"))
        return base
    head = alert.headline
    ref = alert.ref_label
    bench = alert_benchmark(alert, settings)
    sector = (alert.beta * alert.benchmark_move_pct
              if alert.beta is not None and alert.benchmark_move_pct is not None else None)
    peers = []
    for s, v in (alert.peers_move_pct or {}).items():
        try:
            peers.append({"symbol": s, "display": u.display(s), "label": u.label(s), "pct": float(v)})
        except (TypeError, ValueError):
            continue
    if alert.symbol.startswith(NIGHT_PREFIX):
        price_text = f"篮子 {alert.price:,.2f}" if alert.price is not None else "—"
    else:
        price_text = fmt_price(alert.symbol, alert.price) if alert.price is not None else "—"
    base.update(
        alert=alert, rule=head.rule, rule_name=RULE_NAMES.get(head.rule, head.rule), direction=alert.direction,
        headline=head.short(ref), describes=[t.describe(ref) for t in alert.triggers], move_pct=head.move_pct,
        day_change_pct=alert.day_change_pct, ref_label=ref, price_text=price_text,
        bench=bench, bench_display=u.display(bench) if bench else "", beta=alert.beta,
        bench_move_pct=alert.benchmark_move_pct, sector_pct=sector, idio_pct=alert.abnormal_move_pct,
        raw_pct=alert.move_pct, judgment=sector_judgment(alert), volume_ratio=alert.volume_ratio, peers=peers,
        is_escalation=alert.is_escalation, window_minutes=alert.window_minutes,
        window_label=f"{alert.window_minutes}分钟" if alert.window_minutes else ref,
    )
    return base


def split_bar(v: dict[str, Any]) -> dict[str, float] | None:
    """列表里拆分的小条：板块 / 个股各占多少（按绝对值）。"""
    s, i = v.get("sector_pct"), v.get("idio_pct")
    if s is None or i is None:
        return None
    total = abs(s) + abs(i)
    if total < 1e-9:
        return None
    return {"sector": round(100 * abs(s) / total, 1), "idio": round(100 * abs(i) / total, 1)}


def group_by_day(views: list[dict], tz) -> list[dict[str, Any]]:
    """按本地日期分组（views 已按时间倒序）：[{day, title, rows}]。"""
    groups: list[dict[str, Any]] = []
    for v in views:
        ts = v.get("ts")
        day = ts.astimezone(tz).date() if ts else None
        if not groups or groups[-1]["day"] != day:
            title = f"{fmt_local(ts, tz, '%Y-%m-%d')} {weekday_cn(ts, tz)}" if ts else "时间未知"
            groups.append({"day": day, "title": title, "rows": []})
        groups[-1]["rows"].append(v)
    return groups


def summarize(rows: list[dict], settings: Settings) -> dict[str, Any]:
    """总数、按代码、按时段的条数（列表上方的一行）。"""
    by_sym: dict[str, int] = {}
    by_seg: dict[str, int] = {}
    up = down = 0
    for r in rows:
        by_sym[r.get("symbol") or "?"] = by_sym.get(r.get("symbol") or "?", 0) + 1
        seg = r.get("segment") or "regular"
        by_seg[seg] = by_seg.get(seg, 0) + 1
        if (r.get("direction") or 0) > 0:
            up += 1
        elif (r.get("direction") or 0) < 0:
            down += 1
    u = settings.universe
    return {
        "total": len(rows), "up": up, "down": down,
        "symbols": [{"symbol": s, "display": u.display(s), "count": n}
                    for s, n in sorted(by_sym.items(), key=lambda kv: (-kv[1], kv[0]))],
        "segments": [{"seg": s, "name": SEGMENT_NAMES.get(s, s), "count": by_seg[s]}
                     for s, _ in SEGMENTS if s in by_seg],
    }


def symbol_options(rows: list[dict], settings: Settings, selected: str = "") -> list[dict[str, str]]:
    """代码下拉框：数据里出现过的代码，持仓在前。"""
    u = settings.universe
    seen = list(dict.fromkeys(r.get("symbol") for r in rows if r.get("symbol")))
    if selected and selected not in seen:
        seen.append(selected)
    holdings = set(u.symbols)
    seen.sort(key=lambda s: (s not in holdings, is_pseudo(s), s))
    return [{"value": s, "text": u.display(s) + (f" · {n}" if (n := symbol_name(settings, s)) else "")} for s in seen]


# ───────── CSV ─────────

CSV_HEADER = ["id", "本地时间", "代码", "名称", "时段", "规则", "方向", "涨跌%", "较参照%", "参照", "基准", "β", "板块部分%",
              "个股特异%", "成交量倍数", "归因结论", "主导", "标注", "备注"]


def _cell(v: Any) -> str:
    """文字单元格：防 Excel 把 =、+、-、@ 开头的文字当公式（数字列不经过这里）。"""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _num(v: Any, digits: int = 2) -> str:
    try:
        return f"{float(v):.{digits}f}" if v is not None else ""
    except (TypeError, ValueError):
        return ""


def to_csv(views: list[dict], settings: Settings) -> str:
    """UTF-8 带 BOM（Excel 直接打开不乱码）。"""
    tz = settings.tz
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(CSV_HEADER)
    for v in views:
        ts = v.get("ts")
        w.writerow([
            v.get("id"), fmt_local(ts, tz, "%Y-%m-%d %H:%M") + f" {tz_abbr(ts, tz)}" if ts else "",
            _cell(v["display"]), _cell(v.get("name") or ""), v["seg_name"], v.get("rule_name") or "",
            "涨" if (v.get("direction") or 0) > 0 else "跌" if (v.get("direction") or 0) < 0 else "",
            _num(v.get("move_pct")), _num(v.get("day_change_pct")), v.get("ref_label") or "",
            _cell(v.get("bench_display") or ""), _num(v.get("beta")), _num(v.get("sector_pct")), _num(v.get("idio_pct")),
            _num(v.get("volume_ratio"), 1), _cell(v.get("summary") or ""), v.get("driver") or "",
            _cell(v.get("label") or ""), _cell(v.get("notes") or ""),
        ])
    return "﻿" + buf.getvalue()


# ───────── 宏观快照（alerts.macro_json，见 MacroSnapshot.to_json） ─────────

MACRO_GROUP_ORDER = ["利率", "大宗商品", "股指", "其他"]


def parse_macro(raw: Any, settings: Settings) -> dict[str, Any] | None:
    """{at, groups: [(组名, [条目])], themes: [(主题名, [条目])], curve: [(名称, 文字)]}；没有或读不通时 None。
    条目：{name, value, change, change_class, change_label, change_1h}。名称、类型按现在的配置查（JSON 里只存了数值）。"""
    data = _loads(raw)
    if not isinstance(data, dict):
        return None
    base = {i.key: i for i in settings.macro.indicators}
    themed = {f"{th.key}.{i.key}": (th, i) for th in settings.universe.themes.values() for i in th.indicators}
    groups: dict[str, list] = {}
    themes: dict[str, list] = {}
    for key, d in data.items():
        if key in ("at", "treasury") or not isinstance(d, dict):
            continue
        try:
            value = float(d["value"])
        except (KeyError, TypeError, ValueError):
            continue
        th = None
        if key in themed:
            th, ind = themed[key]
        else:
            ind = base.get(key)
        off = bool(d.get("off_hours"))
        name = (ind.off_hours_name if off and ind is not None and ind.off_hours_name else
                ind.name if ind is not None else key.split(".")[-1])
        kind = ind.kind if ind is not None else "level"
        group = ind.group if ind is not None else "其他"

        def num(x: Any) -> float | None:
            try:
                return float(x) if x is not None else None
            except (TypeError, ValueError):
                return None

        q = MacroQuote(key=key, name=name, symbol=str(d.get("symbol") or ""), kind=kind, group=group, value=value,
                       ts=_parse_ts(d.get("ts")) or datetime.now(UTC), change=num(d.get("change")),
                       change_label=str(d.get("change_label") or "较昨收"), change_1h=num(d.get("change_1h")),
                       off_hours=off)
        item = {"name": q.name, "symbol": q.symbol, "value": q.fmt_value(), "change": q.fmt_change(q.change),
                "change_class": "flat" if q.change is None else ("up" if q.change > 0 else "down" if q.change < 0 else "flat"),
                "change_label": "较美股收盘" if off else q.change_label,
                "change_1h": q.fmt_change(q.change_1h) if q.change_1h is not None else None}
        if th is not None:
            themes.setdefault(th.name, []).append(item)
        else:
            groups.setdefault(group, []).append(item)
    order = MACRO_GROUP_ORDER + [g for g in groups if g not in MACRO_GROUP_ORDER]
    curve = []
    tr = data.get("treasury")
    if isinstance(tr, dict):
        for k in ("2Y", "10Y", "30Y"):
            try:
                curve.append((k, f"{float(tr[k]):.2f}%"))
            except (KeyError, TypeError, ValueError):
                pass
        try:
            curve.append(("2s10s", f"{(float(tr['10Y']) - float(tr['2Y'])) * 100:+.0f}bp"))
        except (KeyError, TypeError, ValueError):
            pass
    out = {"at": _parse_ts(data.get("at")), "groups": [(g, groups[g]) for g in order if g in groups],
           "themes": list(themes.items()), "curve": curve, "curve_day": tr.get("day") if isinstance(tr, dict) else None}
    return out if out["groups"] or out["themes"] or out["curve"] else None


# ───────── 前后日程（与 agents/attribution.Attributor.nearby_events 同一口径） ─────────


class _EventsView:
    """给 Attributor.nearby_events 用的最小接口：有 EventHub 就用它（只列监控范围内的），否则直接读库。"""

    def __init__(self, ctx: Any):
        self.ctx = ctx

    def upcoming(self, start, end) -> list[Event]:
        if self.ctx.events is not None:
            return self.ctx.events.upcoming(start, end)
        from tickersentinel.events.hub import event_in_scope

        return [e for e in self.ctx.storage.events_between(start, end) if event_in_scope(self.ctx.settings, e)]


def nearby_events(ctx: Any, alert: Alert) -> tuple[list[dict], list[dict]]:
    """异动前 24 小时内、之后 24 小时内的日程：([{when, rel, title, who, confirmed, url, kind}], [...])。"""
    from types import SimpleNamespace

    from tickersentinel.agents.attribution import Attributor

    try:
        before, ahead = Attributor.nearby_events(SimpleNamespace(events=_EventsView(ctx)), alert)  # type: ignore[arg-type]
    except Exception:
        return [], []
    # 归因把异动后 5 分钟内的日程也算“已发生”（留一点时间差的余量）；页面上按实际先后分开，
    # 否则“异动后 3 分钟”会出现在“异动前”那一栏里（按分钟取整后仍在异动之后的才挪）
    late = [e for e in before if e.at is not None and round((e.at - alert.ts).total_seconds() / 60) > 0]
    if late:
        before = [e for e in before if e not in late]
        ahead = sorted(late, key=lambda e: e.at) + list(ahead)
    tz = ctx.settings.tz
    u = ctx.settings.universe

    def row(e: Event) -> dict[str, Any]:
        if e.at is not None:
            when = f"{fmt_local(e.at, tz, '%m-%d %H:%M')} {tz_abbr(e.at, tz)}"
            rel = rel_text(alert.ts, e.at)
        else:
            when = f"{e.day:%m-%d}" + (f" {e.time_hint}" if e.time_hint else "")
            gap = (e.day - alert.ts.astimezone(ET).date()).days
            rel = {-1: "前一天", 0: "当天", 1: "次日"}.get(gap, f"{e.day:%m-%d}") + "（时间未定）"
        who = "" if e.kind == "macro" else u.display(e.symbol)
        return {"when": when, "rel": rel, "title": e.title, "who": who, "holding": e.symbol in u.symbols,
                "confirmed": e.confirmed, "url": e.url if (e.url or "").startswith(("http://", "https://")) else "",
                "kind": e.kind}

    return [row(e) for e in before], [row(e) for e in ahead]


def rel_text(alert_ts: datetime, t: datetime) -> str:
    """“异动前 12 分钟” / “异动后 3.5 小时” / “与异动同时”。"""
    d = round((t - alert_ts).total_seconds() / 60)
    if d == 0:
        return "与异动同时"
    side = "前" if d < 0 else "后"
    d = abs(d)
    span = f"{d} 分钟" if d < 90 else f"{d / 60:.1f} 小时".replace(".0 ", " ")
    return f"异动{side} {span}"


def et_time(ts: datetime) -> str:
    return f"{fmt_local(ts, ET, '%H:%M')} {tz_abbr(ts, ET)}"
