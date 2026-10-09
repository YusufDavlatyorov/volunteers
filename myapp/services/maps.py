"""Provider-agnostic maps service layer.

Everything the product needs from a "maps API" — tile config for the frontend,
routing, and geocoding — goes through here, so switching providers is a single
`.env` change (`MAPS_PROVIDER`) rather than a code hunt.

Providers:
    osm     (default) — free: OpenStreetMap raster tiles, OSRM routing
                        (``OSRM_BASE_URL``), Nominatim geocoding. No API key.
    mapbox            — raster tiles via ``MAPS_API_KEY``; routing/geocoding
                        currently fall through to the osm backends.
    google           — not a raster drop-in; raises ImproperlyConfigured
                        (needs the Maps JS SDK — a later phase).

Like ``geo.py`` this module is pure I/O (no ORM) and never raises for an
expected failure: a geocode miss returns ``None``, a routing failure returns the
straight-line fallback dict. Network calls go through ``requests.get`` so tests
mock ``myapp.services.maps.requests.get``.
"""

import logging

import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from accounts.models import REGION_CHOICES
from .geo import get_route as _osm_route
from .geo import haversine_km, is_valid_coordinate  # re-exported: single import point for new code

logger = logging.getLogger(__name__)

__all__ = [
    "provider_name",
    "tile_layer",
    "vector_style",
    "routing_provider",
    "normalize_preference",
    "route",
    "geocode",
    "reverse_geocode",
    "haversine_km",
    "is_valid_coordinate",
]

_NOMINATIM_BASE = "https://nominatim.openstreetmap.org"
_REGION_LABELS = dict(REGION_CHOICES)

# OSM's tile servers want one host (no a/b/c subdomains since 2024) and a
# Referer — settings.SECURE_REFERRER_POLICY ("strict-origin-when-cross-origin")
# sends our origin with cross-site tile requests; "same-origin" sent none,
# which OSM's usage policy may answer with 403 "Access blocked".
_OSM_TILE = {
    "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    "attribution": '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    "max_zoom": 19,
    "subdomains": "",
}

# Keyless fallback the frontend switches to when the primary provider's tiles
# keep failing (static/js/map.js tileLayer). CARTO's free raster basemaps (OSM
# data) — free for non-commercial use like this NGO's; attribution required.
_CARTO_TILE = {
    "url": "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
    "attribution": (
        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors '
        '&copy; <a href="https://carto.com/attributions">CARTO</a>'
    ),
    "max_zoom": 20,
    "subdomains": "abcd",
}


def provider_name():
    return (getattr(settings, "MAPS_PROVIDER", "osm") or "osm").strip().lower()


def _timeout():
    return getattr(settings, "GEOCODING_TIMEOUT_SECONDS", 5)


# Vector basemap for the MapLibre GL maps (static/js/map.js): 3D buildings
# (fill-extrusion on OpenMapTiles' ``render_height``), tilt and rotation.
# OpenFreeMap is keyless and free; checked 2026-10-09 that its z14 tiles carry
# building footprints for Dushanbe and Khujand (2–4k per tile; most without an
# OSM height, so they extrude to the 5 m default, a few tagged up to ~66 m).
# The raster ``tile_layer()`` stays as the fallback style if this one fails.
_OPENFREEMAP_STYLE = "https://tiles.openfreemap.org/styles/liberty"


def vector_style():
    """``{"url", "provider"}`` of the vector style, or ``{}`` for raster only.

    ``MAPS_VECTOR_STYLE``: ``openfreemap`` (default, no key) | ``maptiler``
    (needs ``MAPTILER_API_KEY`` — a browser-side key by design: restrict it to
    this site's origin in the MapTiler console) | ``off``."""
    choice = (getattr(settings, "MAPS_VECTOR_STYLE", "openfreemap") or "openfreemap").strip().lower()
    if choice in ("off", "none", "raster"):
        return {}
    if choice == "maptiler":
        key = getattr(settings, "MAPTILER_API_KEY", "")
        if key:
            return {
                "url": f"https://api.maptiler.com/maps/streets-v2/style.json?key={key}",
                "provider": "maptiler",
            }
        logger.warning("MAPS_VECTOR_STYLE=maptiler but MAPTILER_API_KEY is empty; using OpenFreeMap.")
    elif choice != "openfreemap":
        logger.warning("Unknown MAPS_VECTOR_STYLE=%r; using OpenFreeMap.", choice)
    return {"url": _OPENFREEMAP_STYLE, "provider": "openfreemap"}


def tile_layer():
    """Raster tile config for the current provider (the MapLibre maps' fallback
    style if the vector style fails), plus a keyless
    ``fallback`` config the frontend switches to if the provider's tiles keep
    failing. Frontend reads this (see myapp/context_processors.py)."""
    config = _primary_tile_layer()
    if config.get("url") != _CARTO_TILE["url"]:
        config["fallback"] = dict(_CARTO_TILE)
    return config


def _primary_tile_layer():
    provider = provider_name()
    if provider == "mapbox":
        key = getattr(settings, "MAPS_API_KEY", "")
        if not key:
            raise ImproperlyConfigured("MAPS_PROVIDER=mapbox requires MAPS_API_KEY.")
        return {
            "url": (
                "https://api.mapbox.com/styles/v1/mapbox/streets-v12/tiles/512/"
                "{z}/{x}/{y}@2x?access_token=" + key
            ),
            "attribution": (
                '&copy; <a href="https://www.mapbox.com/about/maps/">Mapbox</a> '
                '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
            ),
            "max_zoom": 20,
            "subdomains": "",
        }
    if provider == "google":
        raise ImproperlyConfigured(
            "MAPS_PROVIDER=google is not a raster tile drop-in; it needs the "
            "Maps JS SDK integration (not implemented). Use 'osm' or 'mapbox'."
        )
    if provider == "carto":
        return dict(_CARTO_TILE)
    if provider != "osm":
        logger.warning("Unknown MAPS_PROVIDER=%r; falling back to 'osm'.", provider)
    return dict(_OSM_TILE)


ROUTE_PROFILES = ("driving", "walking")
# "fastest" = least time, "shortest" = least distance (ORS `preference`; on
# OSRM, which has no such switch, the pick among its alternatives).
ROUTE_PREFERENCES = ("fastest", "shortest")
DEFAULT_PREFERENCE = {"driving": "fastest", "walking": "shortest"}
MAX_ALTERNATIVES = 3
# Straight-line limit for a walking route: nobody walks to a client 40 km
# away, and ORS / OSRM-foot answer such requests slowly or with an error.
WALKING_MAX_KM = 40
# OSRM URL profile segment per product profile. A self-hosted osrm-routed
# ignores this segment (it serves whatever profile its data was built with),
# which is why walking needs its own server (OSRM_FOOT_BASE_URL). The public
# router.project-osrm.org is car-only: asked for "foot" it silently returns
# the car route (2.5 km "walked" in 5 min) — never use it for walking.
_OSRM_PROFILE_SEGMENT = {"driving": "driving", "walking": "foot"}
MAX_ROUTE_STEPS = 200
_MAX_STEP_NAME = 120

# Why a route has no road geometry (``reason`` in a failed result):
NO_ROUTE = "no_route"          # the router found no path (no roads / footpaths between the points)
TOO_FAR = "too_far"            # beyond WALKING_MAX_KM on foot, or the router's own distance limit
UNAVAILABLE = "unavailable"    # timeout, HTTP error, malformed answer
NOT_CONFIGURED = "not_configured"  # no backend for this profile


ROUTING_PROVIDERS = ("osrm", "ors")


def routing_provider(profile="driving"):
    """Which backend routes ``profile``.

    ``ROUTING_PROVIDER`` (``osrm`` | ``ors``); unset → ``ors`` when an
    ``ORS_API_KEY`` is configured, else ``osrm``. ``ors`` without a key falls
    back to ``osrm`` (logged) rather than failing every route. Walking always
    goes to ORS when a key exists, whatever ``ROUTING_PROVIDER`` says: OSRM
    walking needs its own foot-profile server (``OSRM_FOOT_BASE_URL``)."""
    explicit = (getattr(settings, "ROUTING_PROVIDER", "") or "").strip().lower()
    has_key = bool(getattr(settings, "ORS_API_KEY", ""))
    if explicit == "ors" and not has_key:
        logger.warning("ROUTING_PROVIDER=ors but ORS_API_KEY is empty; using OSRM.")
        return "osrm"
    if explicit and explicit not in ROUTING_PROVIDERS:
        logger.warning("Unknown ROUTING_PROVIDER=%r; using the default.", explicit)
    if not has_key:
        return "osrm"
    if explicit == "osrm" and profile != "walking":
        return "osrm"
    return "ors"


def normalize_preference(value, profile="driving"):
    value = value.strip().lower() if isinstance(value, str) else ""
    return value if value in ROUTE_PREFERENCES else DEFAULT_PREFERENCE.get(profile, "fastest")


def route(origin, destination, *, profile="driving", steps=False, preference=None, alternatives=False):
    """Route between two (lat, lng) tuples. Same contract as geo.get_route:
    always a dict, never raises.

    The plain driving call (no ``steps``, no alternatives, fastest) is exactly
    ``geo.get_route``. Otherwise the result also carries ``profile``,
    ``preference``, ``steps`` (``[{maneuver, modifier, name, distance_m,
    duration_s, location, exit?}]``, raw OSRM maneuver codes — the frontend
    localizes them) and ``routes``: up to ``MAX_ALTERNATIVES`` variants
    ``{distance_km, duration_min, geometry, steps}``, best-for-``preference``
    first; the top-level fields are ``routes[0]``. A failed result has
    ``reason`` (``no_route`` | ``too_far`` | ``unavailable`` | ``not_configured``).

    Backends: OpenRouteService (``routing_provider(profile) == "ors"``, key
    only ever sent server-side in a header) or OSRM (``OSRM_BASE_URL`` /
    ``OSRM_FOOT_BASE_URL``)."""
    if profile not in ROUTE_PROFILES:
        profile = "driving"
    preference = normalize_preference(preference, profile)
    provider = routing_provider(profile)
    if (provider == "osrm" and profile == "driving" and not steps and not alternatives
            and preference == "fastest"):
        return _osm_route(origin, destination)

    fallback = _route_fallback(origin, destination, profile, preference)
    if not (is_valid_coordinate(*origin) and is_valid_coordinate(*destination)):
        return fallback
    if profile == "walking" and fallback["distance_km"] is not None and fallback["distance_km"] > WALKING_MAX_KM:
        fallback["reason"] = TOO_FAR
        return fallback
    if provider == "ors":
        return _ors_route(origin, destination, profile=profile, steps=steps,
                          preference=preference, alternatives=alternatives, fallback=fallback)
    return _osrm_route_ex(origin, destination, profile=profile, steps=steps,
                          preference=preference, alternatives=alternatives, fallback=fallback)


def _rank(routes, preference):
    """Best-for-preference first; stable, so the router's own order breaks ties."""
    key = "distance_km" if preference == "shortest" else "duration_min"
    return sorted(routes, key=lambda r: (r[key] if r[key] is not None else float("inf")))


def _result(routes, profile, preference):
    routes = _rank(routes, preference)[:MAX_ALTERNATIVES]
    best = routes[0]
    return {
        "success": True,
        "distance_km": best["distance_km"],
        "duration_min": best["duration_min"],
        "geometry": best["geometry"],
        "steps": best["steps"],
        "profile": profile,
        "preference": preference,
        "routes": routes,
    }


# OpenRouteService ------------------------------------------------------------

_ORS_PROFILE = {"driving": "driving-car", "walking": "foot-walking"}
# ORS instruction type → OSRM (maneuver, modifier); see
# https://giscience.github.io/openrouteservice/api-reference/endpoints/directions/instruction-types
_ORS_STEP_TYPES = {
    0: ("turn", "left"), 1: ("turn", "right"),
    2: ("turn", "sharp left"), 3: ("turn", "sharp right"),
    4: ("turn", "slight left"), 5: ("turn", "slight right"),
    6: ("continue", "straight"),
    7: ("roundabout", ""), 8: ("exit roundabout", ""),
    9: ("turn", "uturn"), 10: ("arrive", ""), 11: ("depart", ""),
    12: ("fork", "slight left"), 13: ("fork", "slight right"),
}
# ORS error codes (api-reference/error-codes): 2009 route not found, 2010 a
# point has no routable road nearby, 2004 a request limit (distance — e.g.
# alternative_routes only below 100 km) was exceeded.
_ORS_NO_ROUTE = {2009, 2010}
_ORS_LIMIT = 2004
_ORS_ALTERNATIVES = {"target_count": MAX_ALTERNATIVES, "weight_factor": 1.6, "share_factor": 0.6}


def _ors_timeout():
    return getattr(settings, "ORS_TIMEOUT_SECONDS", 8)


def _ors_error_code(response):
    try:
        return int(((response.json() or {}).get("error") or {}).get("code"))
    except (ValueError, TypeError, AttributeError):
        return None


def _ors_route(origin, destination, *, profile, steps, preference="fastest", alternatives=False, fallback=None):
    """OpenRouteService directions (GeoJSON). Never raises; any failure →
    the straight-line fallback dict with a ``reason``."""
    fallback = fallback or _route_fallback(origin, destination, profile, preference)
    key = getattr(settings, "ORS_API_KEY", "")
    base_url = (getattr(settings, "ORS_BASE_URL", "") or "https://api.openrouteservice.org").rstrip("/")
    (o_lat, o_lng), (d_lat, d_lng) = origin, destination
    url = f"{base_url}/v2/directions/{_ORS_PROFILE[profile]}/geojson"
    body = {
        "coordinates": [[float(o_lng), float(o_lat)], [float(d_lng), float(d_lat)]],
        "instructions": bool(steps),
        "preference": preference,
        "units": "m",
    }
    if alternatives:
        body["alternative_routes"] = dict(_ORS_ALTERNATIVES)
    headers = {"Authorization": key, "Accept": "application/geo+json, application/json"}

    for attempt in range(2):
        try:
            response = requests.post(url, json=body, timeout=_ors_timeout(), headers=headers)
        except (requests.RequestException, ValueError) as exc:
            # Never the exception text: keep the log line uniform with OSRM's.
            logger.warning("ORS %s route request failed: %s", profile, exc.__class__.__name__)
            return dict(fallback, reason=UNAVAILABLE)
        status = getattr(response, "status_code", 200)
        if isinstance(status, int) and status >= 400:
            code = _ors_error_code(response)
            # Alternatives have a distance limit (100 km): retry once without.
            if code == _ORS_LIMIT and "alternative_routes" in body and attempt == 0:
                body.pop("alternative_routes")
                continue
            reason = NO_ROUTE if code in _ORS_NO_ROUTE else (TOO_FAR if code == _ORS_LIMIT else UNAVAILABLE)
            logger.warning("ORS %s route: HTTP %s, error code %s → %s", profile, status, code, reason)
            return dict(fallback, reason=reason)
        break
    try:
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("ORS %s route request failed: %s", profile, exc.__class__.__name__)
        return dict(fallback, reason=UNAVAILABLE)

    routes = []
    try:
        for feature in data["features"]:
            props = feature.get("properties") or {}
            summary = props.get("summary") or {}
            coordinates = (feature.get("geometry") or {}).get("coordinates") or []
            # ORS GeoJSON is [lng, lat]; the API speaks [lat, lng] (map.js flips back).
            geometry = [[float(c[1]), float(c[0])] for c in coordinates]
            if len(geometry) < 2:
                continue
            routes.append({
                "distance_km": round(float(summary.get("distance") or 0) / 1000, 2),
                "duration_min": round(float(summary.get("duration") or 0) / 60, 1),
                "geometry": geometry,
                "steps": _parse_ors_steps(props, geometry) if steps else [],
            })
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        logger.warning("ORS %s returned no usable route: %s", profile, exc.__class__.__name__)
        return dict(fallback, reason=UNAVAILABLE)
    if not routes:
        logger.warning("ORS %s returned no usable route: ValueError", profile)
        return dict(fallback, reason=UNAVAILABLE)
    logger.debug("ORS %s/%s: %d route(s), best %.2f km", profile, preference, len(routes), routes[0]["distance_km"])
    return _result(routes, profile, preference)


def _parse_ors_steps(props, geometry):
    """ORS segments[].steps[] → the same flat step list as ``_parse_steps``."""
    out = []
    for segment in props.get("segments") or []:
        for step in (segment or {}).get("steps") or []:
            try:
                maneuver, modifier = _ORS_STEP_TYPES.get(int(step.get("type")), ("continue", ""))
                first = int((step.get("way_points") or [0])[0])
                lat, lng = geometry[max(0, min(first, len(geometry) - 1))]
                name = str(step.get("name") or "")
                item = {
                    "maneuver": maneuver,
                    "modifier": modifier,
                    "name": "" if name == "-" else name[:_MAX_STEP_NAME],
                    "distance_m": round(float(step.get("distance") or 0), 1),
                    "duration_s": round(float(step.get("duration") or 0), 1),
                    "location": [round(lat, 6), round(lng, 6)],
                }
                if step.get("exit_number") is not None:
                    item["exit"] = int(step["exit_number"])
            except (TypeError, ValueError, IndexError):
                continue
            out.append(item)
            if len(out) >= MAX_ROUTE_STEPS:
                return out
    return out


def _osrm_base_url(profile):
    key = "OSRM_FOOT_BASE_URL" if profile == "walking" else "OSRM_BASE_URL"
    return (getattr(settings, key, "") or "").rstrip("/")


def _route_fallback(origin, destination, profile, preference=None):
    try:
        distance = round(haversine_km(float(origin[0]), float(origin[1]),
                                      float(destination[0]), float(destination[1])), 2)
    except (TypeError, ValueError, IndexError):
        distance = None
    return {
        "success": False, "distance_km": distance, "duration_min": None,
        "geometry": [], "steps": [], "profile": profile,
        "preference": normalize_preference(preference, profile), "routes": [],
    }


def _osrm_route_ex(origin, destination, *, profile, steps, preference="fastest", alternatives=False, fallback=None):
    """geo.get_route's twin with a profile, OSRM steps and alternatives. Never raises."""
    fallback = fallback or _route_fallback(origin, destination, profile, preference)
    if not (is_valid_coordinate(*origin) and is_valid_coordinate(*destination)):
        return fallback
    base_url = _osrm_base_url(profile)
    if not base_url:
        return dict(fallback, reason=NOT_CONFIGURED)

    (o_lat, o_lng), (d_lat, d_lng) = origin, destination
    segment = _OSRM_PROFILE_SEGMENT[profile]
    url = f"{base_url}/route/v1/{segment}/{float(o_lng)},{float(o_lat)};{float(d_lng)},{float(d_lat)}"
    params = {"overview": "full", "geometries": "geojson", "steps": "true" if steps else "false"}
    # OSRM has no "shortest" switch: ask for its alternatives and pick by distance.
    if alternatives or preference == "shortest":
        params["alternatives"] = str(MAX_ALTERNATIVES)
    try:
        response = requests.get(
            url, params=params, timeout=_osrm_timeout(),
            # The FOSSGIS servers (routing.openstreetmap.de) require an app User-Agent.
            headers={"User-Agent": getattr(settings, "NOMINATIM_USER_AGENT", "khayrkhoh")},
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("OSRM %s route request failed: %s", profile, exc.__class__.__name__)
        return dict(fallback, reason=UNAVAILABLE)

    code = data.get("code") if isinstance(data, dict) else None
    if code != "Ok" or not data.get("routes"):
        logger.warning("OSRM %s returned no usable route (code=%s)", profile, code)
        return dict(fallback, reason=NO_ROUTE if code in ("NoRoute", "NoSegment") else UNAVAILABLE)

    try:
        routes = []
        for item in data["routes"]:
            coordinates = (item.get("geometry") or {}).get("coordinates") or []
            routes.append({
                "distance_km": round(float(item["distance"]) / 1000, 2),
                "duration_min": round(float(item["duration"]) / 60, 1),
                "geometry": [[float(lat), float(lng)] for lng, lat, *_ in coordinates],
                "steps": _parse_steps(item) if steps else [],
            })
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("OSRM %s returned a malformed route: %s", profile, exc.__class__.__name__)
        return dict(fallback, reason=UNAVAILABLE)
    logger.debug("OSRM %s/%s: %d route(s), best %.2f km", profile, preference, len(routes), routes[0]["distance_km"])
    result = _result(routes, profile, preference)
    if not alternatives:
        result["routes"] = result["routes"][:1]
    return result


def _osrm_timeout():
    from .geo import OSRM_TIMEOUT_SECONDS

    return getattr(settings, "OSRM_TIMEOUT_SECONDS", OSRM_TIMEOUT_SECONDS)


def _parse_steps(osrm_route):
    """OSRM legs[].steps[] → a flat, whitelisted step list. Location flipped
    to [lat, lng]; malformed steps are skipped, never fatal."""
    out = []
    for leg in osrm_route.get("legs") or []:
        for step in (leg or {}).get("steps") or []:
            try:
                maneuver = step.get("maneuver") or {}
                lng, lat = maneuver["location"][:2]
                item = {
                    "maneuver": str(maneuver.get("type") or "continue")[:32],
                    "modifier": str(maneuver.get("modifier") or "")[:32],
                    "name": str(step.get("name") or "")[:_MAX_STEP_NAME],
                    "distance_m": round(float(step.get("distance") or 0), 1),
                    "duration_s": round(float(step.get("duration") or 0), 1),
                    "location": [round(float(lat), 6), round(float(lng), 6)],
                }
                if maneuver.get("exit") is not None:
                    item["exit"] = int(maneuver["exit"])
            except (KeyError, TypeError, ValueError, IndexError):
                continue
            out.append(item)
            if len(out) >= MAX_ROUTE_STEPS:
                return out
    return out


def _nominatim(path, params):
    base = getattr(settings, "NOMINATIM_BASE_URL", _NOMINATIM_BASE)
    if not base:  # geocoding off (always under `manage.py test`)
        return None
    try:
        response = requests.get(
            f"{base.rstrip('/')}/{path}",
            params={**params, "format": "jsonv2"},
            headers={"User-Agent": getattr(settings, "NOMINATIM_USER_AGENT", "khayrkhoh")},
            timeout=_timeout(),
        )
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Nominatim %s failed (%s): %s", path, params, exc)
        return None


def geocode(query, *, region=None):
    """Address string -> (lat, lng) tuple, or None if it can't be resolved.
    Never raises. Scoped to Tajikistan; the region's display name is appended
    to the query when given to disambiguate common street names."""
    query = (query or "").strip()
    if not query:
        return None
    if region and _REGION_LABELS.get(region):
        query = f"{query}, {_REGION_LABELS[region]}"

    data = _nominatim("search", {"q": query, "limit": 1, "countrycodes": "tj"})
    if not data:
        return None
    try:
        lat, lng = float(data[0]["lat"]), float(data[0]["lon"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    if not is_valid_coordinate(lat, lng):
        return None
    return round(lat, 6), round(lng, 6)


def reverse_geocode(lat, lng):
    """(lat, lng) -> a human-readable address string, or None. Never raises."""
    if not is_valid_coordinate(lat, lng):
        return None
    data = _nominatim("reverse", {"lat": lat, "lon": lng})
    if isinstance(data, dict) and data.get("display_name"):
        return data["display_name"]
    return None
