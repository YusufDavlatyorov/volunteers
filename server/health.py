"""Dependency-free operational health endpoints.

Two levels, both public (they expose nothing sensitive — no versions, no config
values, no exception text) and both cheap enough for nginx / systemd / an uptime
monitor to poll every few seconds:

  GET /health/        liveness  — "the Django process is up and can render a
                                  response". Zero I/O. Always 200 unless the
                                  process is actually broken.
  GET /health/ready/  readiness — "this instance can serve production traffic":
                                  the database answers, the schema in it is the
                                  one this code expects (every migration applied,
                                  none applied that the code doesn't know, every
                                  model table and column present), and (only
                                  when a shared cache is configured) the cache
                                  round-trips. 200 when every check passes, 503
                                  otherwise.

External services (Groq, Telegram, OSRM, Nominatim) are deliberately NOT part of
readiness — the app degrades gracefully without them (see services.geo /
services.maps / notifications), so a routing-provider outage must not pull this
instance out of the load balancer.
"""

import functools
import logging

from django.apps import apps
from django.conf import settings
from django.db import connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
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


# Apps whose migrations live in this repo: a migration of theirs recorded in
# the database but missing on disk means the database is *ahead* of this code
# (e.g. the old image started without rolling the migrations back first).
_PROJECT_APPS = ("accounts", "myapp")


def _check_migrations():
    """The same question as ``migrate --check``, plus the reverse direction:
    the database must have every migration on disk applied, and no project-app
    migration applied that this code doesn't have. One SELECT on
    django_migrations; the graph comes from the already-imported modules
    (≈ 1 ms)."""
    try:
        executor = MigrationExecutor(connections["default"])
        loader = executor.loader
        unapplied = executor.migration_plan(loader.graph.leaf_nodes())
        unknown = [key for key in loader.applied_migrations
                   if key[0] in _PROJECT_APPS and key not in loader.disk_migrations]
    except Exception as exc:  # noqa: BLE001
        logger.warning("readiness: migration check failed: %r", exc)
        return False
    if unapplied or unknown:
        logger.warning(
            "readiness: migrations out of sync: %d unapplied (first: %s), %d applied but not in this code (%s)",
            len(unapplied), f"{unapplied[0][0].app_label}.{unapplied[0][0].name}" if unapplied else "-",
            len(unknown), ", ".join(".".join(key) for key in sorted(unknown)[:5]) or "-",
        )
        return False
    return True


@functools.cache
def _expected_schema():
    """``((table, (columns…)), …)`` for every concrete, managed model (incl. M2M
    through tables) of an app that has migrations. Fixed for the life of the
    process, so built once."""
    migrated = MigrationLoader(None, ignore_no_migrations=True).migrated_apps
    tables = []
    for model in apps.get_models(include_auto_created=True):
        meta = model._meta
        if meta.app_label not in migrated or not meta.managed or meta.proxy or meta.swapped:
            continue
        tables.append((meta.db_table, tuple(field.column for field in meta.local_concrete_fields)))
    return tuple(tables)


def _identifier_quoter(connection):
    """SQLite (dev, tests) reads an unknown *double-quoted* name as a string
    literal, so a dropped column would pass the probe silently; a [bracketed]
    name is always an identifier. PostgreSQL has no such fallback."""
    if connection.vendor == "sqlite":
        return lambda name: f"[{name}]"
    return connection.ops.quote_name


def _probe_sql(quote, table, columns):
    cols = ", ".join(quote(column) for column in columns)
    return f"SELECT 1 FROM (SELECT {cols} FROM {quote(table)} WHERE 1 = 0) AS probe"


def _check_schema():
    """Every model table exists with every model column — catches what the
    migration table can't: a half-restored dump, a faked or hand-edited
    migration, a table dropped by hand. One statement (a ``WHERE 1 = 0``
    probe per table, ``UNION ALL``): the database resolves every name but
    reads no rows. Only on failure are the tables probed one by one, to name
    the broken one in the log. Each probe runs in its own atomic block so a
    failed statement never poisons the connection (PostgreSQL)."""
    connection = connections["default"]
    quote = _identifier_quoter(connection)
    expected = ()
    try:
        expected = _expected_schema()
        sql = " UNION ALL ".join(_probe_sql(quote, table, columns) for table, columns in expected)
        with transaction.atomic(using="default"), connection.cursor() as cursor:
            cursor.execute(sql)
            cursor.fetchall()
        return True
    except Exception as exc:  # noqa: BLE001
        broken = []
        for table, columns in expected:
            try:
                with transaction.atomic(using="default"), connection.cursor() as cursor:
                    cursor.execute(_probe_sql(quote, table, columns))
            except Exception:  # noqa: BLE001
                broken.append(table)
        # Table names only (they are in the public repo) — never the error text
        # of a row, never a value.
        logger.warning("readiness: schema check failed: %s; tables: %s",
                       type(exc).__name__, ", ".join(broken[:10]) or "?")
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
    """Ready to serve traffic: DB reachable and migrated to exactly this code's
    schema, secret key present, and the shared cache (if any) round-trips. 503
    on any failure so an orchestrator / LB can route around this instance.
    Check results are booleans only — no error text, no config values."""
    database_ok = _check_database()
    checks = {"database": "ok" if database_ok else "error"}
    if database_ok:
        checks["migrations"] = "ok" if _check_migrations() else "error"
        checks["schema"] = "ok" if _check_schema() else "error"
    else:  # no point waiting on two more connection timeouts
        checks["migrations"] = checks["schema"] = "skipped"
    checks["config"] = "ok" if getattr(settings, "SECRET_KEY", "") else "error"
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
