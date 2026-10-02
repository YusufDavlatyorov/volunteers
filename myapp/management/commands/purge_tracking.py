"""Live-tracking retention sweep: delete trail points older than 24 h (or of
tasks that are no longer active) and clear latest positions left on ended tasks.
Idempotent — meant for an hourly cron.

All logic lives in myapp.services.tracking.purge; this command is only a CLI wrapper.
"""

import datetime

from django.core.management.base import BaseCommand

from myapp.services import tracking


class Command(BaseCommand):
    help = (
        "Delete live-tracking trail points older than 24 h or of ended tasks, and "
        "clear stale latest positions. Idempotent — safe to run hourly."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count the trail points that would be deleted without deleting anything.",
        )
        parser.add_argument(
            "--hours",
            type=int,
            default=int(tracking.TRAIL_MAX_AGE.total_seconds() // 3600),
            help="Maximum trail age in hours (default 24; values above 24 are capped).",
        )

    def handle(self, *args, **options):
        hours = max(1, min(options["hours"], 24))
        dry_run = options["dry_run"]
        count = tracking.purge(datetime.timedelta(hours=hours), dry_run=dry_run)
        if dry_run:
            self.stdout.write(self.style.WARNING(f"[dry-run] {count} trail point(s) would be deleted."))
        else:
            self.stdout.write(self.style.SUCCESS(f"Tracking purge complete: {count} trail point(s) deleted."))
