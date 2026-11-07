"""推送渠道工厂。settings.yaml 中 notify.provider 就是渠道开关：console | telegram | feishu。"""

from __future__ import annotations

import logging

from tickersentinel.config import Settings
from tickersentinel.notify.base import InboundHandler, Notifier
from tickersentinel.notify.console import ConsoleNotifier

__all__ = ["InboundHandler", "Notifier", "ConsoleNotifier", "build_notifier"]

_log = logging.getLogger(__name__)


def build_notifier(settings: Settings, *, force_console: bool = False, log: bool = True) -> Notifier:
    """force_console：预览模式只打印到终端；log=False 时也不写 outbox.log（outbox.log 只记录真正推送的内容）。"""
    provider = "console" if force_console else settings.notify.provider
    path = settings.resolve_path(settings.notify.console_log_path) if settings.notify.console_log_path and log else None
    if provider == "console":
        return ConsoleNotifier(path)
    try:
        return _build_remote(settings, provider)
    except (ValueError, RuntimeError) as exc:
        if provider not in ("telegram", "feishu"):
            raise
        # 缺密钥或没装 SDK：退回终端打印（README 的承诺），而不是让 tsen run / brief --send 启动就崩溃
        _log.warning("推送渠道 %s 不可用，改为只打印到终端：%s", provider, exc)
        return ConsoleNotifier(path)


def _build_remote(settings: Settings, provider: str) -> Notifier:
    sec = settings.secrets
    if provider == "telegram":
        from tickersentinel.notify.telegram import TelegramNotifier

        return TelegramNotifier(sec.get("telegram_bot_token"), sec.get("telegram_chat_id"))
    if provider == "feishu":
        from tickersentinel.notify.feishu import FeishuNotifier

        return FeishuNotifier(sec.get("feishu_app_id"), sec.get("feishu_app_secret"), sec.get("feishu_chat_id"),
                              domain=settings.notify.feishu_domain,
                              ack=settings.chat.ack if settings.chat.enabled else "")
    raise ValueError(f"未知的推送渠道：{provider}")
