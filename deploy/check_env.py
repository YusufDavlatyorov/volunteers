"""Does the production .env say what the services will actually run with?

Two parsers read /opt/khayrkhoh/.env: systemd (``EnvironmentFile=`` in the
units) and python-dotenv (``server/settings.py`` → used by cron and
``manage.py``). Their quoting rules differ in corner cases, and the value
systemd sets wins inside a service (load_dotenv never overrides). So this
script is run *through systemd* with the same EnvironmentFile, and compares:

    sudo systemd-run --quiet --wait --pipe --collect \\
        -p User=deploy -p EnvironmentFile=/opt/khayrkhoh/.env -p WorkingDirectory=/opt/khayrkhoh \\
        /opt/khayrkhoh/.venv/bin/python deploy/check_env.py

(deploy/deploy.sh does exactly this.) It also checks the production
expectations of the bare-metal setup — local PostgreSQL/Redis, manifest
static files, X-Accel chat photos. Prints variable NAMES only, never values.
Exit code 0 = OK, 1 = a problem listed.
"""

import os
import sys
from pathlib import Path

TRUE = {"1", "true", "yes"}
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
# Redis databases: 0 cache/sessions/rate limits, 1 Celery queue, 2 Channels.
REDIS_URLS = {
    "DJANGO_REDIS_URL": "0",
    "CELERY_BROKER_URL": "1",
    "DJANGO_CHANNEL_REDIS_URL": "2",
}
OBSOLETE = ("SHARED_POSTGRES_NETWORK",)


def _is_local_redis(url, db):
    for host in LOCAL_HOSTS:
        prefix = f"redis://{host}:6379/"
        if url.startswith(prefix) and url[len(prefix):] == db:
            return True
    return False


def parse_mismatches(dotenv_values, environ):
    """Names whose value systemd set differently from python-dotenv (or not at all)."""
    return sorted(
        name for name, value in dotenv_values.items()
        if value is not None and environ.get(name) != value
    )


def production_problems(values):
    """``values``: the effective environment (name → value). Returns (errors, notes)."""
    get = lambda name: (values.get(name) or "").strip()  # noqa: E731
    errors, notes = [], []
    if get("DJANGO_DEBUG").lower() in TRUE | {"on"}:
        errors.append("DJANGO_DEBUG must be False in production")
    if not get("DJANGO_SECRET_KEY"):
        errors.append("DJANGO_SECRET_KEY is empty")
    if not get("DJANGO_DB_NAME"):
        errors.append("DJANGO_DB_NAME is empty (the site would run on SQLite)")
    if get("DJANGO_DB_HOST") not in LOCAL_HOSTS:
        errors.append("DJANGO_DB_HOST must be 127.0.0.1 (local PostgreSQL)")
    for name, db in REDIS_URLS.items():
        if not _is_local_redis(get(name), db):
            errors.append(f"{name} must be redis://127.0.0.1:6379/{db}")
    for name in ("DJANGO_STATIC_MANIFEST", "DJANGO_MEDIA_X_ACCEL", "DJANGO_BEHIND_TLS_PROXY"):
        if get(name).lower() not in TRUE:
            errors.append(f"{name} must be True")
    if get("DJANGO_DB_CACHE").lower() in TRUE | {"on"}:
        notes.append("DJANGO_DB_CACHE=True is ignored while DJANGO_REDIS_URL is set — set it to False")
    if get("DJANGO_DB_CONN_MAX_AGE"):
        notes.append("DJANGO_DB_CONN_MAX_AGE is set; khayrkhoh-web forces 0 anyway (deploy/gunicorn.conf.py)")
    for name in OBSOLETE:
        if name in values:
            notes.append(f"{name} is a Docker-era leftover and unused — delete the line")
    return errors, notes


def main():
    from dotenv import dotenv_values

    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.is_file():
        print(f"FAIL: {env_file} not found")
        return 1
    parsed = dotenv_values(env_file)

    errors = []
    if os.environ.get("INVOCATION_ID"):  # set by systemd for units and systemd-run
        for name in parse_mismatches(parsed, os.environ):
            errors.append(f"{name}: systemd reads a different value than python-dotenv "
                          "(quotes, spaces, '$' or a backslash in the value?)")
        effective = {**{k: v for k, v in parsed.items() if v is not None}, **os.environ}
    else:
        print("note: not run through systemd-run — only the production checks (see the docstring)")
        effective = {k: v for k, v in parsed.items() if v is not None}

    prod_errors, notes = production_problems(effective)
    errors += prod_errors
    for line in notes:
        print(f"note: {line}")
    for line in errors:
        print(f"FAIL: {line}")
    if errors:
        return 1
    print(f"OK: .env is consistent ({len(parsed)} variables; names only were checked/printed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
