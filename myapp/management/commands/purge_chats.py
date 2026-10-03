"""Chat retention sweep: delete chat photos 90 days and messages 180 days after
the task ended; flagged / reported messages are kept until reviewed + 180 days.
Idempotent — meant for a daily cron (DEPLOY checklist).

All logic lives in myapp.services.chat_retention; this command is a CLI wrapper.
"""

from django.conf import settings
from django.core.management.base import BaseCommand

from myapp.services import chat_retention


class Command(BaseCommand):
    help = (
        "Delete chat photos / messages past their retention period "
        "(CHAT_PHOTO_RETENTION_DAYS / CHAT_TEXT_RETENTION_DAYS after the task ended). Idempotent — run daily."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Count what would be deleted without deleting anything.")

    def handle(self, *args, **options):
        result = chat_retention.purge(dry_run=options["dry_run"])
        verb = "would delete" if options["dry_run"] else "deleted"
        self.stdout.write(
            f"{verb}: {result.photos} photo(s) (> {settings.CHAT_PHOTO_RETENTION_DAYS} d), "
            f"{result.messages} message(s) (> {settings.CHAT_TEXT_RETENTION_DAYS} d), "
            f"{result.read_states} read cursor(s); kept on hold: {result.held}"
        )
