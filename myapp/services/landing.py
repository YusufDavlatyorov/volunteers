"""Data for the public About / landing page (``about_view``, also "/").

Everything the page shows comes from the database — no number, name or quote
is written into the template (test-locked in myapp/tests_about.py):

- ``stats``    — completed requests, active volunteers, regions with at least
                 one volunteer, the clients' average rating (visible reviews);
                 plus the hero badges: the latest completed request (region +
                 time) and how many requests were created today.
- ``regions``  — volunteers per region (the five ``REGION_CHOICES``), with a
                 label point for the map.
- ``volunteers`` — the first four of the public rating list
                 (``services/rating.py`` — the same list and order as the
                 Rating page; no places, owner decision 2026-10-10) with their
                 public review summary and completed-request count.
- ``testimonials`` — only reviews staff marked "can be published on the site"
                 (``feedback.publish``), visible and with a comment; the
                 reviewer as a first name + initial, never a username.

The whole payload is one cache entry (``ABOUT_CACHE_KEY``, 5 min): the page is
the most visited on the site, so a hit runs no queries. Review moderation
(publish / unpublish / hide / edit) calls ``invalidate()``.
"""

from datetime import datetime, time
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.db.models import Avg, Count, Q
from django.utils import timezone

from accounts.models import REGION_CHOICES, Profile, Users
from myapp.models import HelpRequest, TaskReview

from . import rating

ABOUT_CACHE_KEY = "about_page:v2"
ABOUT_TTL = 300  # seconds — public landing-page numbers, fine to be a few minutes old

LOCAL_TZ = ZoneInfo("Asia/Dushanbe")
VOLUNTEERS_SHOWN = 4
TESTIMONIALS_SHOWN = 3
HERO_QUOTE_MAX = 140

# Where each region's label sits on the About map (a representative town, not
# a boundary). RRP's is Gharm/Rasht rather than Hisor so its label doesn't
# cover Dushanbe's at country zoom.
REGION_POINTS = {
    "dushanbe": (38.5598, 68.7870),
    "sogd": (40.2839, 69.6220),     # Khujand
    "khatlon": (37.8300, 68.7800),  # Bokhtar
    "gbao": (37.4900, 71.5500),     # Khorog
    "rrp": (39.0200, 70.3700),      # Gharm (Rasht valley)
}


def invalidate():
    cache.delete(ABOUT_CACHE_KEY)


def page_data():
    """The cached payload (see the module docstring)."""
    return cache.get_or_set(ABOUT_CACHE_KEY, build, ABOUT_TTL)


def build():
    regions, total_volunteers = _regions()
    return {
        "stats": _stats(total_volunteers, sum(1 for r in regions if r["count"])),
        "regions": regions,
        "volunteers": _volunteers(),
        "testimonials": _testimonials(),
    }


def _regions():
    counts = {
        row["region"]: row["n"]
        for row in Users.objects.filter(is_volunteer=True, is_active=True).values("region").annotate(n=Count("id"))
    }
    regions = [
        {"key": key, "label": label, "count": counts.get(key, 0),
         "lat": REGION_POINTS[key][0], "lng": REGION_POINTS[key][1]}
        for key, label in REGION_CHOICES
    ]
    return regions, sum(counts.values())


def _today_start():
    """Midnight of today in Dushanbe (the platform's day)."""
    return datetime.combine(timezone.now().astimezone(LOCAL_TZ).date(), time.min, tzinfo=LOCAL_TZ)


def _stats(total_volunteers, regions_covered):
    tasks = HelpRequest.objects.aggregate(
        completed=Count("id", filter=Q(status="completed")),
        today=Count("id", filter=Q(created_at__gte=_today_start())),
    )
    reviews = TaskReview.objects.filter(hidden_at__isnull=True).aggregate(avg=Avg("stars"), count=Count("id"))
    last = (
        HelpRequest.objects.filter(status="completed", completed_at__isnull=False)
        .order_by("-completed_at")
        .values("region", "completed_at")
        .first()
    )
    avg = reviews["avg"]
    return {
        "completed": tasks["completed"],
        "volunteers": total_volunteers,
        "regions": regions_covered,
        "rating_avg": round(float(avg), 1) if avg is not None else None,
        "rating_count": reviews["count"],
        "today_requests": tasks["today"],
        "last_completed": last,
        # Nothing to count yet (an empty production DB): the page shows an
        # empty state instead of a row of zeros.
        "has_activity": bool(tasks["completed"] or total_volunteers),
    }


def _volunteers():
    profiles = rating.cached_rating_profiles()[:VOLUNTEERS_SHOWN]
    ids = [p.user_id for p in profiles]
    completed = {
        row["volunteer_id"]: row["n"]
        for row in HelpRequest.objects.filter(volunteer_id__in=ids, status="completed")
        .values("volunteer_id").annotate(n=Count("id"))
    } if ids else {}
    return [{"profile": p, "completed": completed.get(p.user_id, 0)} for p in profiles]


def short_name(full_name):
    """"Зарина Назарова" → "Зарина Н."; one word stays as is; "" → ""."""
    parts = (full_name or "").split()
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[1][0]}."


def _testimonials():
    reviews = (
        TaskReview.objects.filter(published_at__isnull=False, hidden_at__isnull=True)
        .exclude(comment="")
        .select_related("client__profile", "task")
        .order_by("-published_at", "-id")[:TESTIMONIALS_SHOWN]
    )
    items = []
    for review in reviews:
        try:
            full_name = review.client.profile.full_name
        except Profile.DoesNotExist:
            full_name = ""
        items.append({
            "id": review.pk,
            "stars": review.stars,
            "comment": review.comment,
            "name": short_name(full_name),
            "region": review.task.region or review.client.region,
        })
    return items


def hero_quote(testimonials):
    """The newest published testimonial, shortened for the hero photo; None
    when there is none (the photo then carries no quote — never a made-up one)."""
    if not testimonials:
        return None
    first = testimonials[0]
    text = first["comment"].strip()
    if len(text) > HERO_QUOTE_MAX:
        text = text[:HERO_QUOTE_MAX].rsplit(" ", 1)[0].rstrip(",.;:—- ") + "…"
    return {"text": text, "name": first["name"], "region": first["region"]}


def ago(moment, now=None):
    """``(i18n key, n)`` for "N min / h / days ago" — computed at render time
    from the cached timestamp, so the badge never freezes for 5 minutes."""
    if moment is None:
        return None
    seconds = max(0, int(((now or timezone.now()) - moment).total_seconds()))
    if seconds < 3600:
        return ("ab.ago_min", max(1, seconds // 60))
    if seconds < 86400:
        return ("ab.ago_hours", seconds // 3600)
    return ("ab.ago_days", seconds // 86400)
