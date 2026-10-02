"""Recompute stored volunteer availability in bulk: volunteers not seen within
AVAILABILITY_IDLE_MINUTES go offline, drift is repaired, and expired
do-not-disturb end dates switch off. Idempotent — meant for a 5-minute cron.

All logic lives in myapp.services.presence; this command is only a CLI wrapper.
"""

from django.core.management.base import BaseCommand

from myapp.services import presence


class Command(BaseCommand):
    help = (
        "Sweep volunteer availability (idle -> offline, busy/available drift, "
        "expired do-not-disturb). Idempotent — safe to run every 5 minutes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count what would change without writing or publishing anything.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        result = presence.sweep(dry_run=dry_run)
        summary = ", ".join(f"{key}={value}" for key, value in result.as_dict().items())
        if dry_run:
            self.stdout.write(self.style.WARNING(f"[dry-run] {int(result)} change(s) pending: {summary}"))
        else:
            self.stdout.write(self.style.SUCCESS(f"Availability sweep complete: {int(result)} change(s): {summary}"))
