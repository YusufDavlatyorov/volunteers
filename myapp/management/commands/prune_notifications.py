"""Delete in-app notifications older than NOTIFICATION_RETENTION_DAYS (90).

Run daily from cron (README "Background jobs"). Idempotent; ``--dry-run``
only counts. ``--days`` overrides the setting for a one-off cleanup.
"""

from django.core.management.base import BaseCommand

from myapp.services import notify


class Command(BaseCommand):
    help = "Delete notifications older than NOTIFICATION_RETENTION_DAYS."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Only count what would be deleted.")
        parser.add_argument("--days", type=int, default=None, help="Override NOTIFICATION_RETENTION_DAYS.")

    def handle(self, *args, **options):
        days = options["days"]
        if days is not None and days < 1:
            self.stderr.write("--days must be >= 1")
            return
        count = notify.prune(days, dry_run=options["dry_run"])
        verb = "Would delete" if options["dry_run"] else "Deleted"
        self.stdout.write(f"{verb} {count} notification(s).")
