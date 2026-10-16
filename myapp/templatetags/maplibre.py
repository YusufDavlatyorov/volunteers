"""MapLibre GL — the one place for its version, CDN URLs and SRI hashes.

``partials/_maplibre_css.html`` / ``_maplibre_js.html`` (map pages, loaded
while the page parses) and ``partials/_maplibre_lazy.html`` (the About page,
loaded only when its map scrolls into view — static/js/about-network.js) all
read them from here, so a version bump is one edit. unpkg first (fast CDN,
SRI-pinned), then the identical self-hosted copy under static/vendor/.

Load with ``{% load maplibre %}``: ``{% maplibre "js" %}`` → a URL / hash
(keys of ``MAPLIBRE``), ``{% maplibre_lazy_config as cfg %}`` → the dict the
lazy loader reads (render it with ``json_script``).
"""

from django import template

from .assets import asset

register = template.Library()

MAPLIBRE_VERSION = "5.24.0"
_CDN = f"https://unpkg.com/maplibre-gl@{MAPLIBRE_VERSION}/dist"
_LOCAL = f"vendor/maplibre-gl-{MAPLIBRE_VERSION}"

MAPLIBRE = {
    "js": f"{_CDN}/maplibre-gl.js",
    "js_sri": "sha384-5+cfbwT0iiub6VsQAdn6yz16nr6sDiQoHx6tm4O8OVYXHYOxcffFmCJBL0dgdvGp",
    "css": f"{_CDN}/maplibre-gl.css",
    "css_sri": "sha384-uTttxo/aOKbdE5RlD/SPzSDoDmNvGlUYPjONi2MN/b7c9HPSvW07OIuyP7uL6jxK",
    "js_local": f"{_LOCAL}/maplibre-gl.js",
    "css_local": f"{_LOCAL}/maplibre-gl.css",
}


@register.simple_tag
def maplibre(name):
    """A CDN URL / SRI hash, or the self-hosted copy's asset URL (``*_local``)."""
    value = MAPLIBRE[name]
    return asset(value) if name.endswith("_local") else value


@register.simple_tag
def maplibre_lazy_config():
    """Everything static/js/about-network.js needs to load MapLibre + map.js
    on demand, with the same SRI and the same self-hosted fallback."""
    return {
        **{key: maplibre(key) for key in MAPLIBRE},
        "map_js": asset("js/map.js"),
    }
