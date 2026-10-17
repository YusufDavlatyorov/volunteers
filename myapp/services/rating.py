"""The public volunteer rating (/rating/) and the About page's leaders.

**Places again** (owner 2026-10-14) — this reverses the 2026-10-03/09 rule
"one list by name, no places, no points". There is ONE national ranking, shown
as a podium (places 1–3) and a table from place 4 — no search, region filter
or re-sorting (removed 2026-10-14; such old URL parameters are ignored).

Ranking, always over all time (owner 2026-10-14: no period switch): completed
requests ↓ (credited ones — the photo report approved, ``reward_pending=False``), then the volunteer's average
client rating ↓ (``Profile.review_avg``: visible reviews, all time — the
denormalised value, so the task COUNT and the review AVG don't share a join and
inflate each other), then the number of reviews ↓, then user id (stable).
No "kindness points": ``Profile.rating`` is not shown or used here.

Who is ranked: active volunteers (``is_volunteer``) whose volunteer
application, if they have one, is not pending or rejected (owner 2026-10-14) —
a volunteer created by an admin / ``create_initial_production_accounts`` without
an application counts. (The About page's network *counts* are stricter:
services/network.py.)

One ranking query, cached (``RATING_CACHE_KEY``) with the time it was built
("updated N minutes ago"); cleared by
``accounts.models.clear_rating_cache`` (task completion via ``add_points``,
review stats, application approve/reject). Everything viewer-specific ("You",
the profile button) is decided at render time — never cached.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.cache import cache
from django.db.models import Count, F, Q
from django.utils import timezone

from accounts.models import RATING_CACHE_KEY, REGION_CHOICES, Users
from myapp.models import VolunteerApplication

WEEK = timedelta(days=7)
PODIUM_SIZE = 3
PAGE_SIZE = 20
MAX_ROWS = 2000  # response-size bound, like the map caps
TTL = 300  # seconds; the clears above make a change show at once


def ranked_volunteers():
    """Who may appear in the ranking (see the module docstring)."""
    return Users.objects.filter(is_active=True, is_volunteer=True).exclude(
        volunteer_application__status__in=[VolunteerApplication.STATUS_PENDING, VolunteerApplication.STATUS_REJECTED],
    )


def build() -> dict:
    now = timezone.now()
    # A completed request counts once its photo report is approved
    # (reward_pending cleared by services/photo_reports.credit_task).
    done = Q(volunteer_tasks__status="completed", volunteer_tasks__reward_pending=False)
    users = (
        ranked_volunteers()
        .select_related("profile")
        .annotate(
            completed=Count("volunteer_tasks", filter=done),
            week=Count("volunteer_tasks", filter=done & Q(volunteer_tasks__completed_at__gte=now - WEEK)),
        )
        .order_by(
            "-completed", F("profile__review_avg").desc(nulls_last=True), "-profile__review_count", "id",
        )[:MAX_ROWS]
    )
    rows = []
    for rank, user in enumerate(users, start=1):
        profile = getattr(user, "profile", None)
        name = (profile.full_name if profile else "") or user.username
        avg = profile.review_avg if profile else None
        rows.append({
            "rank": rank,
            "user_id": user.pk,
            "profile": profile,
            "name": name,
            "initials": "".join(word[0] for word in name.split()[:2]).upper(),
            "region": user.region or "",
            "region_label": dict(REGION_CHOICES).get(user.region, ""),
            "completed": user.completed,
            "week": user.week,
            "avg": float(avg) if avg is not None else None,
            "reviews": profile.review_count if profile else 0,
            "joined": user.date_joined,
        })
    return {"computed_at": now, "rows": rows}


def cached() -> dict:
    """``{"computed_at", "rows"}`` — the ranking (one query when cold)."""
    return cache.get_or_set(RATING_CACHE_KEY, build, TTL)


def podium(rows) -> list:
    """Places 1–3 for the podium — only volunteers with at least one
    completed request (fewer than three → as many as there are; nobody → no
    podium). The table continues right after them."""
    return [row for row in rows[:PODIUM_SIZE] if row["completed"] > 0]


def top(n) -> list:
    """The first ``n`` with at least one completed request (About leaders)."""
    return [row for row in cached()["rows"][:n] if row["completed"] > 0]
