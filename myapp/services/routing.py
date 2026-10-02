"""Volunteer → client routes and turn-by-turn navigation (Feature Pack v2, feature 2).

A thin layer over ``maps.route()`` (``geo.py``'s import surface stays
test-locked): driving or walking profile, OSRM steps, a short cache, an ETA
that also covers the walking fallback, and "open in navigator" deep links.

Contract (FEATURES_PLAN §12) — ``route(origin, dest, *, profile, steps)``
never raises and always returns the ``maps.route`` dict plus:

- ``profile``    — ``"driving"`` | ``"walking"`` (unknown values → driving);
- ``steps``      — ``[{maneuver, modifier, name, distance_m, duration_s,
  location:[lat, lng], exit?}]`` with raw OSRM maneuver codes; the frontend
  turns them into ``nav.*`` i18n keys (static/js/map.js ``GCMap.stepText``);
- ``eta_min``    — OSRM duration when routed; for the walking fallback a
  straight-line estimate at ``WALKING_SPEED_KMH`` (``eta_estimated: True``);
  ``None`` for the driving fallback (same as before this feature);
- ``deep_links`` — ``{google, yandex_maps, yandex_navi, dgis}`` to the
  destination (apps start from the device's own position).

Only successful routes are cached (``ROUTE_CACHE_SECONDS``), keyed by the origin
rounded to 3 dp (≈ 100 m), the destination and the profile — so a volunteer
moving along the route re-uses one OSRM call, while an OSRM outage is retried
on the next request instead of being pinned for two minutes.
"""

from __future__ import annotations

import copy
import logging

from django.core.cache import cache

from . import maps

logger = logging.getLogger(__name__)

__all__ = [
    "PROFILES", "DEFAULT_PROFILE", "WALKING_SPEED_KMH", "ROUTE_CACHE_SECONDS",
    "normalize_profile", "route", "deep_links", "cache_key",
]

PROFILES = maps.ROUTE_PROFILES
DEFAULT_PROFILE = "driving"
WALKING_SPEED_KMH = 4.5
ROUTE_CACHE_SECONDS = 120
_CACHE_PREFIX = "route:v1"


def normalize_profile(value) -> str:
    value = (value or "").strip().lower() if isinstance(value, str) else ""
    return value if value in PROFILES else DEFAULT_PROFILE


def cache_key(origin, destination, profile, steps=True) -> str:
    o_lat, o_lng = (round(float(c), 3) for c in origin)
    d_lat, d_lng = (round(float(c), 5) for c in destination)
    return f"{_CACHE_PREFIX}:{profile}:{int(bool(steps))}:{o_lat},{o_lng}:{d_lat},{d_lng}"


def _fmt(value) -> str:
    return f"{float(value):.6f}".rstrip("0").rstrip(".")


def deep_links(destination, profile=DEFAULT_PROFILE) -> dict:
    """External navigator links to ``destination`` (lat, lng). Each app routes
    from the device's current position, so no origin (and no volunteer
    location) ever goes into a URL."""
    lat, lng = _fmt(destination[0]), _fmt(destination[1])
    walking = profile == "walking"
    return {
        "google": (
            "https://www.google.com/maps/dir/?api=1"
            f"&destination={lat},{lng}&travelmode={'walking' if walking else 'driving'}"
        ),
        # rtext "~lat,lng" = from my location to lat,lng; rtt auto | pd (pedestrian).
        "yandex_maps": f"https://yandex.ru/maps/?rtext=~{lat},{lng}&rtt={'pd' if walking else 'auto'}",
        # Yandex Navigator is car-only; its scheme opens the app when installed.
        "yandex_navi": f"yandexnavi://build_route_on_map?lat_to={lat}&lon_to={lng}",
        # 2GIS takes lng,lat.
        "dgis": f"https://2gis.ru/routeSearch/rsType/{'pedestrian' if walking else 'car'}/to/{lng},{lat}",
    }


def _decorate(result, destination, profile):
    result["profile"] = profile
    result.setdefault("steps", [])
    if result.get("success") and result.get("duration_min") is not None:
        result["eta_min"] = result["duration_min"]
        result["eta_estimated"] = False
    elif profile == "walking" and result.get("distance_km") is not None:
        result["eta_min"] = round(result["distance_km"] / WALKING_SPEED_KMH * 60, 1)
        result["eta_estimated"] = True
    else:
        result["eta_min"] = None
        result["eta_estimated"] = False
    result["deep_links"] = deep_links(destination, profile) if destination is not None else {}
    return result


def route(origin, destination, *, profile=DEFAULT_PROFILE, steps=True) -> dict:
    """Route + navigation data between two (lat, lng) tuples. Never raises."""
    profile = normalize_profile(profile)
    if not (maps.is_valid_coordinate(*origin) and maps.is_valid_coordinate(*destination)):
        return _decorate(
            {"success": False, "distance_km": None, "duration_min": None, "geometry": [], "steps": []},
            destination if maps.is_valid_coordinate(*destination) else None,
            profile,
        )
    origin = (float(origin[0]), float(origin[1]))
    destination = (float(destination[0]), float(destination[1]))

    key = cache_key(origin, destination, profile, steps)
    try:
        cached = cache.get(key)
    except Exception:  # noqa: BLE001 — a cache outage must not break routing
        cached = None
    if cached is not None:
        result = copy.deepcopy(cached)
        result["cached"] = True
        return result

    try:
        result = dict(maps.route(origin, destination, profile=profile, steps=steps))
    except Exception:  # noqa: BLE001 — maps.route never raises; belt and braces
        logger.exception("routing: maps.route raised (profile=%s)", profile)
        result = {"success": False, "distance_km": None, "duration_min": None, "geometry": []}
    result = _decorate(result, destination, profile)

    if result.get("success"):
        try:
            cache.set(key, result, ROUTE_CACHE_SECONDS)
        except Exception:  # noqa: BLE001
            logger.warning("routing: cache set failed")
    result = copy.deepcopy(result)
    result["cached"] = False
    return result
