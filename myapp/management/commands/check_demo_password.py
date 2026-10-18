"""Read-only audit: does any account on this database accept the demo password?

``seed_demo``'s shared password is public (README, CLAUDE.md, docs/LOCAL_RUN.md,
the public GitHub repo), so on production no account may accept it, and the
demo usernames must not exist there at all (``seed_demo`` never runs in
production). Run at deploy (deploy/MIGRATION.md step 1) and any time in doubt.

Never writes: passwords are compared with ``hashers.check_password`` without a
setter, so even an old-format hash is not upgraded/saved (``Users.check_password``
would). Prints usernames only — never a password or hash. PBKDF2 makes each
comparison deliberately slow (≈ 0.1–1 s per account; 25 accounts took 1.7 s locally).
Exit code 1 when anything is found, so it can gate a script.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password
from django.core.management.base import BaseCommand, CommandError

from myapp.management.commands.seed_demo import PASSWORD as DEMO_PASSWORD, demo_usernames

DEMO_USERNAMES = frozenset(demo_usernames())


def demo_password_accounts():
    """``[(pk, username, is_active)]`` of accounts whose stored hash accepts the
    demo password. Never writes (no hash upgrade). Shared with check_demo_data."""
    users = get_user_model().objects.order_by("pk").only("pk", "username", "password", "is_active")
    return [(user.pk, user.username, user.is_active) for user in users.iterator()
            if user.has_usable_password() and check_password(DEMO_PASSWORD, user.password)]


class Command(BaseCommand):
    help = ("Read-only: list accounts that accept the public demo password and demo usernames "
            "present on this database. Exit code 1 if any.")

    def handle(self, *args, **options):
        checked = get_user_model().objects.count()
        accepted = [f"{username} ({'active' if active else 'inactive'})"
                    for _pk, username, active in demo_password_accounts()]
        present = sorted(get_user_model().objects.filter(username__in=DEMO_USERNAMES)
                         .values_list("username", flat=True))
        self.stdout.write(f"checked accounts: {checked}")
        self.stdout.write(f"demo password accepted by: {len(accepted)}"
                          + (" — " + ", ".join(accepted) if accepted else ""))
        self.stdout.write(f"demo usernames present: {len(present)}"
                          + (" — " + ", ".join(present) if present else ""))
        if accepted or present:
            raise CommandError("demo credentials found — change these passwords / remove demo accounts")
        self.stdout.write(self.style.SUCCESS("OK: no account accepts the demo password"))
