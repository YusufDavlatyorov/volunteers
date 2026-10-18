"""Restore the profile heatmap's per-day activity (UserDailyActivity) from the
real timestamps already in the database — nothing is invented. The sources and
the rules are in myapp/services/activity_backfill.py.

Idempotent: a day is recounted, never added up; days without an event get no
row; a row DailyActivityMiddleware wrote is never lowered; demo accounts and
demo rows are never touched. ``--dry-run`` prints what would be written.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from myapp.services import activity_backfill


class Command(BaseCommand):
    help = "Restore UserDailyActivity from real timestamps (idempotent; --dry-run writes nothing)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Show what would be written, write nothing.")
        parser.add_argument("--quiet-users", action="store_true", help="Totals only, no per-user lines.")

    def handle(self, *args, **options):
        result = activity_backfill.plan()
        names = dict(get_user_model().objects.values_list("id", "username"))
        self.stdout.write(f"window: {activity_backfill.activity.START_DATE} .. {result.today} (Asia/Dushanbe days)")
        self.stdout.write(f"real events: {sum(result.counts.values())} on {len(result.counts)} user-days "
                          f"of {len(result.per_user())} user(s)")
        for label, n in sorted(result.per_source.items(), key=lambda kv: -kv[1]):
            self.stdout.write(f"  {label}: {n}")
        if not options["quiet_users"]:
            new = {}
            for uid, _day, _n in result.create:
                new[uid] = new.get(uid, 0) + 1
            for uid, (days, events) in sorted(result.per_user().items(), key=lambda kv: names.get(kv[0], "")):
                self.stdout.write(f"  {names.get(uid, uid)}: {days} active day(s), {events} event(s), "
                                  f"{new.get(uid, 0)} new row(s)")
        verb = "would" if options["dry_run"] else "will"
        self.stdout.write(f"{verb} create {len(result.create)}, update {len(result.update)}, "
                          f"delete {len(result.delete)} row(s)")
        if options["dry_run"]:
            self.stdout.write("dry run: nothing written")
            return
        done = activity_backfill.apply(result)
        self.stdout.write(self.style.SUCCESS(
            f"done: created {done['created']}, updated {done['updated']}, deleted {done['deleted']}"))
