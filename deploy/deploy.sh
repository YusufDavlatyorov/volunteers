#!/usr/bin/env bash
# KhayrKhoh — routine redeploy on the server (bare metal: venv + systemd).
#
#   ssh deploy@<server>
#   /opt/khayrkhoh/deploy/deploy.sh            # pulls origin/main
#
# Run as the `deploy` user; sudo is used only to check .env through systemd, to
# install changed unit files and to restart the units. Order (each step stops the script on failure, and
# nothing is restarted until all of them passed):
#   1. git pull --ff-only          — never merges, never touches local files
#   2. pip install into .venv      — exact pins from requirements.txt
#   3. .env check through systemd  — deploy/check_env.py, names only;
#      changed unit files from deploy/systemd/ are installed (nginx: only a warning)
#   4. manage.py check --deploy
#   5. collectstatic               — BEFORE migrate: a static error stops the
#                                    redeploy before the schema changes (the
#                                    2026-10-04 rehearsal hit exactly that)
#   6. migrate
#   7. restart the services, wait for /health/ready/ (= DB, migrations, schema, cache)
#   8. check_demo_data             — read-only audit, deletes nothing
#
# It NEVER seeds anything: no seed_demo, no loaddata, no fixtures — demo data
# must never reach production. The first move off Docker is deploy/MIGRATION.md,
# not this script.
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/khayrkhoh}
BRANCH=${BRANCH:-main}
SITE_HOST=${SITE_HOST:-khayrkhoh.tj}
PYTHON_BIN=${PYTHON_BIN:-python3.14}
SERVICES=(khayrkhoh-web khayrkhoh-celery khayrkhoh-telegram-bot)

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31mFAIL: %s\033[0m\n' "$*" >&2; exit 1; }

[[ "$(id -un)" == deploy ]] || die "run as the deploy user (sudo -iu deploy)"
cd "$APP_DIR"
[[ -f .env ]] || die "$APP_DIR/.env is missing"
PY="$APP_DIR/.venv/bin/python"
manage() { "$PY" manage.py "$@"; }

# The community bot runs only once the owner enabled it (docs/COMMUNITY_BOT.md).
if systemctl is-enabled --quiet khayrkhoh-community-bot 2>/dev/null; then
  SERVICES+=(khayrkhoh-community-bot)
fi

step "1/8 code: origin/$BRANCH"
OLD=$(git rev-parse --short HEAD)
git fetch --quiet origin "$BRANCH"
git merge --ff-only "origin/$BRANCH"
NEW=$(git rev-parse --short HEAD)
echo "$OLD -> $NEW"

step "2/8 dependencies"
[[ -x "$PY" ]] || "$PYTHON_BIN" -m venv .venv
"$PY" -m pip install --quiet --disable-pip-version-check -r requirements.txt

step "3/8 .env (systemd vs python-dotenv, production values)"
sudo systemd-run --quiet --wait --pipe --collect \
  -p User=deploy -p EnvironmentFile="$APP_DIR/.env" -p WorkingDirectory="$APP_DIR" \
  "$PY" deploy/check_env.py

step "3b/8 systemd units and nginx config in sync with deploy/"
changed=()
for unit in deploy/systemd/*.service; do
  cmp -s "$unit" "/etc/systemd/system/$(basename "$unit")" || changed+=("$unit")
done
if ((${#changed[@]})); then
  echo "updating: ${changed[*]}"
  sudo install -m 644 "${changed[@]}" /etc/systemd/system/
  sudo systemctl daemon-reload
fi
# nginx is only reported, never replaced automatically: a config error takes the whole site down.
cmp -s deploy/nginx/khayrkhoh.conf /etc/nginx/sites-available/khayrkhoh \
  || echo "WARNING: /etc/nginx/sites-available/khayrkhoh differs from deploy/nginx/khayrkhoh.conf — review: sudo diff /etc/nginx/sites-available/khayrkhoh deploy/nginx/khayrkhoh.conf"

step "4/8 django checks"
manage check --deploy

step "5/8 collectstatic"
manage collectstatic --noinput | tail -n 1

step "6/8 migrate"
manage migrate --noinput

step "7/8 restart: ${SERVICES[*]}"
sudo systemctl restart "${SERVICES[@]}"
ready=""
for _ in $(seq 1 30); do
  # Straight to gunicorn: the Host nginx would send, and the proto header
  # DJANGO_BEHIND_TLS_PROXY trusts (else SECURE_SSL_REDIRECT answers 301).
  if curl -fsS -m 5 -H "Host: $SITE_HOST" -H "X-Forwarded-Proto: https" \
       http://127.0.0.1:8000/health/ready/ >/dev/null 2>&1; then
    ready=1; break
  fi
  sleep 2
done
systemctl --no-pager --lines=0 status "${SERVICES[@]}" | grep -E "^●|Active:" || true
[[ -n "$ready" ]] || die "/health/ready/ is not 200 after 60 s — journalctl -u khayrkhoh-web -n 50 (the reason is one 'readiness:' WARNING line)"
echo "/health/ready/: 200"

step "8/8 demo-data audit (read-only)"
if ! manage check_demo_data; then
  die "demo data found — delete NOTHING, show this output to the owner (the site is up on $NEW)"
fi

step "done: $NEW is live"
