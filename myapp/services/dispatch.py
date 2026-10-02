"""Dispatch helpers around do-not-disturb volunteers (owner decision 2026-10-02).

Matching (`services/matching.py`) excludes volunteers whose do-not-disturb is in
effect. Staff may still assign one by hand in an emergency, but only after an
explicit confirmation — the dispatch card lists them separately with a warning,
and `task_assign_volunteer_view` refuses the assignment without
`confirm_away=1`. Read-only here; never assigns or notifies.
"""

from django.db.models import Q
from django.utils import timezone

from accounts.models import Users

from .geo import haversine_km

AWAY_CANDIDATE_CAP = 50
AWAY_LIST_LIMIT = 5


def away_in_effect_q(prefix="profile__"):
    """Negation of `presence.not_away_q()` — do-not-disturb on right now."""
    now = timezone.now()
    return Q(**{f"{prefix}is_away": True}) & (
        Q(**{f"{prefix}away_until__isnull": True}) | Q(**{f"{prefix}away_until__gt": now})
    )


def away_volunteers_for(task, limit=AWAY_LIST_LIMIT):
    """Do-not-disturb volunteers worth showing for `task`: same region first
    (or everyone when the task has no region), nearest first when both sides
    have a location. Bounded: one query over at most AWAY_CANDIDATE_CAP rows."""
    qs = (
        Users.objects.filter(is_volunteer=True, is_active=True)
        .filter(away_in_effect_q())
        .select_related("profile")
    )
    if task.region:
        qs = qs.filter(region=task.region)
    candidates = list(qs.order_by("id")[:AWAY_CANDIDATE_CAP])

    def _distance(user):
        profile = user.profile
        if task.has_location and profile.has_location:
            return round(haversine_km(
                float(task.latitude), float(task.longitude),
                float(profile.latitude), float(profile.longitude),
            ), 1)
        return None

    rows = [(user, _distance(user)) for user in candidates]
    rows.sort(key=lambda r: (r[1] is None, r[1] or 0, r[0].pk))
    return rows[:limit]


def is_away_now(user):
    profile = getattr(user, "profile", None)
    return bool(profile and profile.is_away_now)

