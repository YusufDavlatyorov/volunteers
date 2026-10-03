"""Post the weekly "thank you" to the volunteer community (counts + new badges,
no rankings — owner decision). Once per ISO week, even if run again — weekly cron.
Logic: myapp.services.community.post_weekly."""

from django.core.management.base import BaseCommand

from myapp.services import community


class Command(BaseCommand):
    help = "Post the weekly 'thank you' message to the volunteer community (idempotent per ISO week)."

    def add_arguments(self, parser):
        parser.add_argument("--preview", action="store_true", help="Print the text instead of posting it.")

    def handle(self, *args, **options):
        if options["preview"]:
            self.stdout.write(community.weekly_text(community.weekly_numbers()))
            return
        if not community.configured():
            self.stdout.write("community not configured — nothing to do")
            return
        self.stdout.write("posted" if community.post_weekly() else "already posted this week (or failed)")
