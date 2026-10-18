"""The public volunteer rating (/rating/) and the About page's leaders.

**Places again** (owner 2026-10-14) — this reverses the 2026-10-03/09 rule
"one list by name, no places, no points". There is ONE national ranking, shown
as a podium (places 1–3) and a table from place 4 — no search, region filter
or re-sorting (removed 2026-10-14; such old URL parameters are ignored).

Ranking, always over all time (owner 2026-10-14: no period switch; the merit
score 2026-10-18), one deterministic ORDER BY of ORM annotations:

1. ``score`` ↓ — merit points, built on the earlier order (completed requests
   first, then the client rating, then the number of reviews):
   ``TASK_POINTS`` × completed requests (credited ones — the photo report
   approved, ``reward_pending=False``)
   + the client rating weighted by its number of reviews: a Bayesian average
   (``PRIOR_AVG`` counted ``PRIOR_REVIEWS`` times next to the visible reviews,
   ``Profile.review_avg`` / ``review_count`` — the denormalised values, so the
   task COUNT and the reviews don't share a join), ``RATING_POINTS`` per star
   above / below ``PRIOR_AVG``, rounded half up — one 5★ review can't outweigh a long
   4.8★ record, and a poor rating costs points
   + ``BADGE_POINTS`` × achievements earned automatically (a subquery, no
   join; a hand-given badge — ``Achievement.is_manual``, e.g. mentor — is
   shown on the card but earns no points);
2. a profile photo ↓ — **only between equal scores**: a photo never adds points;
3. profile completeness ↓ (``COMPLETENESS_FIELDS``: name, about, age, skills,
   location — 0…5);
4. user id ↑ (stable, so pages never lose or repeat anyone).

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
from django.db.models import Case, Count, F, FloatField, IntegerField, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import Cast, Coalesce, Floor
from django.utils import timezone

from accounts.models import RATING_CACHE_KEY, REGION_CHOICES, Users
from myapp.models import UserAchievement, VolunteerApplication

WEEK = timedelta(days=7)
PODIUM_SIZE = 3
PAGE_SIZE = 20
MAX_ROWS = 2000  # response-size bound, like the map caps
TTL = 300  # seconds; the clears above make a change show at once

# The merit score (module docstring) — every weight in one place.
TASK_POINTS = 10
RATING_POINTS = 10
PRIOR_AVG = 4.0
PRIOR_REVIEWS = 3
BADGE_POINTS = 2
# Profile completeness (the second tie-break): each filled field counts 1.
COMPLETENESS_FIELDS = (
    ~Q(profile__full_name=""),
    ~Q(profile__bio=""),
    Q(profile__age__isnull=False),
    ~Q(profile__skills=[]),
    Q(profile__latitude__isnull=False, profile__longitude__isnull=False),
)


def ranked_volunteers():
    """Who may appear in the ranking (see the module docstring)."""
    return Users.objects.filter(is_active=True, is_volunteer=True).exclude(
        volunteer_application__status__in=[VolunteerApplication.STATUS_PENDING, VolunteerApplication.STATUS_REJECTED],
    )


def _flag(q):
    return Case(When(q, then=Value(1)), default=Value(0), output_field=IntegerField())


def ranked_queryset(now=None):
    """The ranking as one annotated, ordered queryset (``score``,
    ``has_photo``, ``completeness``, ``completed``, ``week``, ``badges``)."""
    now = now or timezone.now()
    # A completed request counts once its photo report is approved
    # (reward_pending cleared by services/photo_reports.credit_task).
    done = Q(volunteer_tasks__status="completed", volunteer_tasks__reward_pending=False)
    badges = Subquery(
        # Automatic badges only: a hand-given one (mentor) is shown but earns no points.
        UserAchievement.objects.filter(user_id=OuterRef("pk"), achievement__is_manual=False).order_by()
        .values("user_id").annotate(n=Count("pk")).values("n")[:1],
        output_field=IntegerField(),
    )
    reviews = Coalesce(F("profile__review_count"), Value(0))
    average = Cast(Coalesce(F("profile__review_avg"), Value(0)), FloatField())
    weighted = (average * reviews + Value(PRIOR_AVG * PRIOR_REVIEWS)) / (reviews + Value(float(PRIOR_REVIEWS)))
    completeness = _flag(COMPLETENESS_FIELDS[0])
    for q in COMPLETENESS_FIELDS[1:]:
        completeness = completeness + _flag(q)
    return (
        ranked_volunteers()
        .select_related("profile")
        .annotate(
            completed=Count("volunteer_tasks", filter=done),
            week=Count("volunteer_tasks", filter=done & Q(volunteer_tasks__completed_at__gte=now - WEEK)),
            badges=Coalesce(badges, Value(0)),
            # floor(x + 0.5): the same half-up rounding on SQLite and PostgreSQL.
            rating_points=Cast(
                Floor((weighted - Value(PRIOR_AVG)) * Value(float(RATING_POINTS)) + Value(0.5)), IntegerField(),
            ),
        )
        .annotate(
            score=F("completed") * TASK_POINTS + F("rating_points") + F("badges") * BADGE_POINTS,
            has_photo=_flag(Q(profile__image__isnull=False) & ~Q(profile__image="")),
            completeness=completeness,
        )
        .order_by("-score", "-has_photo", "-completeness", "id")
    )


def build() -> dict:
    now = timezone.now()
    users = ranked_queryset(now)[:MAX_ROWS]
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
            "score": user.score,
            "has_photo": bool(user.has_photo),
            "completeness": user.completeness,
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
