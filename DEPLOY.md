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
sudo apt-get install -y ca-certificates curl gnupg nginx certbot python3-certbot-nginx git ufw cron
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
- `DJANGO_DB_CACHE=True`
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

## 5. Build and start the app + bot containers

```bash
docker compose up -d --build
```

This starts `web` (gunicorn, 3 workers, 60s timeout) bound to
`127.0.0.1:8000` only, and `telegram_bot` (step 12), both on the same
`$SHARED_POSTGRES_NETWORK` as the database. `mem_limit: 700m`, `cpus: 1.0`, `restart: unless-stopped` are
already set there.

## 6. Migrate

```bash
docker compose exec -T web python manage.py migrate
```

## 7. Create the shared DB cache table

```bash
docker compose exec -T web python manage.py createcachetable
```

Required because `DJANGO_DB_CACHE=True` — without this the login /
password-reset / AI-assistant / Telegram-link rate limiters only throttle
within a single gunicorn worker.

## 8. Collect static files

```bash
docker compose exec -T web python manage.py collectstatic --noinput
```

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

Write `/etc/nginx/sites-available/khayrkhoh`:

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name khayrkhoh.tj www.khayrkhoh.tj;

    client_max_body_size 6M;   # uploads are capped at 5 MB by the app

    location /static/ {
        alias /opt/khayrkhoh/staticfiles/;
        expires 7d;
        access_log off;
    }

    location /media/ {
        alias /opt/khayrkhoh/media/;
        expires 7d;
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
sudo ln -sf /etc/nginx/sites-available/khayrkhoh /etc/nginx/sites-enabled/khayrkhoh
sudo rm /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

`X-Forwarded-Proto` is what `DJANGO_BEHIND_TLS_PROXY=True` trusts — without
it Django sees every request as plain HTTP and redirect-loops.

Once DNS for `khayrkhoh.tj` **and** `www.khayrkhoh.tj` resolves to this
server (`dig +short A khayrkhoh.tj @8.8.8.8`), issue the certificate — certbot
adds the TLS server block and an HTTP→HTTPS redirect to the file above:

```bash
sudo certbot --nginx -d khayrkhoh.tj -d www.khayrkhoh.tj --redirect
sudo certbot renew --dry-run   # renewal runs from certbot's systemd timer
```

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

---

## Redeploying after a code change

```bash
cd /opt/khayrkhoh
git pull
docker compose up -d --build
docker compose exec -T web python manage.py migrate
docker compose exec -T web python manage.py collectstatic --noinput
```

`docker compose up -d --build` rebuilds the image and recreates both `web`
and `telegram_bot`, so the bot picks up new code automatically.

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
