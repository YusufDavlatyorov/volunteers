"""Geo utilities: distance math and the OSRM routing client.

Pure input/output — no ORM access — so it can be unit-tested (and the OSRM
call mocked) without a database or network access. Views are responsible for
permission checks and pulling coordinates out of models; this module only
ever sees plain (lat, lng) numbers.
"""

import logging
import math

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

OSRM_TIMEOUT_SECONDS = 6
EARTH_RADIUS_KM = 6371.0088


def is_valid_coordinate(lat, lng):
    """True if lat/lng (numbers or numeric strings) are valid WGS84 values."""
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return False
    return -90 <= lat <= 90 and -180 <= lng <= 180


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km between two WGS84 points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return EARTH_RADIUS_KM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_route(origin, destination):
    """Fetch a driving route from OSRM between two (lat, lng) tuples.

    Always returns a dict and never raises: routing being unavailable (OSRM
    down, timeout, no route found, OSRM_BASE_URL unset) must not break a
    request that embeds it — callers get a straight-line distance fallback
    and an empty geometry instead of a 500.

    Success:  {"success": True,  "distance_km": float, "duration_min": float, "geometry": [[lat, lng], ...]}
    Fallback: {"success": False, "distance_km": <haversine>, "duration_min": None, "geometry": []}
    """
    origin_lat, origin_lng = origin
    dest_lat, dest_lng = destination
    fallback = {
        "success": False,
        "distance_km": round(haversine_km(origin_lat, origin_lng, dest_lat, dest_lng), 2),
        "duration_min": None,
        "geometry": [],
    }

    base_url = (getattr(settings, "OSRM_BASE_URL", "") or "").rstrip("/")
    if not base_url:
        return fallback

    url = f"{base_url}/route/v1/driving/{origin_lng},{origin_lat};{dest_lng},{dest_lat}"
    try:
        response = requests.get(
            url,
            params={"overview": "full", "geometries": "geojson"},
            timeout=OSRM_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("OSRM route request failed for %s: %s", url, exc)
        return fallback

    if data.get("code") != "Ok" or not data.get("routes"):
        logger.warning("OSRM returned no usable route (code=%s) for %s", data.get("code"), url)
        return fallback

    route = data["routes"][0]
    # GeoJSON coordinates are [lng, lat]; Leaflet wants [lat, lng].
    coordinates = route.get("geometry", {}).get("coordinates", [])
    geometry = [[lat, lng] for lng, lat in coordinates]

    return {
        "success": True,
        "distance_km": round(route["distance"] / 1000, 2),
        "duration_min": round(route["duration"] / 60, 1),
        "geometry": geometry,
    }
