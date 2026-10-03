"""Main KhayrKhoh Telegram bot (long polling) — Feature Pack v2, feature 4.

A role-aware assistant for linked accounts (volunteer / client / staff menus);
see myapp/telegram/main_bot/core.py for what it does and app.py for the
python-telegram-bot wiring. Same command name as before, so the compose
`telegram_bot` service needs no change. Exits (code 0) when
TELEGRAM_BOT_TOKEN is unset.
"""

from django.conf import settings
from django.core.management.base import BaseCommand

from myapp.telegram.main_bot import app as bot_app


class Command(BaseCommand):
    help = "Run the main Telegram bot (long polling): account linking + role menus for linked users."

    def handle(self, *args, **options):
        token = settings.TELEGRAM_BOT_TOKEN
        if not token:
            self.stderr.write("TELEGRAM_BOT_TOKEN не задан в .env")
            return
        application = bot_app.build(token)
        self.stdout.write(self.style.SUCCESS("Telegram bot started"))
        application.run_polling(allowed_updates=bot_app.ALLOWED_UPDATES, drop_pending_updates=False)
