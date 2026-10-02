"""Reminders about a volunteer's saved profile location (owner decision
2026-10-02). Matching uses the saved point (`Profile.latitude/longitude`), never
live tracking — so an old point quietly degrades the volunteer's suggestions.
`remind_stale(...)` nudges active volunteers whose point is missing or older than
LOCATION_STALE_DAYS, at most once per that period (`Profile.location_reminded_at`,
claimed with a conditional UPDATE before notifying, like the overdue sweep).
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from accounts.models import Profile

logger = logging.getLogger(__name__)

REMIND_BATCH_LIMIT = 500


def stale_volunteer_profiles(now=None):
    now = now or timezone.now()
    cutoff = now - timedelta(days=settings.LOCATION_STALE_DAYS)
    return (
        Profile.objects.filter(user__is_volunteer=True, user__is_active=True)
        .filter(Q(location_updated_at__isnull=True) | Q(location_updated_at__lt=cutoff)
                | Q(latitude__isnull=True) | Q(longitude__isnull=True))
        .filter(Q(location_reminded_at__isnull=True) | Q(location_reminded_at__lt=cutoff))
    )


def remind_stale(*, dry_run=False, now=None):
    """Returns the number of volunteers reminded (or that would be)."""
    from myapp.services.notify import notify

    now = now or timezone.now()
    cutoff = now - timedelta(days=settings.LOCATION_STALE_DAYS)
    candidates = list(
        stale_volunteer_profiles(now).select_related("user").order_by("pk")[:REMIND_BATCH_LIMIT]
    )
    if dry_run:
        return len(candidates)
    reminded = 0
    link = reverse("profile") + "#profileLocationCard"
    for profile in candidates:
        claimed = Profile.objects.filter(pk=profile.pk).filter(
            Q(location_reminded_at__isnull=True) | Q(location_reminded_at__lt=cutoff)
        ).update(location_reminded_at=now)
        if not claimed:
            continue  # a concurrent run got it
        notify([profile.user], "location_stale", params={}, link=link)
        reminded += 1
    logger.info("location reminders sent=%d", reminded)
    return reminded
