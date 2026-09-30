"""Cache-busting URLs for CSS/JS.

nginx serves /static/ with a 7-day browser cache and the filenames aren't
content-hashed, so a changed stylesheet or script could stay stale in
visitors' browsers for a week. `{% asset 'css/style.css' %}` returns the
normal static URL plus `?v=<content hash>`: a changed file gets a new URL,
an unchanged one keeps its cache.
"""

import hashlib
from functools import lru_cache
from pathlib import Path

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()


@lru_cache(maxsize=256)
def _digest(path, mtime):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:10]


def asset_version(relative_path):
    found = finders.find(relative_path)
    if not found:
        return ""
    return _digest(found, Path(found).stat().st_mtime)


@register.simple_tag
def asset(relative_path):
    url = static(relative_path)
    version = asset_version(relative_path)
    return f"{url}?v={version}" if version else url
