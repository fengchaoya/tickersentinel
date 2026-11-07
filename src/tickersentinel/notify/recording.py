"""推送记录：包在真正的推送渠道外面，每条消息发完（或没发出去）都记进 messages 表。

网页的消息流从这张表读（同进程的 tsen run、单独运行的 tsen web 都一样），内容和飞书里收到的一致。
发送失败时也记一条（ok = 0），异常照样抛给调用方——异动的“没送达就补发”逻辑不受影响。
记录本身失败只写日志，不能耽误推送。
"""

from __future__ import annotations

import logging
from typing import Any

from tickersentinel.models import Message
from tickersentinel.notify.base import InboundHandler, Notifier

log = logging.getLogger(__name__)


class RecordingNotifier(Notifier):
    def __init__(self, inner: Notifier, storage: Any):
        self.inner = inner
        self.storage = storage

    @property
    def name(self) -> str:  # type: ignore[override]
        return getattr(self.inner, "name", "notifier")

    def __getattr__(self, item: str) -> Any:
        # 其他属性（ConsoleNotifier.sent、log_path……）照样能从外面读到
        return getattr(self.inner, item)

    async def start(self, on_message: InboundHandler | None = None) -> None:
        await self.inner.start(on_message)

    async def stop(self) -> None:
        await self.inner.stop()

    async def send(self, msg: Message) -> str | None:
        try:
            mid = await self.inner.send(msg)
        except Exception as exc:
            self._record(msg, None, ok=False, error=f"{type(exc).__name__}: {exc}")
            raise
        self._record(msg, mid, ok=True)
        return mid

    def _record(self, msg: Message, mid: str | None, *, ok: bool, error: str | None = None) -> None:
        try:
            self.storage.record_message(msg, channel=self.name, message_id=mid, ok=ok, error=error)
        except Exception:  # pragma: no cover - 记录失败不能耽误推送
            log.exception("推送记录写入失败")
