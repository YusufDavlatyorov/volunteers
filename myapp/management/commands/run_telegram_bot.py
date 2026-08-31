import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from myapp.services.telegram_link import redeem_link_code


HELP_TEXT = (
    "Generation Connect\n\n"
    "Команды:\n"
    "/link <код> — привязать этот Telegram к вашему аккаунту.\n"
    "   Одноразовый код можно получить на сайте: войдите в аккаунт →\n"
    "   Профиль → «Привязать Telegram».\n"
    "/id — показать chat id этого чата\n"
    "/help — показать эту справку"
)


class Command(BaseCommand):
    help = "Long-polling Telegram bot: secure account linking and chat-id lookup"

    def handle(self, *args, **options):
        if not settings.TELEGRAM_BOT_TOKEN:
            self.stderr.write("TELEGRAM_BOT_TOKEN не задан в .env")
            return

        offset = None
        self.stdout.write(self.style.SUCCESS("Telegram bot started"))
        while True:
            for update in self._get_updates(offset):
                offset = update["update_id"] + 1
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                text = (message.get("text") or "").strip()
                chat_id = chat.get("id")
                if chat_id:
                    self._handle_message(chat_id, text)
            time.sleep(1)

    def _api(self, method, **payload):
        url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/{method}"
        response = requests.post(url, json=payload, timeout=25)
        response.raise_for_status()
        return response.json()

    def _get_updates(self, offset):
        payload = {"timeout": 20}
        if offset:
            payload["offset"] = offset
        return self._api("getUpdates", **payload).get("result", [])

    def _send(self, chat_id, text):
        self._api("sendMessage", chat_id=chat_id, text=text)

    def _handle_message(self, chat_id, text):
        # First whitespace-delimited word, without a "@BotName" suffix (Telegram
        # appends it to commands sent in group chats).
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text else ""

        if command in ("/start", "/help"):
            self._send(chat_id, HELP_TEXT)
            return

        if command == "/id":
            self._send(chat_id, f"Ваш Telegram chat id: {chat_id}")
            return

        if command == "/link":
            parts = text.split(maxsplit=1)
            code = parts[1].strip() if len(parts) > 1 else ""
            if not code:
                self._send(
                    chat_id,
                    "Использование: /link <код>\n\n"
                    "Одноразовый код можно получить на сайте: Профиль → «Привязать Telegram».",
                )
                return
            outcome = redeem_link_code(chat_id, code)
            self._send(chat_id, outcome.message)
            # Tell the previously-bound chat it has been detached, so a
            # re-link is visible to whoever was receiving notifications.
            if outcome.ok and outcome.previous_chat_id:
                self._send(
                    outcome.previous_chat_id,
                    "Этот Telegram больше не привязан к аккаунту: владелец привязал новый чат.",
                )
            return

        self._send(chat_id, "Напишите /start, чтобы увидеть список команд.")
