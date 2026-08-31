"""Template context processors for myapp."""

from .services import maps


def maps_config(request):
    """Exposes the current provider's Leaflet tile config to every template.
    base.html renders it with `{{ maps_tile_config|json_script:"gc-maps-tile" }}`
    and static/js/map.js reads that element instead of hard-coding a tile URL.

    Only the public tile URL + attribution are exposed (for `osm` there is no
    secret; `mapbox` raster tiles carry a public-scoped token in the URL by
    design). Falls back to an empty dict if the provider is misconfigured, so a
    bad `MAPS_PROVIDER` never 500s an unrelated page — map.js then uses its
    built-in OSM fallback.
    """
    try:
        config = maps.tile_layer()
    except Exception:  # noqa: BLE001 — never let map config break page rendering
        config = {}
    return {"maps_tile_config": config}
