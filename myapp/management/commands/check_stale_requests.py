"""Alert curators/admins about pending help requests that have waited past the
48-hour stale threshold without a volunteer. Idempotent — intended to run from
cron (see README), the mirror of check_overdue_tasks for the pending side.

All logic lives in myapp.services.stale; this command is only a CLI wrapper.
"""

from django.core.management.base import BaseCommand

from myapp.services import stale


class Command(BaseCommand):
    help = (
        "Notify curators/admins about pending help requests that have waited "
        "past the stale threshold (STALE_PENDING_THRESHOLD, 48h) with no "
        "volunteer. Idempotent via HelpRequest.stale_alert_sent — safe to run "
        "repeatedly (e.g. hourly on cron)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show which requests are stale without notifying anyone or setting stale_alert_sent.",
        )

    def handle(self, *args, **options):
        if options["dry_run"]:
            tasks = list(stale.find_stale_pending())
            for task in tasks:
                self.stdout.write(f"  would alert: request #{task.id} (client: {task.client.username})")
            self.stdout.write(self.style.WARNING(f"[dry-run] {len(tasks)} stale request(s), nothing sent."))
            return

        alerted = stale.sweep_stale_pending()
        for task in alerted:
            self.stdout.write(f"  alerted: request #{task.id} (client: {task.client.username})")
        self.stdout.write(self.style.SUCCESS(f"Stale-pending sweep complete: {len(alerted)} request(s) newly alerted."))
