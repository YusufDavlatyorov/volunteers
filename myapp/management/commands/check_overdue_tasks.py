"""Alert curators/admins about help requests that have been active past the
3-hour overdue threshold. Idempotent — intended to run from cron (see README).

All logic lives in myapp.services.overdue; this command is only a CLI wrapper.
"""

from django.core.management.base import BaseCommand

from myapp.services import overdue


class Command(BaseCommand):
    help = (
        "Notify curators/admins about active help requests overdue past the "
        "3-hour threshold. Idempotent via HelpRequest.alarm_sent — safe to run "
        "repeatedly (e.g. every 15 minutes on cron)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show which tasks are overdue without notifying anyone or setting alarm_sent.",
        )

    def handle(self, *args, **options):
        if options["dry_run"]:
            tasks = list(overdue.find_overdue_tasks())
            for task in tasks:
                self.stdout.write(f"  would alert: request #{task.id} (volunteer: {task.volunteer})")
            self.stdout.write(self.style.WARNING(f"[dry-run] {len(tasks)} overdue task(s), nothing sent."))
            return

        alerted = overdue.sweep_overdue_tasks()
        for task in alerted:
            self.stdout.write(f"  alerted: request #{task.id} (volunteer: {task.volunteer})")
        self.stdout.write(self.style.SUCCESS(f"Overdue sweep complete: {len(alerted)} task(s) newly alerted."))
