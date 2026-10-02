"""Award the badges volunteers already earned before achievements existed.

Run once at deploy, after ``migrate`` (DEPLOY checklist). Idempotent — a second
run awards nothing new. Silent by default: a backfill must not wake every
volunteer with a pile of "new achievement" messages; ``--notify`` sends the
usual in-app/Telegram notice for each newly awarded badge.

All rules live in myapp.services.achievements; this command is a CLI wrapper.
"""

from collections import Counter

from django.core.management.base import BaseCommand

from accounts.models import Users
from myapp.models import Achievement, UserAchievement
from myapp.services import achievements


class Command(BaseCommand):
    help = "Award already-earned volunteer badges (idempotent; silent unless --notify)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Count the badges that would be awarded without saving anything.")
        parser.add_argument("--notify", action="store_true",
                            help="Send the 'new achievement' notification for each awarded badge.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        volunteers = Users.objects.filter(is_volunteer=True, is_active=True).order_by("pk")
        active = list(Achievement.objects.filter(is_active=True))
        held = set(UserAchievement.objects.values_list("user_id", "achievement_id"))
        per_code = Counter()
        people = 0
        for user in volunteers.iterator(chunk_size=200):
            if dry_run:
                numbers = achievements.counters(user)
                new = [a for a in active if (user.pk, a.pk) not in held and achievements.is_met(a, numbers)]
                codes = [a.code for a in new]
            else:
                codes = [row.achievement.code for row in
                         achievements.evaluate(user, send_notifications=options["notify"])]
            if codes:
                people += 1
                per_code.update(codes)
        verb = "would award" if dry_run else "awarded"
        total = sum(per_code.values())
        self.stdout.write(f"{verb} {total} badge(s) to {people} volunteer(s)")
        for code, count in sorted(per_code.items()):
            self.stdout.write(f"  {code}: {count}")
