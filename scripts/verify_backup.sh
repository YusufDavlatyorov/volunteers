#!/usr/bin/env bash
# Verify a production backup copied to the Mac (deploy/MIGRATION.md, step 2).
#
#   scripts/verify_backup.sh ~/khayrkhoh-backups/pre-migration-2026-10-07-0830
#
# 1. checksums match the SHA256SUMS written on the server (the copy is byte-identical);
# 2. media.tgz is a readable gzip/tar archive;
# 3. db.dump really restores: a throw-away PostgreSQL 16 cluster (same major
#    version as production) is created in a temp directory, pg_restore runs
#    into an empty database and the key tables are counted. The cluster listens
#    only on a private Unix socket (no TCP port) and is deleted afterwards;
#    nothing is sent anywhere.
#
# Needs Homebrew's PostgreSQL 16 once:  brew install postgresql@16
# (keg-only — nothing is started or added to PATH; this script finds it).
# Other location: PG_BIN=/path/to/postgresql-16/bin scripts/verify_backup.sh …
set -euo pipefail

DIR="${1:?usage: $0 <backup directory>}"
PG_BIN="${PG_BIN:-$(brew --prefix postgresql@16 2>/dev/null)/bin}"
[[ -x "$PG_BIN/pg_restore" ]] || { echo "FAIL: PostgreSQL 16 not found — brew install postgresql@16 (or set PG_BIN)"; exit 1; }
"$PG_BIN/pg_restore" --version | grep -q " 16\." || { echo "FAIL: $PG_BIN is not PostgreSQL 16"; exit 1; }

cd "$DIR"
for f in db.dump media.tgz SHA256SUMS; do
  [[ -s "$f" ]] || { echo "FAIL: $f is missing or empty"; exit 1; }
done

echo "== 1. checksums"
shasum -a 256 -c SHA256SUMS

echo "== 2. media archive"
gzip -t media.tgz
echo "OK: $(tar -tzf media.tgz | grep -vc '/$') files, $(du -h media.tgz | cut -f1) compressed"

echo "== 3. database dump (test restore into a temporary PostgreSQL 16)"
# /tmp, not $TMPDIR: a Unix socket path must stay under ~100 characters on macOS.
TMP=$(mktemp -d /tmp/kk-verify.XXXXXX)
PGOPTS=(-h "$TMP" -p 54329 -U postgres)
cleanup() {
  "$PG_BIN/pg_ctl" -D "$TMP/data" -m immediate stop >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT
"$PG_BIN/initdb" -D "$TMP/data" -U postgres --auth=trust -E UTF8 --no-locale >/dev/null
"$PG_BIN/pg_ctl" -D "$TMP/data" -l "$TMP/server.log" -w \
  -o "-p 54329 -k $TMP -c listen_addresses=''" start >/dev/null
"$PG_BIN/createdb" "${PGOPTS[@]}" restore
"$PG_BIN/pg_restore" --list db.dump >/dev/null
"$PG_BIN/pg_restore" "${PGOPTS[@]}" --no-owner --no-privileges --exit-on-error -d restore db.dump
"$PG_BIN/psql" "${PGOPTS[@]}" -d restore -At -F ' | ' -c "
  SELECT 'users', count(*)::text FROM accounts_users
  UNION ALL SELECT 'help requests', count(*)::text FROM myapp_helprequest
  UNION ALL SELECT 'applied migrations', count(*)::text FROM django_migrations
  UNION ALL SELECT 'last migration applied', max(applied)::text FROM django_migrations;"
echo "OK: the dump restores and the tables are readable."
