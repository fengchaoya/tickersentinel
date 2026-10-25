"""把方案写进配置（tsen scope add / remove / undo，飞书 /scope）。

- 写之前把要改的文件备份到 data/scope_history/<时间>-<动作>-<代码>/（data/ 不进 git），写完立刻重新读一遍配置，
  读不通就自动还原；undo 还原最近一次（新建的主题文件删掉，改过的文件换回原样）。
- assets.yaml 用 ruamel.yaml 读写（保留注释和格式；原样读写一字不差，只多出新加的那一项）；移除持仓时只删那一项自己的行，
  前后的注释不动。新主题写成 config/themes/<主题>.yaml，开头注明是哪天根据什么数据生成的。
- 卖掉某个主题的最后一只持仓时，主题文件留着（不生效，以后再买可以直接用）。
"""

from __future__ import annotations

import io
import json
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from tickersentinel.config import DEFAULT_THEME, Settings, exchange_of, is_pseudo, is_us_symbol, load_settings
from tickersentinel.scope.discover import MARKET, PEER_WINDOW, Proposal, short_name
from tickersentinel.timeutil import utcnow

HISTORY_DIR = "scope_history"
MAX_HISTORY = 30
class ScopeEditError(Exception):
    pass


@dataclass
class Change:
    action: str  # add / remove
    symbol: str
    summary: str
    files: dict[str, str]  # 路径 → 写入后的完整内容
    created: list[str] = field(default_factory=list)  # 新建的文件（撤销时删掉）

    def describe(self) -> str:
        lines = [self.summary]
        for path in self.files:
            p = Path(path)
            lines.append(f"- {'新建' if path in self.created else '修改'} {p.parent.name}/{p.name}")
        return "\n".join(lines)


def _num(x: float) -> float | int:
    return int(x) if float(x) == int(x) else float(x)


def _q(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False)


def _flow(d: dict[str, list[str]]) -> str:
    return "{" + ", ".join(f"{k}: [{', '.join(_q(v) for v in vs)}]" for k, vs in d.items()) + "}"


def _flow_alert(alert: dict[str, Any]) -> str:
    """{"rapid_pct": {5: 1.9, ...}, "day_pct": [6, 10]} → YAML 行内写法。"""
    parts = []
    if alert.get("rapid_pct"):
        parts.append("rapid_pct: {" + ", ".join(f"{k}: {v:g}" for k, v in sorted(alert["rapid_pct"].items())) + "}")
    if alert.get("day_pct"):
        parts.append("day_pct: [" + ", ".join(f"{v:g}" for v in alert["day_pct"]) + "]")
    return "{" + ", ".join(parts) + "}"


def theme_yaml(p: Proposal, today: str) -> str:
    """新主题文件的内容（带上实测数字作为注释，以后重新校准时有参照）。"""
    out = [f"# 主题「{p.theme_name}」：{today} 由 tsen scope 在新增持仓 {p.symbol} 时根据近一年日线生成，可以手工修改。",
           "# 字段说明见 memory.yaml 的开头。下面行尾的数字是生成时的实测值（会随行情阶段变化）："]
    if p.bench_scores:
        top = "、".join(f"{k} {v:.2f}" for k, v in sorted(p.bench_scores.items(), key=lambda kv: -kv[1])[:5])
        out.append(f"#   板块基准 R²：{top}")
    out += ["", f"name: {_q(p.theme_name)}"]
    if p.focus:
        out.append(f"focus: {_q(p.focus)}")
    out.append(f"benchmark: {p.benchmark}" + ("                # 下面 peers 里美股的等权篮子" if p.benchmark == "basket" else ""))
    peers = p.chosen("peers")
    if peers:
        out += ["", f"peers:                         # 剔除大盘（{MARKET}）后的残差相关，近 {PEER_WINDOW} 个交易日"]
        out += [f"  {_q(x.symbol)}: {_q(x.name or x.symbol)}" + (f"   # {x.score:.2f}" if x.score is not None else "") for x in peers]
    night = p.chosen("overnight")
    if night:
        out += ["", f"overnight:                      # 夜间信号：当天涨跌与 {p.symbol} 次日开盘跳空的偏相关（剔除前一天涨跌）"]
        # 同一交易所 2 只以上会合成篮子触发（成分只跑“个股特异”）：阈值写给篮子（overnight_alert），单独成组的才写自己的 alert
        by_ex = Counter(exchange_of(x.symbol) for x in night)
        pooled = bool(p.overnight_alert) and any(n >= 2 for ex, n in by_ex.items() if ex)
        for x in night:
            if p.overnight_alert and by_ex.get(exchange_of(x.symbol), 0) < 2:
                out.append(f"  {_q(x.symbol)}: {{name: {_q(x.name or x.symbol)}, alert: {_flow_alert(p.overnight_alert)}}}"
                           f"   # {x.score:.2f}，{x.n} 天")
            else:
                out.append(f"  {_q(x.symbol)}: {_q(x.name or x.symbol)}   # {x.score:.2f}，{x.n} 天")
        if pooled:
            out.append(f"overnight_alert: {_flow_alert(p.overnight_alert)}   # 同一交易所的几只合成篮子触发：按它们的日波动换算")
    cats = p.chosen("catalysts")
    if cats:
        out += ["", "catalysts:                      # 大客户 / 供应商：只进事件日历和归因"]
        out += [f"  {_q(x.symbol)}: {_q(x.name or x.symbol)}" + (f"   # {x.note[:40]}" if x.note else "") for x in cats]
    if p.news:
        out += ["", "news:"] + [f"  {k}: [{', '.join(_q(v) for v in vs)}]" for k, vs in p.news.items()]
    if p.indicators:
        out += ["", "indicators:"]
        for i in p.indicators:
            out.append("  - {" + ", ".join(f"{k}: {_q(v) if isinstance(v, str) else v}" for k, v in i.items()) + "}")
    return "\n".join(out) + "\n"


def _yaml():
    from ruamel.yaml import YAML

    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def add_asset_text(text: str, entry: dict[str, Any], comment: str) -> str:
    """在 assets.yaml 的 assets 列表末尾加一项（保留原有注释和格式）。"""
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    y = _yaml()
    data = y.load(text) if text.strip() else None
    if data is None:
        data = CommentedMap()
    if "assets" not in data or data["assets"] is None:
        data["assets"] = CommentedSeq()
    seq = data["assets"]
    item = CommentedMap()
    for k, v in entry.items():
        if isinstance(v, dict):
            m = CommentedMap()
            for kk, vv in v.items():
                if isinstance(vv, list):
                    sq = CommentedSeq(vv)
                    sq.fa.set_flow_style()
                    m[kk] = sq
                elif isinstance(vv, dict):
                    mm = CommentedMap(vv)
                    mm.fa.set_flow_style()
                    m[kk] = mm
                else:
                    m[kk] = vv
            m.fa.set_flow_style()
            item[k] = m
        else:
            item[k] = v
    seq.append(item)
    seq.yaml_set_comment_before_after_key(len(seq) - 1, before=comment, indent=2)
    buf = io.StringIO()
    y.dump(data, buf)
    return buf.getvalue()


def _entry_pattern(symbol: str) -> re.Pattern:
    """assets 列表里 symbol 那一项的起始行：块格式 “- symbol: X” 或单行 “- {symbol: X, …}”，代码可以带引号。"""
    return re.compile(r"^(?P<indent>[ \t]*)-[ \t]+(?:\{[ \t]*)?symbol:[ \t]*['\"]?" + re.escape(symbol)
                      + r"['\"]?[ \t]*(?:[,}#]|$)")


def remove_asset_text(text: str, symbol: str) -> str:
    """删掉 assets.yaml 里 symbol 那一项自己的行（缩进比“- ”深的续行），前后的注释不动；
    紧挨着它上面那行如果是 tsen scope 加的标记注释，也一起删。"""
    lines = text.splitlines(keepends=True)
    pat = _entry_pattern(symbol)
    start = next((i for i, ln in enumerate(lines) if pat.match(ln)), None)
    if start is None:
        raise ScopeEditError(f"assets.yaml 里没找到 {symbol}")
    col = len(pat.match(lines[start]).group("indent"))
    end = start + 1
    while end < len(lines):
        ln = lines[end]
        if not ln.strip():
            break
        indent = len(ln) - len(ln.lstrip(" \t"))
        if indent <= col:
            break
        end += 1
    if start > 0 and "由 tsen scope 加入" in lines[start - 1] and lines[start - 1].lstrip().startswith("#"):
        start -= 1
    return "".join(lines[:start] + lines[end:])


class ScopeEditor:
    def __init__(self, settings: Settings, data_dir: Path | None = None):
        self.settings = settings
        self.cfg_path, self.assets_path, self.themes_dir = settings.paths
        self.data_dir = Path(data_dir) if data_dir else settings.resolve_path(settings.storage.sqlite_path).parent
        self.history_dir = self.data_dir / HISTORY_DIR

    # ───────── 生成改动 ─────────

    def plan_add(self, p: Proposal) -> Change:
        u = self.settings.universe
        if p.symbol in u.symbols:
            raise ScopeEditError(f"{p.symbol} 已经是持仓")
        today = utcnow().astimezone(self.settings.tz).strftime("%Y-%m-%d")
        files: dict[str, str] = {}
        created: list[str] = []
        if p.new_theme:
            path = self.themes_dir / f"{p.theme_key}.yaml"
            if path.exists():
                raise ScopeEditError(f"主题文件 {path.name} 已经存在：换个主题名，或用 --theme {p.theme_key} 并入它")
            files[str(path)] = theme_yaml(p, today)
            created.append(str(path))
        entry: dict[str, Any] = {"symbol": p.symbol, "name": short_name(p.name), "class": p.asset_class}
        if p.theme_key and p.theme_key != DEFAULT_THEME:
            entry["theme"] = p.theme_key
        if p.queries:
            entry["queries"] = p.queries
        alert: dict[str, Any] = {}
        rules = self.settings.rules_for(p.symbol)
        if p.rapid and dict(p.rapid) != dict(rules.min_move_pct):
            alert["rapid_pct"] = {int(w): _num(v) for w, v in p.rapid.items()}
        if p.day_pct and list(p.day_pct) != list(rules.day_pct):
            alert["day_pct"] = [_num(x) for x in p.day_pct]
        if alert:
            entry["alert"] = alert
        text = self.assets_path.read_text(encoding="utf-8") if self.assets_path.exists() else "assets:\n"
        files[str(self.assets_path)] = add_asset_text(text, entry, f"{today} 由 tsen scope 加入（主题 {p.theme_key}）")
        where = (f"新建主题「{p.theme_name}」（{p.theme_key}.yaml）" if p.new_theme
                 else f"并入主题「{p.theme_name}」")
        return Change("add", p.symbol, f"新增持仓 {p.symbol}，{where}", files, created)

    def plan_remove(self, symbol: str) -> Change:
        u = self.settings.universe
        sym = symbol.strip().upper()
        inst = next((i for i in u.watchlist if i.symbol.upper() == sym), None)
        if inst is None:
            auto = u.find(sym)
            if auto is not None and auto.origin == "theme":
                raise ScopeEditError(f"{sym} 是主题「{u.theme_of(sym).name}」的夜间信号，不在 assets.yaml 里：要去掉就改主题文件的 overnight")
            raise ScopeEditError(f"{sym} 不在 assets.yaml 里（tsen scope 查看现在的范围）")
        text = self.assets_path.read_text(encoding="utf-8")
        new = remove_asset_text(text, inst.symbol)
        summary = f"移除 {inst.symbol}（{inst.name}）"
        for key in inst.themes:
            rest = [s for s in u.theme_holdings(key) if s != inst.symbol]
            if not rest and key in u.themes:
                summary += f"；主题「{u.themes[key].name}」不再有持仓，整体停用（文件保留，以后可以再用）"
        return Change("remove", inst.symbol, summary, {str(self.assets_path): new})

    # ───────── 写入 / 撤销 ─────────

    def apply(self, change: Change) -> Settings:
        """备份 → 写入 → 重新读配置验证；读不通就还原并报错。返回新的 Settings。"""
        stamp = utcnow().strftime("%Y%m%d-%H%M%S")
        entry = self.history_dir / f"{stamp}-{change.action}-{re.sub(r'[^A-Za-z0-9]+', '_', change.symbol)}"
        entry.mkdir(parents=True, exist_ok=True)
        backups: dict[str, str | None] = {}
        for i, path in enumerate(change.files):
            src = Path(path)
            if src.exists():
                dst = entry / f"{i}-{src.name}"
                shutil.copy2(src, dst)
                backups[path] = dst.name
            else:
                backups[path] = None
        manifest = {"action": change.action, "symbol": change.symbol, "summary": change.summary,
                    "at": utcnow().isoformat(), "backups": backups, "created": change.created}
        (entry / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            for path, content in change.files.items():
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_text(content, encoding="utf-8")
            new = self.reload()
        except Exception as exc:
            self._restore(entry)
            shutil.rmtree(entry, ignore_errors=True)
            raise ScopeEditError(f"写入后配置读不通，已还原：{type(exc).__name__}: {exc}") from exc
        # 记下写入后每个文件的指纹：撤销前核对，之后又被改过（手工编辑、git pull）就不整份覆盖
        manifest["written"] = {path: _digest(Path(path)) for path in change.files}
        (entry / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        self._prune()
        return new

    def reload(self) -> Settings:
        new = load_settings(self.cfg_path, load_env=False)
        new.secrets = self.settings.secrets
        return new

    def _restore(self, entry: Path) -> None:
        manifest = json.loads((entry / "manifest.json").read_text(encoding="utf-8"))
        for path, backup in manifest["backups"].items():
            if backup is None:
                Path(path).unlink(missing_ok=True)
            else:
                shutil.copy2(entry / backup, path)

    def history(self) -> list[dict[str, Any]]:
        if not self.history_dir.is_dir():
            return []
        out = []
        for d in sorted(self.history_dir.iterdir()):
            m = d / "manifest.json"
            if m.exists():
                out.append({**json.loads(m.read_text(encoding="utf-8")), "dir": str(d)})
        return out

    def undo(self, *, force: bool = False) -> tuple[str, Settings]:
        """撤销最近一次改动。返回 (说明, 新的 Settings)。撤销是整份还原备份：那次改动之后文件又被改过的话，
        之后的改动会一起丢掉，所以先核对指纹，不一致就拒绝（force=True 照样还原）。"""
        hist = self.history()
        if not hist:
            raise ScopeEditError("没有可以撤销的改动")
        last = hist[-1]
        entry = Path(last["dir"])
        if not force:
            written = last.get("written")
            if written is None:
                raise ScopeEditError(f"「{last['summary']}」是旧版本记下的，没有文件指纹，没法确认之后有没有再改过；"
                                     f"确定要整份还原就加 --force（备份在 {entry}）")
            changed = [p for p, d in written.items() if _digest(Path(p)) != d]
            if changed:
                raise ScopeEditError(f"「{last['summary']}」之后这些文件又被改过：{'、'.join(Path(p).name for p in changed)}；"
                                     f"撤销会把之后的改动一起丢掉。确定要还原就加 --force（备份在 {entry}）")
        self._restore(entry)
        try:
            new = self.reload()
        except Exception as exc:
            raise ScopeEditError(f"撤销后配置读不通：{type(exc).__name__}: {exc}（备份在 {entry}）") from exc
        shutil.rmtree(entry, ignore_errors=True)
        return f"已撤销：{last['summary']}", new

    def _prune(self) -> None:
        hist = self.history()
        for h in hist[:-MAX_HISTORY]:
            shutil.rmtree(h["dir"], ignore_errors=True)


def _digest(path: Path) -> str | None:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def settings_after(settings: Settings, change: Change) -> Settings:
    """改动生效后的配置（写在临时目录里，不碰正式文件）：预览、回放用。"""
    import tempfile

    from tickersentinel.config import load_from

    cfg, assets, themes = settings.paths
    tmp = Path(tempfile.mkdtemp(prefix="tsen-scope-"))
    try:
        shutil.copy2(cfg, tmp / "settings.yaml")
        if assets.exists():
            shutil.copy2(assets, tmp / "assets.yaml")
        (tmp / "themes").mkdir()
        if themes.is_dir():
            for f in themes.glob("*.yaml"):
                shutil.copy2(f, tmp / "themes" / f.name)
        for path, content in change.files.items():
            p = Path(path)
            dst = tmp / "assets.yaml" if p == assets else tmp / "themes" / p.name
            dst.write_text(content, encoding="utf-8")
        new = load_from(tmp / "settings.yaml", tmp / "assets.yaml", tmp / "themes")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    new.secrets = settings.secrets
    new._paths = settings.paths  # 指回正式文件：预览的配置只用来回放，不会被写回
    return new


async def replay_preview(settings_new: Settings, prices, symbols: list[str], days: int = 5) -> str:
    """按改动后的配置回放最近 days 个交易日，只看 symbols（新持仓及它的夜间信号）会推多少条。"""
    from collections import Counter

    from tickersentinel.jobs.replay import run_replay
    from tickersentinel.market_calendar import MarketCalendar

    res = await run_replay(settings_new, MarketCalendar(), prices, days, symbols)
    counts = Counter(a.symbol for _, a in res.alerts)
    if not res.segments:
        return f"回放：最近 {days} 个交易日没有可回放的时段"
    total = sum(counts.values())
    per = "、".join(f"{settings_new.universe.label(s)} {n} 条" for s, n in counts.most_common()) or "没有"
    return f"按建议的警报标准回放最近 {days} 个交易日：共 {total} 条（{per}）"


def describe_change_effects(old: Settings, new: Settings) -> list[str]:
    """改动前后范围的差别（多 / 少盯了哪些代码、哪些主题生效 / 停用），给读者确认用。"""
    ou, nu = old.universe, new.universe
    lines = []
    ot, nt = {t.key for t in ou.active_themes}, {t.key for t in nu.active_themes}
    if nt - ot:
        lines.append("生效的主题：+" + "、".join(nu.themes[k].name for k in nt - ot))
    if ot - nt:
        lines.append("停用的主题：" + "、".join(ou.themes[k].name for k in ot - nt))
    om, nm = {i.symbol for i in ou.monitored}, {i.symbol for i in nu.monitored}
    if nm - om:
        lines.append("开始盯盘：" + "、".join(sorted(nm - om)))
    if om - nm:
        lines.append("不再盯盘：" + "、".join(sorted(om - nm)))
    oe, ne = set(ou.event_symbols), set(nu.event_symbols)
    if ne - oe:
        lines.append(f"事件日历多收集 {len(ne - oe)} 家：" + "、".join(sorted(ne - oe)[:12]))
    if oe - ne:
        lines.append(f"事件日历少收集 {len(oe - ne)} 家")
    us = [s for s in nu.intraday_symbols if s not in ou.intraday_symbols and is_us_symbol(s)]
    if us:
        lines.append(f"每分钟多取 {len(us)} 个美股代码的行情")
    lines += threshold_changes(old, new)
    return lines


def _rules_text(settings: Settings, symbol: str) -> dict[str, str]:
    r = settings.rules_for(symbol)
    if r.rules == ["idio"]:
        return {"单独大动": f"±{r.idiosyncratic.min_move_pct:g}%"}
    return {"全天": "/".join(f"{x:g}" for x in r.day_pct) + "%",
            "急涨急跌": "、".join(f"{w}分钟 {r.min_move_pct[w]:g}%" for w in r.windows_minutes),
            "z": f"{r.z_threshold:g}", "时段": settings.hours_for(symbol) if not is_pseudo(symbol) else ""}


def threshold_changes(old: Settings, new: Settings) -> list[str]:
    """两边都在盯的资产，警报标准有没有变（改了 assets.yaml / 主题文件里的阈值时告诉读者改成了什么）。"""
    both = [i.symbol for i in new.universe.monitored if old.universe.find(i.symbol) is not None
            and any(j.symbol == i.symbol for j in old.universe.monitored)]
    out = []
    for sym in both:
        a, b = _rules_text(old, sym), _rules_text(new, sym)
        diffs = [f"{k} {a.get(k, '—')} → {v}" for k, v in b.items() if a.get(k) != v and v]
        if diffs:
            out.append(f"{new.universe.display(sym)}：" + "；".join(diffs))
    return out


__all__ = ["Change", "ScopeEditor", "ScopeEditError", "add_asset_text", "remove_asset_text", "theme_yaml",
           "short_name", "describe_change_effects", "datetime"]
