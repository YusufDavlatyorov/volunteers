"""Dependency-free operational health endpoints.

Two levels, both public (they expose nothing sensitive — no versions, no config
values, no exception text) and both cheap enough for nginx / systemd / an uptime
monitor to poll every few seconds:

  GET /health/        liveness  — "the Django process is up and can render a
                                  response". Zero I/O. Always 200 unless the
                                  process is actually broken.
  GET /health/ready/  readiness — "this instance can serve production traffic":
                                  the database answers, and (only when a shared
                                  cache is configured) the cache round-trips.
                                  200 when every check passes, 503 otherwise.

External services (Groq, Telegram, OSRM, Nominatim) are deliberately NOT part of
readiness — the app degrades gracefully without them (see services.geo /
services.maps / notifications), so a routing-provider outage must not pull this
instance out of the load balancer.
"""

import logging

from django.conf import settings
from django.db import connections
from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)

_CACHE_PROBE_KEY = "healthcheck:probe"


def _no_store(response):
    response["Cache-Control"] = "no-store"
    return response


@require_GET
def liveness_view(request):
    """Process is alive. No database, no cache, no template — just proves the
    WSGI worker can execute a view and return."""
    return _no_store(JsonResponse({"status": "ok"}))


def _check_database():
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return True
    except Exception as exc:  # noqa: BLE001 — any DB error means "not ready"
        # One line, no stack trace: this can be polled every few seconds and a
        # traceback per poll would flood the log during an outage. DB connection
        # errors don't carry the password.
        logger.warning("readiness: database check failed: %r", exc)
        return False


def _shared_cache_configured():
    backend = settings.CACHES.get("default", {}).get("BACKEND", "")
    return "locmem" not in backend.lower() and "dummy" not in backend.lower()


def _check_cache():
    """Only meaningful when a shared cache backend is configured — the default
    per-process LocMemCache is always 'up' and tells us nothing about readiness."""
    if not _shared_cache_configured():
        return None
    try:
        cache.set(_CACHE_PROBE_KEY, "1", 10)
        return cache.get(_CACHE_PROBE_KEY) == "1"
    except Exception as exc:  # noqa: BLE001
        logger.warning("readiness: cache check failed: %r", exc)
        return False


@require_GET
def readiness_view(request):
    """Ready to serve traffic: DB reachable, secret key present, and the shared
    cache (if any) round-trips. 503 on any failure so an orchestrator / LB can
    route around this instance. Check results are booleans only — no error text,
    no config values."""
    checks = {
        "database": "ok" if _check_database() else "error",
        "config": "ok" if getattr(settings, "SECRET_KEY", "") else "error",
    }
    cache_ok = _check_cache()
    if cache_ok is not None:
        checks["cache"] = "ok" if cache_ok else "error"

    ready = all(value == "ok" for value in checks.values())
    body = {"status": "ready" if ready else "not ready", "checks": checks}
    response = _no_store(JsonResponse(body, status=200 if ready else 503))
    if not ready:
        # A not-ready 503 is normal operation (the LB routes around this
        # instance) — the per-check logger.warning above is the real signal, so
        # suppress Django's automatic "Service Unavailable" request-logger line
        # that would otherwise repeat on every poll.
        response._has_been_logged = True
    return response
