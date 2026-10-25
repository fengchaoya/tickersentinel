"""控制台推送：开发调试用。打印到终端，同时追加写入 data/outbox.log，方便回看。"""

from __future__ import annotations

import itertools
from pathlib import Path

from tickersentinel.models import Message
from tickersentinel.notify.base import Notifier
from tickersentinel.timeutil import utcnow

_KIND_BAR = {"brief": "═", "alert": "━", "attribution": "─", "info": "·", "error": "!"}


class ConsoleNotifier(Notifier):
    name = "console"

    def __init__(self, log_path: Path | None = None, *, echo: bool = True):
        self.log_path = log_path
        self.echo = echo
        self.sent: list[tuple[str, Message]] = []  # 便于测试检查
        self._ids = itertools.count(1)

    async def send(self, msg: Message) -> str:
        mid = f"console-{next(self._ids)}"
        bar = _KIND_BAR.get(msg.kind, "─") * 48
        reply = f"  ↳ 回复 {msg.reply_to}\n" if msg.reply_to else ""
        text = f"\n{bar}\n{reply}{msg.title}\n{bar}\n{msg.body}\n"
        if self.echo:
            print(text, flush=True)
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(f"[{utcnow().isoformat(timespec='seconds')}] ({mid}){text}")
        self.sent.append((mid, msg))
        return mid
