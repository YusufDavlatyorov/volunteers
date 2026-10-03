"""Remove community members who are no longer eligible (deactivated accounts,
volunteer role removed, Telegram unlinked). Idempotent — hourly cron.
Logic: myapp.services.community.sweep."""

from django.core.management.base import BaseCommand

from myapp.services import community


class Command(BaseCommand):
    help = "Remove volunteer-community members who are no longer eligible. Idempotent — run hourly."

    def handle(self, *args, **options):
        if not community.configured():
            self.stdout.write("community not configured — nothing to do")
            return
        self.stdout.write(f"removed: {community.sweep()}")
