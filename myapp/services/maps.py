"""Provider-agnostic maps service layer.

Everything the product needs from a "maps API" — tile config for the frontend,
routing, and geocoding — goes through here, so switching providers is a single
`.env` change (`MAPS_PROVIDER`) rather than a code hunt.

Providers:
    osm     (default) — free: Leaflet + OpenStreetMap tiles, OSRM routing
                        (``OSRM_BASE_URL``), Nominatim geocoding. No API key.
    mapbox            — raster tiles via ``MAPS_API_KEY``; routing/geocoding
                        currently fall through to the osm backends.
    google           — not a Leaflet raster drop-in; raises ImproperlyConfigured
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
    "route",
    "geocode",
    "reverse_geocode",
    "haversine_km",
    "is_valid_coordinate",
]

_NOMINATIM_BASE = "https://nominatim.openstreetmap.org"
_REGION_LABELS = dict(REGION_CHOICES)

_OSM_TILE = {
    "url": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    "attribution": '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    "max_zoom": 19,
    "subdomains": "abc",
}


def provider_name():
    return (getattr(settings, "MAPS_PROVIDER", "osm") or "osm").strip().lower()


def _timeout():
    return getattr(settings, "GEOCODING_TIMEOUT_SECONDS", 5)


def tile_layer():
    """Leaflet tile-layer config for the current provider. Frontend reads this
    (see myapp/context_processors.py -> window.GC_MAPS_TILE)."""
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
            "MAPS_PROVIDER=google is not a Leaflet raster drop-in; it needs the "
            "Maps JS SDK integration (not implemented). Use 'osm' or 'mapbox'."
        )
    if provider != "osm":
        logger.warning("Unknown MAPS_PROVIDER=%r; falling back to 'osm'.", provider)
    return dict(_OSM_TILE)


def route(origin, destination):
    """Driving route between two (lat, lng) tuples. Same contract as
    geo.get_route: always a dict, never raises. Non-osm providers currently
    reuse the OSRM backend."""
    return _osm_route(origin, destination)


def _nominatim(path, params):
    try:
        response = requests.get(
            f"{_NOMINATIM_BASE}/{path}",
            params={**params, "format": "jsonv2"},
            headers={"User-Agent": getattr(settings, "NOMINATIM_USER_AGENT", "generation-connect")},
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
