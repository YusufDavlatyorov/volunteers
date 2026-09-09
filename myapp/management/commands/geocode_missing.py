"""Backfill coordinates for HelpRequests / Profiles that have an address or
region but no lat/lng, via the maps service layer.

For operators enriching real data — not run automatically. Idempotent: only
touches rows where latitude is NULL. Sleeps ~1.1s between calls to respect the
Nominatim usage policy (max 1 req/s).

    python manage.py geocode_missing [--limit N] [--dry-run] [--profiles]
"""

import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import Profile
from myapp.models import HelpRequest
from myapp.services import maps

SLEEP_SECONDS = 1.1


class Command(BaseCommand):
    help = "Geocode HelpRequests / Profiles that are missing coordinates."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--profiles", action="store_true", help="Also geocode Profile rows by region.")

    def handle(self, *args, **options):
        limit = options["limit"]
        dry_run = options["dry_run"]
        resolved = skipped = 0

        requests_qs = (
            HelpRequest.objects.filter(latitude__isnull=True)
            .exclude(address="")
            .order_by("-created_at")[:limit]
        )
        for req in requests_qs:
            coords = maps.geocode(req.address, region=req.region)
            if coords:
                resolved += 1
                self.stdout.write(f"  request #{req.id}: {req.address!r} -> {coords}")
                if not dry_run:
                    req.latitude, req.longitude = coords
                    req.save(update_fields=["latitude", "longitude"])
            else:
                skipped += 1
                self.stdout.write(f"  request #{req.id}: {req.address!r} -> (no match)")
            time.sleep(SLEEP_SECONDS)

        if options["profiles"]:
            profiles_qs = (
                Profile.objects.filter(latitude__isnull=True)
                .exclude(user__region="")
                .select_related("user")[:limit]
            )
            for profile in profiles_qs:
                coords = maps.geocode(profile.user.region, region=profile.user.region)
                if coords:
                    resolved += 1
                    if not dry_run:
                        profile.latitude, profile.longitude = coords
                        profile.location_updated_at = timezone.now()
                        profile.save(update_fields=["latitude", "longitude", "location_updated_at"])
                else:
                    skipped += 1
                time.sleep(SLEEP_SECONDS)

        verb = "would resolve" if dry_run else "resolved"
        self.stdout.write(self.style.SUCCESS(f"Done. {verb} {resolved}, no match {skipped}."))
