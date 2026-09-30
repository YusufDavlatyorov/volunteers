"""Bootstrap exactly four production accounts (admin/curator/volunteer/client)
on a fresh database — the only accounts production should ever get from a
management command. Idempotent: safe to run more than once, never creates
duplicates, never touches an account that already exists (so a later manual
password change survives a re-run). Creates no demo business data — contrast
with `seed_demo`, which is dev/demo-only and must never run in production.

Credentials come from environment variables only (see .env.example); this
command never invents or logs a password.
"""

import os

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import REGION_CHOICES

User = get_user_model()

VALID_REGIONS = {key for key, _ in REGION_CHOICES}
DEFAULT_REGION = "dushanbe"

# (role key, env prefix, default username, extra field kwargs, needs a region)
ROLE_SPECS = [
    ("admin", "INITIAL_ADMIN", "admin", {"is_superuser": True, "is_staff": True}, False),
    ("curator", "INITIAL_CURATOR", "curator", {"is_curator": True}, True),
    ("volunteer", "INITIAL_VOLUNTEER", "volunteer", {"is_volunteer": True}, True),
    ("client", "INITIAL_CLIENT", "client", {"is_client": True}, True),
]


class Command(BaseCommand):
    help = (
        "Create the 4 initial production accounts (1 admin, 1 curator, 1 "
        "volunteer, 1 client) from INITIAL_<ROLE>_EMAIL / _PASSWORD / "
        "_USERNAME / _REGION env vars. Idempotent — an account that already "
        "exists (by username or email) is left untouched. Never run seed_demo "
        "in production; this command creates no demo business data."
    )

    def handle(self, *args, **options):
        specs = []
        missing = []
        for role, prefix, default_username, extra, needs_region in ROLE_SPECS:
            username = os.getenv(f"{prefix}_USERNAME", "").strip() or default_username
            email = os.getenv(f"{prefix}_EMAIL", "").strip()
            password = os.getenv(f"{prefix}_PASSWORD", "")
            region = os.getenv(f"{prefix}_REGION", "").strip() or DEFAULT_REGION

            if not email:
                missing.append(f"{prefix}_EMAIL")
            if not password:
                missing.append(f"{prefix}_PASSWORD")
            if needs_region and region not in VALID_REGIONS:
                raise CommandError(
                    f"{prefix}_REGION={region!r} is not a valid region "
                    f"(choices: {', '.join(sorted(VALID_REGIONS))})."
                )
            specs.append((role, prefix, username, email, password, region, extra, needs_region))

        if missing:
            raise CommandError(
                "Missing required environment variable(s): "
                + ", ".join(missing)
                + ". Set them in .env (see .env.example) and re-run — "
                "no credentials are invented by this command."
            )

        created, skipped = 0, 0
        for role, prefix, username, email, password, region, extra, needs_region in specs:
            existing = User.objects.filter(username=username).first() or User.objects.filter(
                email=email
            ).first()
            if existing:
                self.stdout.write(
                    f"skip: {role} account already exists "
                    f"(username={existing.username!r}, email={existing.email!r})"
                )
                skipped += 1
                continue

            candidate = User(username=username, email=email, **extra)
            try:
                validate_password(password, user=candidate)
            except DjangoValidationError as exc:
                raise CommandError(
                    f"Password for {role} ({prefix}_PASSWORD) fails validation: "
                    + " ".join(exc.messages)
                )

            with transaction.atomic():
                candidate.is_active = True
                candidate.is_email_verified = True
                if needs_region:
                    candidate.region = region
                candidate.set_password(password)
                candidate.save()

            self.stdout.write(self.style.SUCCESS(f"created: {role} account (username={username!r})"))
            created += 1

        self.stdout.write(self.style.SUCCESS(f"Done. Created {created}, skipped {skipped} (already existed)."))
