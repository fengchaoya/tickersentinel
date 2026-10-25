"""同一主题的几只同方向一起异动时合并推送（monitor.merge_minutes，2026-10-24 用户同意）。

- 同一次检测里、同一主题（持仓、夜间信号、篮子都算）、同方向的几条 → 合成一条推送，归因也只做一次；
- 第一条推出去之后 merge_minutes 分钟内，同一主题同方向又触发的（别的股票跟上了，或者同一只跌得更深）→
  挂在第一条下面作回复，不另起一条，也不重复附宏观快照；个股因素为主的照样单独归因，板块联动的不再重复归因；
- 没写 theme 的资产（默认主题）彼此不相关，不合并。merge_minutes = 0 关掉合并。
异动本身照样一条条入库（alerts 表是事件研究的数据集），合并只影响怎么推送。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from tickersentinel.config import DEFAULT_THEME, Settings
from tickersentinel.models import Alert


@dataclass
class Thread:
    """一串合并的推送：第一条的消息 ID 和时间。"""

    theme: str
    direction: int
    started: datetime  # 第一条异动的时间
    message_id: str
    symbols: list[str] = field(default_factory=list)
    alert_ids: list[int] = field(default_factory=list)  # 第一条推送里的异动（跟帖的归因指向它们）


@dataclass
class Batch:
    """一次要推送的内容：一条或几条异动；thread 不为空时挂在那一串下面作回复。"""

    alerts: list[Alert]
    theme: str | None = None  # None = 不参与合并
    thread: Thread | None = None

    @property
    def direction(self) -> int:
        return self.alerts[0].direction


def merge_theme(settings: Settings, symbol: str) -> str | None:
    """参与合并的主题：资产的主主题；默认主题不合并。"""
    keys = settings.universe.theme_keys_of(symbol)
    key = keys[0] if keys else None
    return None if not key or key == DEFAULT_THEME or key not in settings.universe.themes else key


class Merger:
    def __init__(self, minutes: int):
        self.minutes = minutes
        self.threads: dict[tuple[str, int], Thread] = {}

    def plan(self, alerts: list[Alert], settings: Settings) -> list[Batch]:
        """把同一次检测出的异动分成要推送的几批（保持原来的先后）。"""
        if self.minutes <= 0:
            return [Batch([a]) for a in alerts]
        out: list[Batch] = []
        index: dict[tuple[str, int], Batch] = {}
        for a in alerts:
            th = merge_theme(settings, a.symbol)
            if th is None:
                out.append(Batch([a]))
                continue
            key = (th, a.direction)
            if key in index:
                index[key].alerts.append(a)
                continue
            t = self.threads.get(key)
            live = t is not None and bool(t.message_id) and a.ts - t.started <= timedelta(minutes=self.minutes)
            index[key] = Batch([a], th, thread=t if live else None)
            out.append(index[key])
        return out

    def opened(self, batch: Batch, message_id: str | None) -> None:
        """新的一条推出去了：之后 minutes 分钟内同主题同方向的挂在它下面。"""
        if batch.theme is None or not message_id:
            return
        self.threads[(batch.theme, batch.direction)] = Thread(
            batch.theme, batch.direction, batch.alerts[0].ts, message_id, [a.symbol for a in batch.alerts],
            [a.id for a in batch.alerts if a.id is not None])

    def followed(self, batch: Batch) -> None:
        if batch.thread is not None:
            batch.thread.symbols += [a.symbol for a in batch.alerts if a.symbol not in batch.thread.symbols]


def count_messages(alerts: list[Alert], settings: Settings) -> tuple[int, int]:
    """回放用：按实盘的合并规则，这些异动一共会推几条消息、其中几条是挂在前一条下面的回复。"""
    m = Merger(settings.monitor.merge_minutes)
    msgs = follows = 0
    by_ts: dict[datetime, list[Alert]] = {}
    for a in sorted(alerts, key=lambda x: x.ts):
        by_ts.setdefault(a.ts, []).append(a)
    for _, items in sorted(by_ts.items()):
        for b in m.plan(items, settings):
            msgs += 1
            if b.thread is not None:
                follows += 1
                m.followed(b)
            else:
                m.opened(b, "replay")
    return msgs, follows


__all__ = ["Batch", "Merger", "Thread", "count_messages", "merge_theme"]
