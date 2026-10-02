from django.core.management.base import BaseCommand

from myapp.services.profile_location import remind_stale


class Command(BaseCommand):
    help = "Remind volunteers whose saved profile location is missing or older than LOCATION_STALE_DAYS (daily cron)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Only count who would be reminded.")

    def handle(self, *args, **options):
        count = remind_stale(dry_run=options["dry_run"])
        verb = "would be reminded" if options["dry_run"] else "reminded"
        self.stdout.write(f"{count} volunteer(s) {verb}.")
