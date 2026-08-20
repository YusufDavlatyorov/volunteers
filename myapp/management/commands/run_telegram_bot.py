import time

import requests
from django.conf import settings
from django.core.management.base import BaseCommand

from accounts.models import Users


class Command(BaseCommand):
    help = "Запускает простого Telegram-бота для привязки chat id и просмотра запросов"

    def handle(self, *args, **options):
        if not settings.TELEGRAM_BOT_TOKEN:
            self.stderr.write("TELEGRAM_BOT_TOKEN не задан в .env")
            return

        offset = None
        self.stdout.write(self.style.SUCCESS("Telegram bot started"))
        while True:
            updates = self._get_updates(offset)
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                text = (message.get("text") or "").strip()
                chat_id = chat.get("id")
                if not chat_id:
                    continue
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
        if text.startswith("/start"):
            self._send(
                chat_id,
                "Generation Connect\n\n"
                "Команды:\n"
                "/link username - привязать Telegram к аккаунту\n"
                "/id - показать ваш chat id\n"
                "/help - помощь",
            )
            return

        if text.startswith("/id"):
            self._send(chat_id, f"Ваш Telegram chat id: {chat_id}")
            return

        if text.startswith("/link"):
            username = text.replace("/link", "", 1).strip()
            user = Users.objects.filter(username=username).first()
            if not user:
                self._send(chat_id, "Пользователь не найден. Проверьте username.")
                return
            user.telegram_id = chat_id
            user.save(update_fields=["telegram_id"])
            self._send(chat_id, f"Готово. Telegram привязан к аккаунту {user.username}.")
            return

        self._send(chat_id, "Напишите /start, чтобы увидеть команды.")
