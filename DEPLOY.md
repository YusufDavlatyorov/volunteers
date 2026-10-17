# DEPLOY.md — KhayrKhoh production bring-up on a fresh Ubuntu server

Exact command sequence to take a bare **Ubuntu 24.04** server to a running
production deployment at **https://khayrkhoh.tj** (and `www.khayrkhoh.tj`).
**No Docker** — everything runs directly on the host:

- **nginx** (TLS via certbot) serves `/static/` and `/media/` from disk and
  proxies everything else — pages and WebSockets (`/ws/`) — to
- **`khayrkhoh-web`**: gunicorn with uvicorn workers running the ASGI app
  (`server/asgi.py`) on `127.0.0.1:8000`;
- **`khayrkhoh-celery`**: background email/Telegram delivery and image variants;
- **`khayrkhoh-telegram-bot`** (and, once configured, `khayrkhoh-community-bot`);
- **PostgreSQL 16** and **Redis** from apt, both listening on localhost only;
- Python 3.14 in a venv at `/opt/khayrkhoh/.venv`; cron for the periodic sweeps.

All service/config files live in [`deploy/`](deploy/) and are copied into place
below. Moving the existing Docker-based server to this layout is a different,
one-time procedure: [`deploy/MIGRATION.md`](deploy/MIGRATION.md).

Run everything as the `deploy` user (group `sudo`) unless noted; SSH is key-only
(step 9). **`sudo` asks for the `deploy` user's password** — passwordless sudo is
deliberately not configured (owner decision 2026-10-05; don't add a sudoers rule).
`deploy/deploy.sh` therefore prompts for it once per run. No secrets are hardcoded anywhere in this repo or in this document —
every credential goes into `.env` on the server, which is never committed.
**Never run `seed_demo` (or `loaddata`, or any fixture) in production.**

---

## 1. Packages

```bash
sudo apt-get update && sudo apt-get -y upgrade
sudo apt-get install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa      # Python 3.14 — what the test suite runs on
# PostgreSQL from the PGDG repository (apt.postgresql.org) — same build as upstream's own
# packages, newer minor releases than Ubuntu's archive
sudo install -d /usr/share/postgresql-common/pgdg
sudo curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc
. /etc/os-release
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
  | sudo tee /etc/apt/sources.list.d/pgdg.list >/dev/null
sudo apt-get update
sudo apt-get install -y python3.14 python3.14-venv \
    postgresql-16 redis-server tmux \
    nginx libnginx-mod-http-brotli-filter certbot python3-certbot-nginx \
    git curl ufw cron
python3.14 -V && psql -V && redis-server -v && nginx -v
```

Ubuntu 24.04's own `python3` is 3.12; the project is tested on 3.14 only.

## 2. Clone the repo, create the venv

```bash
sudo mkdir -p /opt/khayrkhoh
sudo chown deploy:deploy /opt/khayrkhoh
git clone https://github.com/YusufDavlatyorov/volunteers.git /opt/khayrkhoh
cd /opt/khayrkhoh
python3.14 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p staticfiles media    # owned by deploy (the services' user); nginx (www-data) only reads them
```

`/opt/khayrkhoh` must stay world-readable (`755`) so nginx can serve
`staticfiles/` and `media/`.

## 3. Configure `.env`

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Fill in at minimum (`.env.example` documents every variable):

- `DJANGO_SECRET_KEY` — generate with
  `.venv/bin/python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"`
- `DJANGO_DEBUG=False`
- `DJANGO_ALLOWED_HOSTS=khayrkhoh.tj,www.khayrkhoh.tj`
- `DJANGO_CSRF_TRUSTED_ORIGINS=https://khayrkhoh.tj,https://www.khayrkhoh.tj`
- `DJANGO_BEHIND_TLS_PROXY=True` (nginx terminates TLS)
- `DJANGO_DB_NAME`, `DJANGO_DB_USER`, `DJANGO_DB_PASSWORD` — pick real values
  (used in step 4), `DJANGO_DB_HOST=127.0.0.1`, `DJANGO_DB_PORT=5432`
- `DJANGO_REDIS_URL=redis://127.0.0.1:6379/0`,
  `CELERY_BROKER_URL=redis://127.0.0.1:6379/1`,
  `DJANGO_CHANNEL_REDIS_URL=redis://127.0.0.1:6379/2`, `DJANGO_DB_CACHE=False`
- `DJANGO_STATIC_MANIFEST=True`, `DJANGO_MEDIA_X_ACCEL=True`
- `GROQ_API_KEY`, `TELEGRAM_BOT_TOKEN` + `TELEGRAM_BOT_USERNAME`,
  `SMTP_USER`/`SMTP_PASSWORD` — real values. A value containing spaces (e.g. a
  Gmail app password) must be single-quoted; avoid `$` and `\` in values.
- `INITIAL_ADMIN_EMAIL` / `INITIAL_ADMIN_PASSWORD` (+ curator/volunteer/client)
  for the 4 bootstrap accounts (step 6)

The file is read twice — by systemd (`EnvironmentFile=` in the units) and by
python-dotenv (`manage.py`, cron). Check that both see the same values and that
the production values above are right (prints variable names only):

```bash
sudo systemd-run --quiet --wait --pipe --collect \
  -p User=deploy -p EnvironmentFile=/opt/khayrkhoh/.env -p WorkingDirectory=/opt/khayrkhoh \
  /opt/khayrkhoh/.venv/bin/python deploy/check_env.py
```

## 4. PostgreSQL

```bash
sudo cp deploy/postgresql/khayrkhoh.conf /etc/postgresql/16/main/conf.d/khayrkhoh.conf
sudo systemctl restart postgresql
set -a; . ./.env; set +a
printf 'CREATE ROLE :"u" LOGIN PASSWORD :%s;\n' "'pw'" \
  | sudo -u postgres psql -v ON_ERROR_STOP=1 -v u="$DJANGO_DB_USER" -v pw="$DJANGO_DB_PASSWORD"
sudo -u postgres createdb -O "$DJANGO_DB_USER" -E UTF8 -T template0 \
  --lc-collate=C.UTF-8 --lc-ctype=C.UTF-8 "$DJANGO_DB_NAME"
PGPASSWORD="$DJANGO_DB_PASSWORD" psql -h 127.0.0.1 -U "$DJANGO_DB_USER" -d "$DJANGO_DB_NAME" -c "select 1"
```

The app role owns its database and is **not** a superuser. The tuning file is
sized for the 3.8 GB host (512 MB shared buffers, 8 MB work_mem, 50
connections) — the comments in it explain the budget.

## 5. Redis

```bash
sudo cp deploy/redis/khayrkhoh.conf /etc/redis/khayrkhoh.conf
echo 'include /etc/redis/khayrkhoh.conf' | sudo tee -a /etc/redis/redis.conf
sudo systemctl restart redis-server
redis-cli ping                       # PONG
redis-cli config get maxmemory-policy   # volatile-lru
```

DB 0 = cache, cached sessions and rate limits; DB 1 = Celery queue (append-only
file, so queued notifications survive a restart); DB 2 = Channels layer.

## 6. Static files, migrations, the 4 initial accounts

```bash
.venv/bin/python manage.py collectstatic --noinput
.venv/bin/python manage.py migrate --noinput
.venv/bin/python manage.py create_initial_production_accounts
.venv/bin/python manage.py check --deploy
```

`create_initial_production_accounts` is idempotent and creates exactly 1 admin,
1 curator, 1 volunteer, 1 client from the `INITIAL_*` vars. It is the **only**
way accounts get onto a production database; `seed_demo` refuses to run with
`DEBUG=False` and must never be forced there.

## 7. systemd services

```bash
sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now khayrkhoh-web khayrkhoh-celery khayrkhoh-telegram-bot
systemctl --no-pager status 'khayrkhoh-*' | grep -E "^●|Active:"
curl -fsS -H "Host: khayrkhoh.tj" -H "X-Forwarded-Proto: https" http://127.0.0.1:8000/health/ready/
```

`khayrkhoh-community-bot` stays disabled until the community bot is configured
(docs/COMMUNITY_BOT.md): `sudo systemctl enable --now khayrkhoh-community-bot`.
Logs: `journalctl -u khayrkhoh-web -f` (likewise `-celery`, `-telegram-bot`).
The Telegram bot is a long-polling process: only **one** copy per token may run
(a second gets `409 Conflict`).

## 8. nginx + HTTPS (certbot)

First a plain-HTTP site so certbot can issue the certificate:

```bash
sudo tee /etc/nginx/sites-available/khayrkhoh >/dev/null <<'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name khayrkhoh.tj www.khayrkhoh.tj;
    location / { proxy_pass http://127.0.0.1:8000; proxy_set_header Host $host; }
}
EOF
sudo ln -sf /etc/nginx/sites-available/khayrkhoh /etc/nginx/sites-enabled/khayrkhoh
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

Once DNS for `khayrkhoh.tj` **and** `www.khayrkhoh.tj` resolves to this server
(`dig +short A khayrkhoh.tj @8.8.8.8`):

```bash
sudo certbot --nginx -d khayrkhoh.tj -d www.khayrkhoh.tj --redirect
sudo certbot renew --dry-run   # renewal runs from certbot's systemd timer
```

Then the final config — [`deploy/nginx/khayrkhoh.conf`](deploy/nginx/khayrkhoh.conf):
`https://khayrkhoh.tj` as the one canonical host (HTTP and `www` 301 there),
gzip + brotli, year-long immutable cache for hashed `/static/` names, `/media/`
for 30 days with `nosniff`, `/ws/` upgraded to WebSocket, private chat and donation photos
(`/media/chat/`, `/media/donations/` → 404, served only via the `internal` `/protected-media/`):

```bash
sudo cp deploy/nginx/khayrkhoh.conf /etc/nginx/sites-available/khayrkhoh
sudo nginx -t && sudo systemctl reload nginx
```

`X-Forwarded-Proto` is what `DJANGO_BEHIND_TLS_PROXY=True` trusts — without it
Django sees every request as plain HTTP and redirect-loops. `X-Real-IP` is how
the rate limits see the visitor (server/client_ip.py).

## 9. Firewall + SSH hardening

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

Nothing else needs to be open — PostgreSQL, Redis and gunicorn all listen on
loopback only.

After confirming `ssh deploy@<server>` works with your key, disable password
logins (a `00-` drop-in wins over any later file that sets the same option):

```bash
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin prohibit-password\n' \
  | sudo tee /etc/ssh/sshd_config.d/00-hardening.conf
sudo sshd -t && sudo systemctl reload ssh
```

## 10. Cron, log limits

All 9 periodic jobs are idempotent management commands run from the venv
([`deploy/crontab.example`](deploy/crontab.example) — overdue/stale alerts,
availability sweep, retention purges, community jobs). Times are UTC.

```bash
sudo install -d -o deploy -g deploy /var/log/khayrkhoh
crontab deploy/crontab.example
crontab -l | grep -c manage.py     # 9
```

Logs never fill the disk: the services log to journald, capped at 500 MB / one
month; `cron.log` and nginx's logs rotate with a size cap too.

```bash
sudo install -d /etc/systemd/journald.conf.d
sudo cp deploy/journald/khayrkhoh.conf /etc/systemd/journald.conf.d/
sudo systemctl restart systemd-journald
sudo cp deploy/logrotate/khayrkhoh /etc/logrotate.d/khayrkhoh
sudo cp deploy/logrotate/nginx /etc/logrotate.d/nginx
sudo logrotate --debug /etc/logrotate.d/khayrkhoh /etc/logrotate.d/nginx 2>&1 | grep -i error
```

## 11. Verify

```bash
curl -fsS https://khayrkhoh.tj/health/
curl -fsS https://khayrkhoh.tj/health/ready/
curl -sI http://khayrkhoh.tj/ | head -3        # 301 → https
.venv/bin/python manage.py check_realtime      # channels_redis… / round trip: OK
curl -sk -o /dev/null -w "%{http_code}\n" --http1.1 -m 5 \
  -H "Connection: Upgrade" -H "Upgrade: websocket" -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" -H "Origin: https://khayrkhoh.tj" \
  https://khayrkhoh.tj/ws/                     # 101 (curl exits 28 after 5 s — expected)
systemctl --no-pager status 'khayrkhoh-*' postgresql redis-server nginx | grep -E "^●|Active:"
```

Expect `{"status":"ready",...}` with HTTP 200. Also log in with the admin account
from step 6, open the dashboard and the map; DevTools → Network → WS shows one
`/ws/` socket (101) with `hello`/`pong` frames. Finally `sudo reboot` once and
check that everything comes back by itself (all units are `enabled`).

### Compression check (gzip / brotli)

Step 8's config compresses HTML, CSS, JS, JSON and SVG (`gzip on` + `brotli on`,
`gzip_proxied any` so the HTML from the app is compressed too). Checked from
outside on 2026-10-03:

```bash
JS=$(curl -s https://khayrkhoh.tj/ | grep -o '/static/js/i18n[^"]*' | head -1)
for enc in br gzip identity; do
  curl -s -o /dev/null -H "Accept-Encoding: $enc" -D - "https://khayrkhoh.tj$JS" \
    -w "$enc: %{size_download} bytes\n" | grep -i "content-encoding\|bytes"
done
curl -s -o /dev/null -H "Accept-Encoding: br" -D - https://khayrkhoh.tj/ | grep -i content-encoding
```

Expected (2026-10-03): `i18n.js` 120 KB raw → 34 KB gzip → 29 KB brotli; `style.css`
59 → 16 → 15 KB; the landing HTML 35 → 9 → 8 KB. If `content-encoding` is
missing: `sudo apt-get install -y libnginx-mod-http-brotli-filter`, make sure no
`gzip off;` in `/etc/nginx/nginx.conf` overrides it, then
`sudo nginx -t && sudo systemctl reload nginx`.

---

## Redeploying after a code change

```bash
/opt/khayrkhoh/deploy/deploy.sh
```

As `deploy`: `git pull --ff-only` → `pip install` → `.env` check through
systemd → changed unit files installed → `check --deploy` → **collectstatic
before migrate** (a static error then stops the redeploy before the schema
changes — the 2026-10-04 rehearsal hit exactly that) → `migrate` → restart the
services → wait for `/health/ready/` → read-only `check_demo_data`. It never
seeds anything. A changed nginx config is only reported — review the diff and
copy it by hand (step 8).

## Notes

- **`.env` is never touched by any of the above except step 3** — it is
  gitignored and stays only on the server / in your secret store.
- No `DATABASE_URL` variable exists in this project by design — PostgreSQL
  connection info is the discrete `DJANGO_DB_*` vars (see `.env.example`).
- Backup/restore, disaster recovery, and known production limitations are
  documented in README.md → "Backup & recovery".
