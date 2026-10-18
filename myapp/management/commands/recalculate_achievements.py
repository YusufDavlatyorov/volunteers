"""Recalculate every volunteer's achievements from the data.

The rules live in ``myapp.services.achievements`` (thresholds on the
``Achievement`` rows); a badge is also re-evaluated whenever a request is
completed / credited and whenever a review is saved. This command runs the
same ``evaluate`` for every active volunteer — after a threshold change in the
admin, after ``seed_demo``, or any time in doubt. Idempotent; silent unless
``--notify``; ``--demo-only`` limits it to demo accounts; ``--dry-run`` lists
who would get which badge and saves nothing. Never revokes (owner decision —
see services/achievements.py).
"""

from collections import Counter

from django.core.management.base import BaseCommand

from accounts.models import Users
from myapp.models import Achievement, UserAchievement
from myapp.services import achievements


class Command(BaseCommand):
    help = "Recalculate volunteer achievements from completed requests and reviews (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--notify", action="store_true", help="Send the 'new achievement' notification.")
        parser.add_argument("--demo-only", action="store_true", help="Only demo accounts (is_demo=True).")
        parser.add_argument("--dry-run", action="store_true", help="List who would get which badge; save nothing.")

    def handle(self, *args, **options):
        volunteers = Users.objects.filter(is_volunteer=True, is_active=True).order_by("pk")
        if options["demo_only"]:
            volunteers = volunteers.filter(is_demo=True)
        per_code = Counter()
        checked = 0
        active = list(Achievement.objects.filter(is_active=True))
        held = set(UserAchievement.objects.values_list("user_id", "achievement_id"))
        for user in volunteers.iterator(chunk_size=200):
            checked += 1
            if options["dry_run"]:
                numbers = achievements.counters(user)
                codes = [a.code for a in active if (user.pk, a.pk) not in held and achievements.is_met(a, numbers)]
            else:
                codes = [row.achievement.code for row in
                         achievements.evaluate(user, send_notifications=options["notify"])]
            if codes:
                self.stdout.write(f"  {user.username}: {', '.join(codes)}")
            per_code.update(codes)
        verb = "would award" if options["dry_run"] else "awarded"
        self.stdout.write(f"checked {checked} volunteer(s), {verb} {sum(per_code.values())} new badge(s)")
        for code, count in sorted(per_code.items()):
            self.stdout.write(f"  {code}: {count}")
