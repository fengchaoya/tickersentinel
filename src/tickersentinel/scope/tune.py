"""调警报阈值（tsen scope tune / 飞书 /scope tune）：按这只资产最近的波动给建议（也可以直接给数字），
回放对比“原来 / 改后”各推多少条，确认后写入，可以撤销（同 scope/edit.py 的备份）。

可以调的对象，以及写到哪里：
- 持仓、assets.yaml 里的资产 → assets.yaml 里那一项的 alert；
- 夜间篮子 → 主题文件的 overnight_alert。可以用篮子的伪代码、篮子的任何一个成分（成分平时只在相对篮子单独大动时推送，
  整体的涨跌由篮子触发）或主题名指定；
- 没有组成篮子的单只夜间信号 → 主题文件 overnight 里那一项的 alert。
不给数字时按日波动建议（规则同 scope/discover.py）：类别默认值 × 日波动 / 这组默认值针对的日波动（ref_sigma_pct：
美股、海外股票、加密货币 5%，期货 2%）——各资产用同样的“几倍波动”。纯计算 + 回放，不需要大模型。
给数字时只改给出的那几项：“急涨 60:4”只改 60 分钟那一档（其他窗口不变），“60:0”去掉这一档。
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tickersentinel.config import NIGHT_PREFIX, Settings
from tickersentinel.detect.basket import basket_daily
from tickersentinel.scope.discover import (
    ScopeError,
    class_rules,
    daily_sigma,
    log_returns,
    suggest_day,
    suggest_rapid,
)
from tickersentinel.scope.edit import Change, ScopeEditError, _yaml, replay_preview, settings_after

if TYPE_CHECKING:
    from tickersentinel.sources.base import PriceSource


@dataclass
class TunePlan:
    target: str  # 代码；夜间篮子为伪代码 night:<主题>:<交易所>
    label: str
    where: str  # assets / theme_basket / theme_overnight
    theme_key: str | None
    sigma_pct: float | None
    old: dict[str, Any]  # {"rapid_pct": {...}, "day_pct": [...]}
    new: dict[str, Any]
    basis: str  # 按日波动建议 / 按你给的数字
    change: Change | None = None
    destination: str = ""  # 写到哪个文件的哪一项
    notes: list[str] = field(default_factory=list)
    replay: str = ""

    @property
    def unchanged(self) -> bool:
        return self.old == self.new


def resolve_target(settings: Settings, text: str) -> tuple[str, str, str | None, list[str]]:
    """(代码, 写到哪, 主题, 说明)。"""
    u = settings.universe
    t = (text or "").strip()
    if not t:
        raise ScopeError("需要一个代码，例如 MU；夜间篮子可以写主题名（memory）或篮子里任何一个成分（000660.KS）")
    notes: list[str] = []
    if t in u.themes:  # 主题名 → 它的夜间篮子
        codes = [c for c, (th, _, _) in u.night_baskets.items() if th.key == t]
        if len(codes) != 1:
            raise ScopeError(f"主题 {t} 有 {len(codes)} 个夜间篮子，请写篮子的成分代码来指定")
        return codes[0], "theme_basket", t, notes
    if t.lower().startswith(NIGHT_PREFIX):
        if t not in u.night_baskets:
            raise ScopeError(f"没有夜间篮子 {t}（tsen scope 查看现在的范围）")
        return t, "theme_basket", u.night_baskets[t][0].key, notes
    hit = next((c for c in u.night_baskets if u.label(c) == t or t in u.label(c)), None)
    if hit:
        return hit, "theme_basket", u.night_baskets[hit][0].key, notes
    inst = u.find(t) or u.find(t.upper())
    if inst is None:
        raise ScopeError(f"{t} 不在监控范围里（tsen scope 查看现在的范围）")
    if nb := u.night_basket_of(inst.symbol):  # 篮子的成分只跑“个股特异”，它自己的急涨急跌 / 全天阈值不起作用：调篮子
        th = u.night_baskets[nb][0]
        notes.append(f"{inst.symbol} 是「{u.label(nb)}」的成分：整体的涨跌由篮子触发，所以调的是篮子的阈值；"
                     f"成分单独大动的门槛是主题 {th.key} 的 overnight_single_pct（现在 {th.overnight_single_pct:g}%）")
        return nb, "theme_basket", th.key, notes
    if any(i.symbol == inst.symbol for i in u.watchlist):
        return inst.symbol, "assets", None, notes
    if inst.origin == "theme" and inst.themes:
        return inst.symbol, "theme_overnight", inst.themes[0], notes
    raise ScopeError(f"{inst.symbol} 的阈值不知道该写到哪里")


def _flow(d: dict | list):
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    if isinstance(d, dict):
        m = CommentedMap()
        for k, v in d.items():
            m[k] = _flow(v) if isinstance(v, (dict, list)) else v
        m.fa.set_flow_style()
        return m
    s = CommentedSeq(d)
    s.fa.set_flow_style()
    return s


def _num(x: float) -> float | int:
    return int(x) if float(x) == int(x) else float(x)


def _alert_map(old: dict | None, delta: dict[str, Any]) -> dict:
    """在原来的 alert 上只改变了的那几项（rapid_pct / day_pct），其他项（z、hours、没改的阈值）原样保留——
    没改的不写死，以后调整类别默认时它还能跟着变。旧名 levels_pct 改写成 day_pct。"""
    out = dict(old or {})
    if "levels_pct" in out:
        out["day_pct"] = out.pop("levels_pct")
    if delta.get("rapid_pct") is not None:
        out["rapid_pct"] = {int(w): _num(v) for w, v in delta["rapid_pct"].items()}
    if delta.get("day_pct") is not None:
        out["day_pct"] = [_num(v) for v in delta["day_pct"]]
    return out


def _dump(y, data) -> str:
    buf = io.StringIO()
    y.dump(data, buf)
    return buf.getvalue()


def plan_change(settings: Settings, plan: TunePlan) -> Change:
    _, assets_path, themes_dir = settings.paths
    y = _yaml()
    delta = {k: v for k, v in plan.new.items() if v != plan.old.get(k)}
    if plan.where == "assets":
        data = y.load(assets_path.read_text(encoding="utf-8"))
        item = next((i for i in data.get("assets") or [] if str(i.get("symbol")) == plan.target), None)
        if item is None:
            raise ScopeEditError(f"assets.yaml 里没找到 {plan.target}")
        item["alert"] = _flow(_alert_map(dict(item.get("alert") or {}), delta))
        files = {str(assets_path): _dump(y, data)}
        where = f"config/assets.yaml 里 {plan.target} 那一项的 alert"
    else:
        path = Path(themes_dir) / f"{plan.theme_key}.yaml"
        data = y.load(path.read_text(encoding="utf-8"))
        if plan.where == "theme_basket":
            data["overnight_alert"] = _flow(_alert_map(dict(data.get("overnight_alert") or {}), delta))
            where = f"config/themes/{path.name} 的 overnight_alert"
        else:
            from ruamel.yaml.comments import CommentedMap

            ov = data.get("overnight")
            if isinstance(ov, list):  # 列表写法：换成 {代码: 名称}，顺序不变
                ov = CommentedMap((str(c), str(c)) for c in ov)
                data["overnight"] = ov
            elif ov is None:
                ov = data["overnight"] = CommentedMap()
            cur = ov.get(plan.target)
            if isinstance(cur, dict):  # 已经是 {name, alert}：原地改 alert，这一项的注释和格式保留
                cur["alert"] = _flow(_alert_map(dict(cur.get("alert") or {}), delta))
            else:
                ov[plan.target] = _flow({"name": cur or plan.target, "alert": _alert_map({}, delta)})
            where = f"config/themes/{path.name} 里 overnight 的 {plan.target}"
        files = {str(path): _dump(y, data)}
    plan.destination = where
    return Change("tune", plan.target, f"调整 {plan.label} 的警报标准", files)


async def propose_tune(settings: Settings, text: str, prices: "PriceSource | None", *,
                       day: list[float] | None = None, rapid: dict[int, float] | None = None,
                       replay_days: int = 10) -> TunePlan:
    u = settings.universe
    code, where, theme_key, notes = resolve_target(settings, text)
    inst = u.find(code)
    cls = inst.asset_class if inst is not None else "us_equity"
    rules = settings.rules_for(code)
    old = {"rapid_pct": dict(rules.min_move_pct), "day_pct": list(rules.day_pct)}
    sigma = None
    if prices is not None:
        members = u.basket_members(code) if code.startswith(NIGHT_PREFIX) else [code]
        try:
            daily = await prices.daily_bars(members, lookback_days=200)
            if code.startswith(NIGHT_PREFIX):
                b = basket_daily(daily, members)
                daily = {code: b} if b is not None else {}
            r = log_returns(daily)
            sigma = daily_sigma(r[code]) if code in r else None
        except Exception as exc:
            notes.append(f"日线取不到，没法按波动给建议（{type(exc).__name__}）")
    d_rapid, d_day, ref = class_rules(settings, "foreign_equity" if code.startswith(NIGHT_PREFIX) else cls)
    if day is None and rapid is None:
        if sigma is None:
            raise ScopeError("没有日波动数据，给不出建议；可以直接写数字，例如 /scope tune MU 全天 5 8 12 急涨 60:3")
        new = {"rapid_pct": suggest_rapid(sigma, d_rapid, ref), "day_pct": suggest_day(sigma, d_day, ref)}
        basis = f"按日波动 σ≈{sigma:.1f}% 建议"
    else:
        # 只改给出的窗口，其他窗口保持不变；写 0 去掉这一档
        merged = {**old["rapid_pct"], **{int(w): float(v) for w, v in (rapid or {}).items()}}
        merged = {w: v for w, v in sorted(merged.items()) if v > 0}
        if not merged:
            raise ScopeError("急涨急跌至少要留一档")
        new = {"rapid_pct": merged, "day_pct": sorted(float(x) for x in (day or old["day_pct"]))}
        basis = "按你给的数字"
    plan = TunePlan(code, u.label(code) if u.label(code) != code else code, where, theme_key, sigma, old, new, basis,
                    notes=notes)
    if plan.unchanged:
        return plan
    plan.change = plan_change(settings, plan)
    if prices is not None and replay_days > 0:
        try:
            after = settings_after(settings, plan.change)
            before = await replay_preview(settings, prices, [code], replay_days)
            now = await replay_preview(after, prices, [code], replay_days)
            plan.replay = f"原来：{_count(before)} 条 → 改后：{_count(now)} 条（回放最近 {replay_days} 个交易日）"
        except Exception as exc:
            plan.notes.append(f"回放没做成（{type(exc).__name__}: {exc}）")
    return plan


def _count(text: str) -> str:
    m = re.search(r"共 (\d+) 条", text)
    return m.group(1) if m else "?"


def _fmt(d: dict[str, Any]) -> tuple[str, str]:
    rapid = "、".join(f"{w}分钟 {v:g}%" for w, v in sorted(d["rapid_pct"].items()))
    day = "/".join(f"{x:g}" for x in d["day_pct"]) + "%"
    return rapid, day


def render_tune(plan: TunePlan) -> str:
    sig = f"，日波动 σ≈{plan.sigma_pct:.1f}%" if plan.sigma_pct and "σ" not in plan.basis else ""
    lines = [f"**调整 {plan.label} 的警报标准**（{plan.basis}{sig}）"]
    (orap, oday), (nrap, nday) = _fmt(plan.old), _fmt(plan.new)
    if plan.unchanged:
        lines.append(f"- 现在的标准已经是这样了（急涨急跌 {orap}；全天 {oday}），不用改")
    else:
        lines.append(f"- 急涨急跌：{orap} → {nrap}" if orap != nrap else f"- 急涨急跌：{orap}（不变）")
        lines.append(f"- 全天：{oday} → {nday}" if oday != nday else f"- 全天：{oday}（不变）")
        if plan.sigma_pct:
            lo = plan.new["day_pct"][0] / plan.sigma_pct
            lines.append(f"- 改后全天最低一档 ≈ {lo:.1f}σ")
        if plan.replay:
            lines.append(f"- {plan.replay}")
        if plan.destination:
            lines.append(f"- 写入：{plan.destination}")
    lines += [f"- 注意：{n}" for n in plan.notes]
    return "\n".join(lines)


def parse_numbers(text: str) -> tuple[list[float] | None, dict[int, float] | None]:
    """“全天 5 8 12 急涨 5:2 60:3” → ([5, 8, 12], {5: 2, 60: 3})。只写数字时当全天的档位（旧写法“关口”也认）。
    急涨急跌只改写出来的窗口，其他窗口不变；“60:0”去掉这一档。"""
    t = text.replace("，", " ").replace(",", " ").replace("%", " ")
    rapid = {int(w): float(v) for w, v in re.findall(r"(\d+)\s*[:：]\s*(\d+(?:\.\d+)?)", t)} or None
    t2 = re.sub(r"\d+\s*[:：]\s*\d+(?:\.\d+)?", " ", t)
    nums = [float(x) for x in re.findall(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])", t2)]
    return (sorted(nums) if nums else None), rapid


__all__ = ["TunePlan", "propose_tune", "render_tune", "resolve_target", "parse_numbers", "plan_change"]
