"""WebContext：网页用到的全部依赖。App.web_context() 组装；测试里直接构造（数据库用内存库，行情用假数据源）。"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable, Literal

from tickersentinel.timeutil import utcnow

if TYPE_CHECKING:
    from tickersentinel.agents.chat import ChatAgent
    from tickersentinel.barstore import BarStore
    from tickersentinel.config import Settings
    from tickersentinel.jobs.collect import BarCollector
    from tickersentinel.events.hub import EventHub
    from tickersentinel.market_calendar import MarketCalendar
    from tickersentinel.sources.base import PriceSource
    from tickersentinel.sources.macro import MacroSource
    from tickersentinel.storage import Storage

HEARTBEAT_KEY = "runtime:heartbeat"  # tsen run 每次检测写一次（App.write_heartbeat）
HEARTBEAT_STALE = timedelta(minutes=3)  # 超过这么久没有心跳，就当 tsen run 没在运行

Mode = Literal["embedded", "standalone", "preview"]
MODE_NAMES = {"embedded": "随 tsen run 运行", "standalone": "单独运行（不检测、不跑定时任务；提问、标注、补抓照常写入正式库）",
              "preview": "预览（正式库的内存副本，改动不保存）"}


@dataclass
class WebContext:
    settings: "Settings"
    storage: "Storage"
    calendar: "MarketCalendar"
    prices: "PriceSource | None" = None
    events: "EventHub | None" = None
    macro: "MacroSource | None" = None
    bars: "BarStore | None" = None  # 分钟线库（宏观趋势图、走势图先查这里）
    collector: "BarCollector | None" = None  # 网页上“现在补抓”用；预览模式没有
    chat: "ChatAgent | None" = None  # 网页上提问（和飞书里同一套对话，会话 web）
    mode: Mode = "standalone"
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24))  # 写请求要带上它（security.py）
    clock: Callable[[], datetime] = utcnow

    @property
    def readonly(self) -> bool:
        """预览模式下网页里的改动不落盘（内存副本），页面上要注明。"""
        return self.mode == "preview"

    def heartbeat(self) -> dict[str, Any] | None:
        """tsen run 写的心跳（启动时间、上次检测、推送渠道……）；没有或读不通时 None。"""
        raw = self.storage.kv_get(HEARTBEAT_KEY)
        if not raw:
            return None
        try:
            hb = json.loads(raw)
        except ValueError:
            return None
        return hb if isinstance(hb, dict) else None

    def runtime(self) -> dict[str, Any]:
        """tsen run 是否在运行：{running, heartbeat, age_seconds}。嵌入模式下当然在运行。"""
        hb = self.heartbeat()
        age = None
        if hb and hb.get("at"):
            try:
                age = (self.clock() - datetime.fromisoformat(hb["at"])).total_seconds()
            except (TypeError, ValueError):
                age = None
        running = self.mode == "embedded" or (age is not None and age <= HEARTBEAT_STALE.total_seconds())
        return {"running": running, "heartbeat": hb, "age_seconds": age}
