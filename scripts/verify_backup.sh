#!/usr/bin/env bash
# Verify a production backup copied to the Mac (docs/DEPLOY_FEATURE_PACK_V2.md, step 2).
#
#   scripts/verify_backup.sh ~/khayrkhoh-backups/pre-fp2-2026-10-07-0830
#
# 1. checksums match the SHA256SUMS written on the server (the copy is byte-identical);
# 2. media.tgz is a readable gzip/tar archive;
# 3. db.dump really restores: a throw-away postgres:16 container (same major
#    version as shared_postgres) runs pg_restore into an empty database and the
#    key tables are counted. The container is removed afterwards; nothing is
#    sent anywhere. Needs Docker Desktop running.
set -euo pipefail

DIR="${1:?usage: $0 <backup directory>}"
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
NAME="kk-verify-$$"
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker run -d --name "$NAME" -e POSTGRES_PASSWORD=verify -e POSTGRES_DB=restore \
  -v "$PWD/db.dump:/backup/db.dump:ro" postgres:16-alpine >/dev/null
for _ in $(seq 1 30); do
  docker exec "$NAME" pg_isready -U postgres -d restore >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$NAME" pg_restore --list /backup/db.dump >/dev/null
docker exec "$NAME" pg_restore --no-owner --no-privileges --exit-on-error -U postgres -d restore /backup/db.dump
docker exec "$NAME" psql -U postgres -d restore -At -F ' | ' -c "
  SELECT 'users', count(*)::text FROM accounts_users
  UNION ALL SELECT 'help requests', count(*)::text FROM myapp_helprequest
  UNION ALL SELECT 'applied migrations', count(*)::text FROM django_migrations
  UNION ALL SELECT 'last migration applied', max(applied)::text FROM django_migrations;"
echo "OK: the dump restores and the tables are readable."
