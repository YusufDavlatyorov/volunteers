#!/usr/bin/env bash
# Run Django tests against PostgreSQL 16 — production's database — on the Mac,
# before every deploy (CLAUDE.md "Before every deploy"). SQLite, the default for
# `manage.py test`, ignores what PostgreSQL enforces (SELECT … FOR UPDATE rules,
# varchar lengths), so a green SQLite run alone has shipped a production 500
# (2026-10-17, photo-report delete).
#
#   scripts/test_postgres.sh myapp.tests_photo_report_review accounts   # the changed parts
#   scripts/test_postgres.sh                                             # the whole suite (slow)
#
# A throw-away Homebrew postgresql@16 cluster (brew install postgresql@16):
# Unix socket only, no TCP port, deleted afterwards — the scripts/verify_backup.sh
# pattern. Never touches a real database. Serial on purpose: the parallel runner
# lost its cloned test databases on PostgreSQL (2026-10-17).
set -euo pipefail

PG_BIN=${PG_BIN:-/opt/homebrew/opt/postgresql@16/bin}
[[ -x "$PG_BIN/pg_ctl" ]] || { echo "PostgreSQL 16 not found at $PG_BIN — brew install postgresql@16" >&2; exit 1; }
cd "$(dirname "$0")/.."

# /tmp, not $TMPDIR: a Unix socket path must stay under ~100 characters on macOS.
TMP=$(mktemp -d /tmp/kk-pgtest.XXXXXX)
cleanup() {
  "$PG_BIN/pg_ctl" -D "$TMP/data" -m immediate stop >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT
"$PG_BIN/initdb" -D "$TMP/data" -U postgres --auth=trust -E UTF8 --no-locale >/dev/null
"$PG_BIN/pg_ctl" -D "$TMP/data" -l "$TMP/server.log" -w \
  -o "-p 54329 -k $TMP -c listen_addresses=''" start >/dev/null

# The DJANGO_DB_* variables win over .env (python-dotenv does not override them).
DJANGO_DB_NAME=khayrkhoh_test DJANGO_DB_USER=postgres DJANGO_DB_PASSWORD= \
DJANGO_DB_HOST="$TMP" DJANGO_DB_PORT=54329 \
  .venv/bin/python manage.py test --noinput --parallel 1 "$@"
