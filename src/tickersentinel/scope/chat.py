"""在飞书里调监控范围（/scope）。所有改动都要读者回复“确认”才写入（30 分钟内有效），写入后立即生效、可以撤销。

  /scope 或“监控范围”        看现在的范围（持仓 × 主题）
  /scope add 代码 [主题]     给新持仓找联动、出方案（附回放预览）→ 回复“确认”写入；“去掉 代码”“加上 代码”调整；“取消”放弃
  /scope remove 代码          移除持仓（主题文件保留备用）→ 回复“确认”
  /scope undo                撤销最近一次改动 → 回复“确认”
  /scope check               联动体检
  /scope tune 代码 [全天 5 8 12] [急涨 5:1.5 15:2.5 30:3 60:3]
                             调警报阈值：不给数字就按日波动建议，附回放对比 → 回复“确认”写入
预览模式（tsen ask 不加 --keep）只看不写。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Awaitable, Callable

from tickersentinel.config import Settings
from tickersentinel.scope.discover import Proposal, ScopeError, propose, render_proposal
from tickersentinel.scope.edit import (
    ScopeEditError,
    ScopeEditor,
    describe_change_effects,
    replay_preview,
    settings_after,
)
from tickersentinel.scope.view import describe_scope
from tickersentinel.textutil import truncate
from tickersentinel.timeutil import utcnow

if TYPE_CHECKING:
    from tickersentinel.llm.client import LLMClient
    from tickersentinel.sources.base import PriceSource
    from tickersentinel.storage import Storage

log = logging.getLogger(__name__)

PENDING_KEY = "scope:pending:{chat_id}"
PENDING_TTL = timedelta(minutes=30)
SHOW = {"/scope", "/scope show", "监控范围", "/范围"}
CONFIRM = {"确认", "确定", "/scope confirm", "confirm", "yes", "y", "是", "好"}
CANCEL = {"取消", "算了", "/scope cancel", "cancel", "no", "n", "不"}
_CMD = re.compile(r"^/scope\s+(add|suggest|remove|undo|check|drop|keep)\b\s*(\S+)?\s*(\S+)?\s*$", re.I)
_TUNE = re.compile(r"^/scope\s+tune\b\s*(\S+)?\s*(.*)$", re.I)
_DROP = re.compile(r"^(?:去掉|删掉|不要)\s*(\S+)$")
_KEEP = re.compile(r"^(?:加上|加回|保留)\s*(\S+)$")
HELP = ("回复「确认」写入，「取消」放弃；也可以回复「去掉 代码」「加上 代码」调整候选（30 分钟内有效）。")
OnChange = Callable[[Settings], Awaitable[list[str]]]


class ScopeChat:
    def __init__(self, settings: Settings, storage: "Storage", prices: "PriceSource | None", llm: "LLMClient | None", *,
                 on_change: OnChange | None = None, readonly: bool = False, replay_days: int = 5):
        self.settings = settings
        self.storage = storage
        self.prices = prices
        self.llm = llm
        self.on_change = on_change
        self.readonly = readonly
        self.replay_days = replay_days

    # ───────── 路由 ─────────

    def wants(self, chat_id: str, text: str) -> bool:
        t = text.strip()
        if t.lower() in SHOW or t.lower().startswith("/scope"):
            return True
        if self._pending(chat_id) is None:
            return False
        return t.lower() in CONFIRM or t.lower() in CANCEL or bool(_DROP.match(t) or _KEEP.match(t))

    async def handle(self, chat_id: str, text: str) -> str:
        t = text.strip()
        try:
            if t.lower() in SHOW:
                return describe_scope(self.settings)
            if (tm := _TUNE.match(t)) is not None:
                return await self._tune(chat_id, tm.group(1), tm.group(2) or "")
            m = _CMD.match(t)
            if m:
                action, a1, a2 = m.group(1).lower(), m.group(2), m.group(3)
                if action in ("add", "suggest"):
                    return await self._add(chat_id, a1, a2, write=action == "add")
                if action == "remove":
                    return self._remove(chat_id, a1)
                if action == "undo":
                    return self._undo(chat_id)
                if action == "check":
                    return await self._check()
                if action == "drop":
                    return self._adjust(chat_id, a1, drop=True)
                if action == "keep":
                    return self._adjust(chat_id, a1, drop=False)
            if t.lower().startswith("/scope"):
                return __doc__.split("\n\n", 1)[1].split("预览模式")[0].strip()
            if t.lower() in CONFIRM:
                return await self._confirm(chat_id)
            if t.lower() in CANCEL:
                self._clear(chat_id)
                return "好的，已放弃，没有改动。"
            if (dm := _DROP.match(t)) is not None:
                return self._adjust(chat_id, dm.group(1), drop=True)
            if (km := _KEEP.match(t)) is not None:
                return self._adjust(chat_id, km.group(1), drop=False)
        except (ScopeError, ScopeEditError) as exc:
            return str(exc)
        except Exception as exc:  # 不能让对话断掉
            log.exception("/scope 处理失败")
            return f"处理失败：{type(exc).__name__}: {truncate(str(exc), 200)}"
        return "没看懂。发 /scope 看用法。"

    # ───────── 待确认的改动（存在 kv，30 分钟内有效） ─────────

    def _pending(self, chat_id: str) -> dict | None:
        raw = self.storage.kv_get(PENDING_KEY.format(chat_id=chat_id))
        if not raw:
            return None
        try:
            data = json.loads(raw)
            if utcnow() - datetime.fromisoformat(data["at"]) > PENDING_TTL:
                self._clear(chat_id)
                return None
            return data
        except (ValueError, KeyError):
            self._clear(chat_id)
            return None

    def _save(self, chat_id: str, data: dict) -> None:
        self.storage.kv_set(PENDING_KEY.format(chat_id=chat_id),
                            json.dumps({**data, "at": utcnow().isoformat()}, ensure_ascii=False))

    def _clear(self, chat_id: str) -> None:
        self.storage.kv_set(PENDING_KEY.format(chat_id=chat_id), "")

    def _editor(self) -> ScopeEditor:
        return ScopeEditor(self.settings)

    # ───────── 各个动作 ─────────

    async def _add(self, chat_id: str, symbol: str | None, theme: str | None, *, write: bool) -> str:
        if not symbol:
            return "用法：/scope add 代码，例如 /scope add LITE（海外股票带交易所后缀，如 300308.SZ）"
        if self.prices is None:
            return "没有行情数据源，没法打分。"
        prop = await propose(self.settings, symbol, self.prices, llm=self.llm, theme=theme)
        text = render_proposal(prop, self.settings)
        if not write:
            return text + "\n\n（只是方案；要写入就发 /scope add " + prop.symbol + "）"
        change = self._editor().plan_add(prop)
        new = settings_after(self.settings, change)
        effects = describe_change_effects(self.settings, new)
        if self.replay_days > 0:
            watch = [prop.symbol, *(i.symbol for i in new.universe.monitored if not i.holding and prop.symbol in i.related_to)]
            try:
                effects.append(await replay_preview(new, self.prices, watch, self.replay_days))
            except Exception as exc:
                effects.append(f"回放没做成（{type(exc).__name__}），不影响写入")
        if self.readonly:
            return text + "\n\n" + "\n".join(f"- {x}" for x in effects) + "\n\n（预览模式不写配置：在飞书里发同样的命令，或用 tsen scope add）"
        self._save(chat_id, {"kind": "add", "proposal": prop.to_dict()})
        return text + "\n\n**会有这些变化**\n" + "\n".join(f"- {x}" for x in effects) + "\n\n" + HELP

    def _remove(self, chat_id: str, symbol: str | None) -> str:
        if not symbol:
            return "用法：/scope remove 代码"
        change = self._editor().plan_remove(symbol)
        new = settings_after(self.settings, change)
        effects = describe_change_effects(self.settings, new)
        body = change.summary + "\n" + "\n".join(f"- {x}" for x in effects)
        if self.readonly:
            return body + "\n\n（预览模式不写配置）"
        self._save(chat_id, {"kind": "remove", "symbol": change.symbol})
        return body + "\n\n回复「确认」写入，「取消」放弃（30 分钟内有效）。"

    def _undo(self, chat_id: str) -> str:
        hist = self._editor().history()
        if not hist:
            return "没有可以撤销的改动。"
        if self.readonly:
            return f"最近一次改动：{hist[-1]['summary']}（预览模式不写配置）"
        self._save(chat_id, {"kind": "undo", "summary": hist[-1]["summary"]})
        return f"将撤销最近一次改动：{hist[-1]['summary']}\n回复「确认」执行，「取消」放弃（30 分钟内有效）。"

    async def _tune(self, chat_id: str, target: str | None, rest: str) -> str:
        from tickersentinel.scope.tune import parse_numbers, propose_tune, render_tune

        if not target:
            return "用法：/scope tune 代码，例如 /scope tune MU（按波动给建议）或 /scope tune MU 全天 5 8 12 急涨 60:3"
        day, rapid = parse_numbers(rest) if rest.strip() else (None, None)
        plan = await propose_tune(self.settings, target, self.prices, day=day, rapid=rapid,
                                  replay_days=2 * self.replay_days if self.replay_days else 0)
        text = render_tune(plan)
        if plan.unchanged or plan.change is None:
            return text
        if self.readonly:
            return text + "\n\n（预览模式不写配置）"
        self._save(chat_id, {"kind": "tune", "target": target, "day": plan.new["day_pct"],
                             "rapid": {str(k): v for k, v in plan.new["rapid_pct"].items()}})
        return text + "\n\n回复「确认」写入，「取消」放弃（30 分钟内有效）。"

    async def _check(self) -> str:
        if self.prices is None:
            return "没有行情数据源，做不了联动体检。"
        from tickersentinel.scope.health import linkage_report

        return await linkage_report(self.settings, self.prices)

    def _adjust(self, chat_id: str, symbol: str | None, *, drop: bool) -> str:
        pending = self._pending(chat_id)
        if not symbol or pending is None or pending.get("kind") != "add":
            return "现在没有待确认的新增方案。"
        prop = Proposal.from_dict(pending["proposal"])
        ok = prop.drop(symbol) if drop else prop.restore(symbol)
        if not ok:
            return f"方案里没有可以{'去掉' if drop else '加上'}的 {symbol.upper()}。"
        self._save(chat_id, {"kind": "add", "proposal": prop.to_dict()})
        return render_proposal(prop, self.settings) + "\n\n（回放预览没有重算）" + HELP

    async def _confirm(self, chat_id: str) -> str:
        pending = self._pending(chat_id)
        if pending is None:
            return "没有待确认的改动（超过 30 分钟会失效）。"
        self._clear(chat_id)
        editor = self._editor()
        kind = pending.get("kind")
        if kind == "add":
            change = editor.plan_add(Proposal.from_dict(pending["proposal"]))
            new = editor.apply(change)
            head = f"✅ 已写入：{change.summary}"
        elif kind == "remove":
            change = editor.plan_remove(pending["symbol"])
            new = editor.apply(change)
            head = f"✅ 已写入：{change.summary}"
        elif kind == "undo":
            msg, new = editor.undo()
            head = f"✅ {msg}"
        elif kind == "tune":
            from tickersentinel.scope.tune import propose_tune

            plan = await propose_tune(self.settings, pending["target"], None, day=pending.get("day") or pending.get("levels"),
                                      rapid={int(k): v for k, v in pending["rapid"].items()}, replay_days=0)
            if plan.change is None:
                return "阈值已经是这样了，没有改动。"
            new = editor.apply(plan.change)
            head = f"✅ 已写入：{plan.change.summary}"
        else:
            return "待确认的改动格式不对，已清掉。"
        effects = await self.on_change(new) if self.on_change is not None else describe_change_effects(self.settings, new)
        tail = "（已经生效，不用重启；/scope undo 可以撤销）" if self.on_change is not None else "（重启 tsen run 生效）"
        return "\n".join([head, *(f"- {x}" for x in effects), tail])


__all__ = ["ScopeChat", "SHOW", "CONFIRM", "CANCEL"]
