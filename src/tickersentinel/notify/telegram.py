"""Telegram 推送（尚未实现，目前是占位；飞书已可用，见 notify/feishu.py）。

实现计划（python-telegram-bot v21+，安装：pip install -e ".[telegram]"）：
1. 准备：在 Telegram 里找 @BotFather → /newbot 拿到 TELEGRAM_BOT_TOKEN；给机器人发一条消息后，
   访问 https://api.telegram.org/bot<TOKEN>/getUpdates 找到自己的 chat.id 填入 TELEGRAM_CHAT_ID。
2. 发送：Application.bot.send_message(chat_id, text, parse_mode="HTML",
   reply_parameters=ReplyParameters(message_id=...))；把轻量 Markdown 转成 Telegram HTML 子集
  （<b>、<a href>），注意转义 & < >；单条消息上限 4096 字符，超长要拆分。
3. 接收：Application 长轮询（run_polling / updater.start_polling），不需要公网地址；
   收到消息 → 构造 IncomingMessage（含被回复的那条推送的文本）→ on_message 回调 → 回复。
   只处理来自 TELEGRAM_CHAT_ID 的消息，其他人一律忽略。
4. 可选：InlineKeyboard 按钮「展开详情」「静音 1 小时」。
"""

from __future__ import annotations

from tickersentinel.models import Message
from tickersentinel.notify.base import InboundHandler, Notifier


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(self, bot_token: str | None, chat_id: str | None):
        if not bot_token or not chat_id:
            raise ValueError("使用 Telegram 推送需要在 .env 中填写 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID")
        self.bot_token = bot_token
        self.chat_id = chat_id

    async def start(self, on_message: InboundHandler | None = None) -> None:
        raise NotImplementedError("Telegram 推送尚未实现（按计划放在后面）。目前请把 notify.provider 设为 console。")

    async def send(self, msg: Message) -> str | None:
        raise NotImplementedError("Telegram 推送尚未实现（按计划放在后面）。目前请把 notify.provider 设为 console。")
