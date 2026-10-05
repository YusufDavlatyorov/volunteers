"""Volunteer community Telegram bot (long polling) — Feature Pack v2, feature 5.

Invites are minted by the website / main bot; this process binds joiners to
their invites, removes everyone else, and moderates the chats
(myapp/telegram/community_bot). Setup: docs/COMMUNITY_BOT.md. Exits (code 0)
when TELEGRAM_COMMUNITY_BOT_TOKEN or the chat ids are not configured.
"""

from django.core.management.base import BaseCommand

from myapp.services import community
from myapp.telegram import polling
from myapp.telegram.community_bot import app as bot_app


class Command(BaseCommand):
    help = "Run the volunteer community bot (long polling): invite binding + moderation."

    def handle(self, *args, **options):
        if not community.configured():
            self.stderr.write("TELEGRAM_COMMUNITY_BOT_TOKEN / TELEGRAM_COMMUNITY_CHAT_IDS не заданы в .env")
            return
        application = bot_app.build(community.token())
        self.stdout.write(self.style.SUCCESS(f"Community bot started ({len(community.chats())} chat(s))"))
        polling.run(application, bot_app.ALLOWED_UPDATES)
