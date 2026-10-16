"""The About page's "volunteer network" section: five regions on a map.

The map and the region names are public. The **number of volunteers per
region** is not: it reaches the context (and so the HTML / the map JSON) only
for a viewer whose access ``access_level()`` returns ``GRANTED`` — an
approved volunteer (``VolunteerApplication.status == approved``, owner
2026-10-14: strictly the application, a volunteer flag without one is not
enough), a curator or an admin. Clients are not granted: anyone can register as
a client without a review, so showing them the numbers would be showing them to
everyone. The counts are cached once for everybody (``NETWORK_CACHE_KEY``) and
read only after the access check, so a cache can't leak them.
"""

from __future__ import annotations

from django.core.cache import cache
from django.db.models import Count

from accounts.models import ABOUT_NETWORK_CACHE_KEY as NETWORK_CACHE_KEY
from accounts.models import REGION_CHOICES, Users
from myapp.models import VolunteerApplication

NETWORK_TTL = 300  # seconds, like the landing stats

GRANTED = "granted"
LOGIN = "login"      # anonymous: sign in first
PENDING = "pending"  # the volunteer application is still under review
DENIED = "denied"    # signed in, but not an approved volunteer / staff (clients, rejected, …)

# Map centre of each region (lat, lng) — its main city — the zoom a click
# flies to, and which side of the point its label sits on (Dushanbe and
# Vahdat are ~20 km apart, Bokhtar ~80 km south of them: at country zoom the
# labels would overlap without this).
REGION_CENTERS = {
    "dushanbe": {"lat": 38.5598, "lng": 68.7870, "zoom": 10.5, "anchor": "bottom-right"},  # Dushanbe
    "sogd": {"lat": 40.2826, "lng": 69.6221, "zoom": 8.5, "anchor": "bottom"},             # Khujand
    "khatlon": {"lat": 37.8364, "lng": 68.7803, "zoom": 8.5, "anchor": "top"},             # Bokhtar
    "gbao": {"lat": 37.4897, "lng": 71.5530, "zoom": 8, "anchor": "bottom"},               # Khorog
    "rrp": {"lat": 38.5563, "lng": 69.0144, "zoom": 9.5, "anchor": "bottom-left"},         # Vahdat
}
# [[west, south], [east, north]] — the whole country in view at first.
TAJIKISTAN_BOUNDS = [[67.34, 36.67], [75.15, 41.04]]


def _application_status(user):
    try:
        return user.volunteer_application.status
    except VolunteerApplication.DoesNotExist:
        return None


def access_level(user) -> str:
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return LOGIN
    if user.is_superuser or user.is_curator:
        return GRANTED
    status = _application_status(user)
    if user.is_volunteer and status == VolunteerApplication.STATUS_APPROVED:
        return GRANTED
    if status == VolunteerApplication.STATUS_PENDING:
        return PENDING
    return DENIED


def volunteer_counts() -> dict[str, int]:
    """``{region: n}`` for every region (0 included): active volunteers with an
    approved application, by their region. One query."""
    rows = (
        Users.objects.filter(
            is_active=True, is_volunteer=True,
            volunteer_application__status=VolunteerApplication.STATUS_APPROVED,
        )
        .values("region").annotate(n=Count("id")).order_by()
    )
    found = {row["region"]: row["n"] for row in rows}
    return {key: found.get(key, 0) for key, _label in REGION_CHOICES}


def cached_volunteer_counts() -> dict[str, int]:
    return cache.get_or_set(NETWORK_CACHE_KEY, volunteer_counts, NETWORK_TTL)


def for_user(user) -> dict:
    """The section's context. ``regions`` / ``map.points`` carry ``count`` and
    ``total`` / ``covered`` exist only when access is GRANTED — for anyone
    else the numbers are never read, not merely hidden."""
    access = access_level(user)
    counts = cached_volunteer_counts() if access == GRANTED else None
    regions, points = [], []
    for key, label in REGION_CHOICES:
        center = REGION_CENTERS[key]
        region = {"key": key, "label": label}
        point = {"key": key, **center}
        if counts is not None:
            region["count"] = point["count"] = counts[key]
        regions.append(region)
        points.append(point)
    # ``map`` is what the page's JSON (json_script) hands to about-network.js.
    data = {"access": access, "regions": regions, "map": {"points": points, "bounds": TAJIKISTAN_BOUNDS}}
    if counts is not None:
        data["total"] = sum(counts.values())
        data["covered"] = sum(1 for n in counts.values() if n)
    return data
