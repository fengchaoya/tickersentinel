"""推送渠道接口。新增渠道时实现 Notifier，并在 notify/__init__.py 的 build_notifier 里注册。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Awaitable, Callable

from tickersentinel.agents.chat import IncomingMessage
from tickersentinel.models import Message

# 收到用户消息时的回调：返回要回复的文本（None 表示不回复）
InboundHandler = Callable[[IncomingMessage], Awaitable[str | None]]


class Notifier(ABC):
    name: str = "notifier"

    async def start(self, on_message: InboundHandler | None = None) -> None:  # noqa: B027
        """建立连接；支持双向对话的渠道在这里开始接收消息（Telegram 长轮询 / 飞书长连接）。"""

    @abstractmethod
    async def send(self, msg: Message) -> str | None:
        """发送消息，返回平台消息 ID（用于把归因作为“回复”挂在异动消息下面）。"""

    async def stop(self) -> None:  # noqa: B027
        pass
