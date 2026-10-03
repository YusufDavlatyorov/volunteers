# DEPLOY.md — KhayrKhoh production bring-up on a fresh Ubuntu server

Exact command sequence to take a bare Ubuntu 22.04/24.04 server to a running
production deployment at **https://khayrkhoh.tj** (and `www.khayrkhoh.tj`).
Stack: host **nginx** (TLS via certbot) → **gunicorn** in the `web`
container (`127.0.0.1:8000`) → **PostgreSQL 16** in its own `shared_postgres`
container, plus the long-polling **Telegram bot** as the `telegram_bot`
container. Run everything below as the `deploy` user (groups `sudo`,
`docker`) unless noted; SSH is key-only (step 11).

This assumes nothing exists yet — including the shared PostgreSQL container.
If `shared_postgres` already exists on this host (a second app is also using
it), skip step 4.

No secrets are hardcoded anywhere in this repo or in this document — every
credential below is filled into `.env` on the server, which is never
committed (see `.gitignore`).

---

## 1. Install Docker

```bash
sudo apt-get update && sudo apt-get -y upgrade
sudo apt-get install -y ca-certificates curl gnupg nginx libnginx-mod-http-brotli-filter certbot python3-certbot-nginx git ufw cron
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
# log out/in (or `newgrp docker`) for the group change to take effect
```

Verify: `docker --version && docker compose version`.

## 2. Clone the repo

```bash
sudo mkdir -p /opt/khayrkhoh
sudo chown "$USER":"$USER" /opt/khayrkhoh
git clone https://github.com/YusufDavlatyorov/volunteers.git /opt/khayrkhoh
cd /opt/khayrkhoh
# Bind mounts for nginx: the container runs as uid 1000 and must be able to
# write them (collectstatic, uploads); nginx (www-data) only reads them.
mkdir -p staticfiles media
sudo chown 1000:"$USER" staticfiles media
sudo chmod 2775 staticfiles media
```

## 3. Configure `.env`

```bash
cp .env.example .env
chmod 600 .env
nano .env   # or your editor of choice
```

Fill in at minimum (see `.env.example` for the full annotated list):

- `DJANGO_SECRET_KEY` — generate with:
  `docker run --rm -v "$PWD":/app -w /app python:3.14-slim python -c "import secrets; print(secrets.token_urlsafe(50))"`
- `DJANGO_DEBUG=False`
- `DJANGO_ALLOWED_HOSTS=khayrkhoh.tj,www.khayrkhoh.tj`
- `DJANGO_CSRF_TRUSTED_ORIGINS=https://khayrkhoh.tj,https://www.khayrkhoh.tj`
- `DJANGO_BEHIND_TLS_PROXY=True` (nginx terminates TLS — see step 10)
- (no cache setting needed: `docker-compose.yml` points every app container at
  the `redis` service — `DJANGO_REDIS_URL`, `CELERY_BROKER_URL` — and forces
  `DJANGO_DB_CACHE=False`)
- `DJANGO_DB_NAME`, `DJANGO_DB_USER`, `DJANGO_DB_PASSWORD` — pick real values,
  used again in step 4 below
- `DJANGO_DB_HOST=shared_postgres` (must match the container name in step 4)
- `DJANGO_DB_PORT=5432`
- `SHARED_POSTGRES_NETWORK=shared_postgres_net` (or whatever you name it below)
- `GROQ_API_KEY`, `TELEGRAM_BOT_TOKEN` + `TELEGRAM_BOT_USERNAME`,
  `SMTP_USER`/`SMTP_PASSWORD` — real values. A value containing spaces (e.g. a
  Gmail app password) must be single-quoted; avoid `$` in any value, since
  `docker compose` interpolates it.
- `INITIAL_ADMIN_EMAIL` / `INITIAL_ADMIN_PASSWORD` (+ curator/volunteer/client) —
  real values for the 4 bootstrap accounts, see step 8

**Never commit this file.** `.gitignore` already excludes it.

## 4. Create the shared Docker network + the shared PostgreSQL container

```bash
set -a; source .env; set +a   # load DJANGO_DB_* / SHARED_POSTGRES_NETWORK into this shell

docker network create "$SHARED_POSTGRES_NETWORK"

docker volume create shared_postgres_data

docker run -d \
  --name "$DJANGO_DB_HOST" \
  --network "$SHARED_POSTGRES_NETWORK" \
  --restart unless-stopped \
  -e POSTGRES_USER="$DJANGO_DB_USER" \
  -e POSTGRES_PASSWORD="$DJANGO_DB_PASSWORD" \
  -e POSTGRES_DB="$DJANGO_DB_NAME" \
  -v shared_postgres_data:/var/lib/postgresql/data \
  postgres:16
```

No `-p`/published port — the database is reachable only from other
containers on `$SHARED_POSTGRES_NETWORK`, never from the host or internet.
The container name (`$DJANGO_DB_HOST`) is what Docker's embedded DNS
resolves inside that network, which is why `DJANGO_DB_HOST` in `.env` must
match it exactly.

## 5. Build and start the containers

```bash
docker compose up -d --build
```

This starts:
- `web` (gunicorn, 3 workers, 60s timeout) bound to `127.0.0.1:8000` only;
- `celery_worker` — delivers queued email/Telegram notifications in the
  background (threads pool, retries with backoff; CLAUDE.md → Notifications);
- `telegram_bot` (step 12);
- `redis` — cache (rate limits, cached sessions, public-page aggregates) and
  the Celery broker; private `internal` network only, no published port,
  append-only file on the `redis_data` volume so queued notifications survive
  a restart.

The app containers are on both `$SHARED_POSTGRES_NETWORK` (database) and
`internal` (redis). `mem_limit: 700m`, `cpus: 1.0`, `restart: unless-stopped` are
already set there.

## 6. Migrate

```bash
docker compose exec -T web python manage.py migrate
```

## 7. (Optional) DB cache table

Not needed with the `redis` service (the default). Only if you ever run
without Redis and set `DJANGO_DB_CACHE=True` instead:

```bash
docker compose exec -T web python manage.py createcachetable
```

## 8. Collect static files

```bash
docker compose exec -T web python manage.py collectstatic --noinput
docker compose restart web   # load the new staticfiles.json (hashed names)
```

`docker-compose.yml` sets `DJANGO_STATIC_MANIFEST=True`, so collectstatic
writes content-hashed copies (`style.3f2a….css`) plus `staticfiles.json`, and
templates reference the hashed names. `web` reads that manifest at start-up —
hence the restart.

Writes into the `./staticfiles` bind mount (`docker-compose.yml`), which
nginx serves directly in step 10.

## 9. Create the 4 initial production accounts

```bash
docker compose exec -T web python manage.py create_initial_production_accounts
```

Idempotent — safe to re-run. Creates exactly 1 admin, 1 curator, 1
volunteer, 1 client from the `INITIAL_*` vars in `.env`. **Never run
`seed_demo` in production** — it seeds fake business data and must stay a
local-dev-only tool.

## 10. nginx reverse proxy + HTTPS (certbot)

Write `/etc/nginx/sites-available/khayrkhoh` — first as plain HTTP so
certbot can issue the certificate:

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name khayrkhoh.tj www.khayrkhoh.tj;
    location / { proxy_pass http://127.0.0.1:8000; proxy_set_header Host $host; }
}
```

```bash
sudo ln -sf /etc/nginx/sites-available/khayrkhoh /etc/nginx/sites-enabled/khayrkhoh
sudo rm /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

Once DNS for `khayrkhoh.tj` **and** `www.khayrkhoh.tj` resolves to this
server (`dig +short A khayrkhoh.tj @8.8.8.8`), issue the certificate:

```bash
sudo certbot --nginx -d khayrkhoh.tj -d www.khayrkhoh.tj --redirect
sudo certbot renew --dry-run   # renewal runs from certbot's systemd timer
```

Then replace the file with the final config. It keeps certbot's certificate
paths and makes **`https://khayrkhoh.tj` the one canonical host**: plain HTTP
and `www` both 301 there with the path kept (SEO — see CLAUDE.md → "SEO").

```nginx
# HTTP (both names) -> https://khayrkhoh.tj, path kept
server {
    listen 80;
    listen [::]:80;
    server_name khayrkhoh.tj www.khayrkhoh.tj;
    return 301 https://khayrkhoh.tj$request_uri;
}

# https://www -> https://khayrkhoh.tj, path kept
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name www.khayrkhoh.tj;
    ssl_certificate /etc/letsencrypt/live/khayrkhoh.tj/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/khayrkhoh.tj/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
    return 301 https://khayrkhoh.tj$request_uri;
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2 ipv6only=on;
    server_name khayrkhoh.tj;
    ssl_certificate /etc/letsencrypt/live/khayrkhoh.tj/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/khayrkhoh.tj/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    client_max_body_size 6M;   # uploads are capped at 5 MB by the app

    # text/html is always compressed once gzip is on; add the static types.
    gzip on;
    gzip_vary on;
    gzip_proxied any;
    gzip_comp_level 5;
    gzip_min_length 256;
    gzip_types text/plain text/css text/xml application/javascript application/json
               application/xml image/svg+xml;

    # Brotli (apt: libnginx-mod-http-brotli-filter) for browsers that accept it
    # — ~15-20% smaller than gzip for CSS/JS/HTML; gzip stays as the fallback.
    # Fonts, images and video are already compressed and are not in either list.
    brotli on;
    brotli_comp_level 5;
    brotli_min_length 256;
    brotli_types text/plain text/css text/xml application/javascript application/json
                 application/xml image/svg+xml;

    # Content-hashed static files (DJANGO_STATIC_MANIFEST=True →
    # ManifestStaticFilesStorage: name.<12 hex>.ext). A changed file gets a new
    # name, so the same URL never changes content: cache for a year, immutable.
    # Covers CSS/JS/fonts/images and the hero videos (Range requests work —
    # Accept-Ranges: bytes; mime.types maps .webm/.mp4/.woff2).
    location ~ "^/static/(.+\.[0-9a-f]{12}\.[A-Za-z0-9]+)$" {
        alias /opt/khayrkhoh/staticfiles/$1;
        expires 1y;
        add_header Cache-Control "public, immutable";
        sendfile on;
        tcp_nopush on;
        access_log off;
    }

    # Unhashed originals (collectstatic keeps them too, e.g. for a hand-typed
    # URL) — these can change under the same name, so keep the cache modest.
    location /static/ {
        alias /opt/khayrkhoh/staticfiles/;
        expires 7d;
        add_header Cache-Control "public";
        access_log off;
    }

    location /media/ {
        alias /opt/khayrkhoh/media/;
        expires 7d;
        add_header Cache-Control "public";
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 65s;
    }
}
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

`X-Forwarded-Proto` is what `DJANGO_BEHIND_TLS_PROXY=True` trusts — without
it Django sees every request as plain HTTP and redirect-loops.

### 10a. WebSockets (`/ws/` → `ws` service) and private chat photos

Feature Pack v2 adds the `ws` container (uvicorn, `127.0.0.1:8001`, Channels
layer on Redis DB 2 via `DJANGO_CHANNEL_REDIS_URL`, set for every app container
in `docker-compose.yml`) and protected chat photos. Two additions to the config
above — verified locally against `nginx:1.27-alpine` (2026-10-02): anonymous
socket → upgraded then closed 4401, foreign `Origin` → 403 at the handshake,
logged-in socket gets `hello`/`pong`, an event published from another process
arrives through Redis; `/media/chat/…` → 404, `/protected-media/…` → 404
from outside.

Once, in the `http {}` context (e.g. `/etc/nginx/conf.d/websocket-map.conf`):

```nginx
map $http_upgrade $connection_upgrade { default upgrade; '' close; }
```

Inside the `server_name khayrkhoh.tj` 443 block, **above** `location /media/`
and `location /`:

```nginx
    # WebSockets → the ws container (Django Channels under uvicorn).
    location /ws/ {
        proxy_pass http://127.0.0.1:8001;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;               # AllowedHostsOriginValidator
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 3600s;                  # clients heartbeat every 25 s
        proxy_send_timeout 3600s;
        proxy_buffering off;
    }

    # Chat photos are private: never served straight from /media/ …
    location ^~ /media/chat/ { return 404; }
    # … only through the permission-checked view's X-Accel-Redirect
    # (compose sets DJANGO_MEDIA_X_ACCEL=True for web).
    location ^~ /protected-media/ {
        internal;
        alias /opt/khayrkhoh/media/;
        add_header Cache-Control "private, no-store";
    }
```

Verify on the server (read-only):

```bash
sudo nginx -t
docker compose exec -T web python manage.py check_realtime   # backend: channels_redis… / round trip: OK
docker compose exec -T ws  python manage.py check_realtime
# Handshake through nginx: prints 101 (curl then waits and exits 28 after 5 s
# — expected; the consumer closes an anonymous socket with 4401).
curl -sk -o /dev/null -w "%{http_code}\n" --http1.1 -m 5 \
  -H "Connection: Upgrade" -H "Upgrade: websocket" -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" -H "Origin: https://khayrkhoh.tj" \
  https://khayrkhoh.tj/ws/
curl -s -o /dev/null -w "%{http_code}\n" https://khayrkhoh.tj/media/chat/x.jpg        # 404
curl -s -o /dev/null -w "%{http_code}\n" https://khayrkhoh.tj/protected-media/x.jpg   # 404
```

In a browser: log in, DevTools → Network → WS: one `/ws/` socket with status
101 and `hello` / `pong` frames.

## 11. Firewall + SSH hardening

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

Nothing else needs to be open — PostgreSQL (step 4) has no published port,
and the app container (step 5) is bound to loopback only.

After confirming `ssh deploy@<server>` works with your key, disable password
logins (a `00-` drop-in wins over any later file that sets the same option):

```bash
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin prohibit-password\n' \
  | sudo tee /etc/ssh/sshd_config.d/00-hardening.conf
sudo sshd -t && sudo systemctl reload ssh
```

## 12. Telegram bot

The bot is a separate long-polling process — not part of the gunicorn
request cycle (see CLAUDE.md → "Telegram account linking"). It runs as the
`telegram_bot` service in `docker-compose.yml`: same image and `.env` as
`web`, `restart: unless-stopped`, on the same `$SHARED_POSTGRES_NETWORK`.
`docker compose up -d --build` (step 5) already starts it.

```bash
docker compose logs -f telegram_bot   # should show polling, no tracebacks
docker compose restart telegram_bot   # after a code update / .env change
```

### Background notifications (celery_worker + redis)

```bash
docker compose logs -f celery_worker          # "ready", then "Task notifications.send_email[…] succeeded"
docker compose exec -T redis redis-cli ping   # PONG
docker compose exec -T redis redis-cli -n 1 llen celery   # queued deliveries waiting (normally 0)
```

If the worker is down, notifications wait in Redis and go out when it comes
back; if Redis itself is down, `notify_users` delivers inline (slower requests,
nothing lost) and logs a WARNING.

## 13. Cron: overdue + stale request sweeps

Both commands are idempotent — safe to re-run; see CLAUDE.md → "Background
jobs" for why.

```bash
sudo install -d -o "$USER" -g "$USER" /var/log/khayrkhoh
crontab -e   # as the deploy user
```

Add:

```cron
# KhayrKhoh — alert curators/admins about tasks overdue past 3h, every 15 min.
*/15 * * * * cd /opt/khayrkhoh && flock -n /tmp/khayrkhoh-overdue.lock docker compose exec -T web python manage.py check_overdue_tasks >> /var/log/khayrkhoh/cron.log 2>&1
# KhayrKhoh — alert about pending requests stuck without a volunteer past 48h, hourly.
0 * * * * cd /opt/khayrkhoh && flock -n /tmp/khayrkhoh-stale.lock docker compose exec -T web python manage.py check_stale_requests >> /var/log/khayrkhoh/cron.log 2>&1
```

Feature Pack v2 adds seven more idempotent sweeps (same pattern; times are the
server clock, UTC — 05:00 UTC = 10:00 in Dushanbe):

```cron
# Volunteer availability: idle → offline, expired do-not-disturb → off. Every 5 min.
*/5 * * * * cd /opt/khayrkhoh && flock -n /tmp/khayrkhoh-availability.lock docker compose exec -T web python manage.py sweep_availability >> /var/log/khayrkhoh/cron.log 2>&1
# Live-tracking trail: delete points of ended tasks / older than 24 h. Hourly.
20 * * * * cd /opt/khayrkhoh && flock -n /tmp/khayrkhoh-tracking.lock docker compose exec -T web python manage.py purge_tracking >> /var/log/khayrkhoh/cron.log 2>&1
# In-app notifications older than 90 days. Daily.
10 3 * * * cd /opt/khayrkhoh && flock -n /tmp/khayrkhoh-notifications.lock docker compose exec -T web python manage.py prune_notifications >> /var/log/khayrkhoh/cron.log 2>&1
# Chat retention: photos 90 d / messages 180 d after the task ended. Daily.
30 3 * * * cd /opt/khayrkhoh && flock -n /tmp/khayrkhoh-chats.lock docker compose exec -T web python manage.py purge_chats >> /var/log/khayrkhoh/cron.log 2>&1
# Remind volunteers whose saved location is older than 30 days. Daily, 10:00 Dushanbe.
0 5 * * * cd /opt/khayrkhoh && flock -n /tmp/khayrkhoh-location.lock docker compose exec -T web python manage.py remind_stale_locations >> /var/log/khayrkhoh/cron.log 2>&1
# Volunteer community (docs/COMMUNITY_BOT.md): remove members who are no longer eligible. Hourly.
40 * * * * cd /opt/khayrkhoh && flock -n /tmp/khayrkhoh-community.lock docker compose exec -T web python manage.py community_sweep >> /var/log/khayrkhoh/cron.log 2>&1
# Volunteer community: the weekly "thank you" post (once per ISO week). Mondays, 10:00 Dushanbe.
0 5 * * 1 cd /opt/khayrkhoh && flock -n /tmp/khayrkhoh-weekly.lock docker compose exec -T web python manage.py community_weekly >> /var/log/khayrkhoh/cron.log 2>&1
```

`/etc/logrotate.d/khayrkhoh` rotates `/var/log/khayrkhoh/*.log` weekly
(8 kept, compressed, `su deploy deploy`).

## 14. Verify

```bash
curl -fsS https://khayrkhoh.tj/health/
curl -fsS https://khayrkhoh.tj/health/ready/
curl -sI http://khayrkhoh.tj/ | head -3        # 301 → https
docker compose ps                             # web + telegram_bot "Up"
```

Expect `{"status":"ready",...}` with HTTP 200. Also spot-check: load
`https://khayrkhoh.tj/` in a browser, log in with the admin account created
in step 9, open the dashboard and the map.

### Compression check (gzip / brotli)

Step 10's config compresses HTML, CSS, JS, JSON and SVG (`gzip on` + `brotli on`,
`gzip_proxied any` so the HTML that gunicorn returns is compressed too). Checked
from outside on 2026-10-03 — it is on in production:

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
missing: `sudo apt-get install -y libnginx-mod-http-brotli-filter`, make sure the
`gzip …` / `brotli …` lines of step 10 are inside the `server_name khayrkhoh.tj`
block (and not overridden by a `gzip off;` in `/etc/nginx/nginx.conf`), then
`sudo nginx -t && sudo systemctl reload nginx`. Already-compressed media (WebP,
MP4/WebM, WOFF2) is deliberately not in either list.

---

## Redeploying after a code change

```bash
cd /opt/khayrkhoh
git pull
docker compose build
docker compose run --rm web python manage.py migrate --noinput
docker compose run --rm web python manage.py collectstatic --noinput
docker compose up -d
```

Order matters with hashed static files: migrate and collectstatic run in
one-off containers from the **new** image first, so when `up -d` recreates
`web` (and `telegram_bot`) the new manifest and schema are already in place —
the running site never serves new templates against an old `staticfiles.json`.
`up -d` recreates every app container (`web`, `ws`, `telegram_bot`,
`celery_worker`) on the new image, so all of them pick up new code automatically.
A redeploy that changes nginx or crons, or the first Feature Pack v2 rollout,
follows `docs/DEPLOY_FEATURE_PACK_V2.md` (backups, order, rollback plan).

## Notes

- **`.env` is never touched by any of the above except step 3** — it is
  gitignored and stays only on the server / in your secret store.
- No `DATABASE_URL` variable exists in this project by design — PostgreSQL
  connection info is the discrete `DJANGO_DB_*` vars (see `.env.example`).
- `docker-compose.yml` deliberately runs no PostgreSQL service — step 4's
  container is the one and only database, external to compose, on the
  shared network so other apps on the same host can reach it too.
- Backup/restore, disaster recovery, and known production limitations are
  documented in README.md → "Backup & recovery".
