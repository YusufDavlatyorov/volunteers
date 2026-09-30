# DEPLOY.md — KhayrKhoh production bring-up on a fresh Ubuntu server

Exact command sequence to take a bare Ubuntu 22.04/24.04 server to a running
production deployment at **https://khayrkhoh.tj** (and `www.khayrkhoh.tj`).
Run everything below as a `sudo`-capable non-root user unless noted.

This assumes nothing exists yet — including the shared PostgreSQL container.
If `shared_postgres` already exists on this host (a second app is also using
it), skip step 4.

No secrets are hardcoded anywhere in this repo or in this document — every
credential below is filled into `.env` on the server, which is never
committed (see `.gitignore`).

---

## 1. Install Docker

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
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
```

## 3. Configure `.env`

```bash
cp .env.example .env
nano .env   # or your editor of choice
```

Fill in at minimum (see `.env.example` for the full annotated list):

- `DJANGO_SECRET_KEY` — generate with:
  `docker run --rm -v "$PWD":/app -w /app python:3.14-slim python -c "import secrets; print(secrets.token_urlsafe(50))"`
- `DJANGO_DEBUG=False`
- `DJANGO_ALLOWED_HOSTS=khayrkhoh.tj,www.khayrkhoh.tj`
- `DJANGO_CSRF_TRUSTED_ORIGINS=https://khayrkhoh.tj,https://www.khayrkhoh.tj`
- `DJANGO_BEHIND_TLS_PROXY=True` (Caddy terminates TLS — see step 10)
- `DJANGO_DB_CACHE=True`
- `DJANGO_DB_NAME`, `DJANGO_DB_USER`, `DJANGO_DB_PASSWORD` — pick real values,
  used again in step 4 below
- `DJANGO_DB_HOST=shared_postgres` (must match the container name in step 4)
- `DJANGO_DB_PORT=5432`
- `SHARED_POSTGRES_NETWORK=shared_postgres_net` (or whatever you name it below)
- `GROQ_API_KEY`, `TELEGRAM_BOT_TOKEN`, `SMTP_USER`/`SMTP_PASSWORD` — real values
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

## 5. Build and start the app container

```bash
docker compose up -d --build
```

This starts `web` (gunicorn) bound to `127.0.0.1:8000` only (see
`docker-compose.yml`), on the same `$SHARED_POSTGRES_NETWORK` as the
database. `mem_limit: 700m`, `cpus: 1.0`, `restart: unless-stopped` are
already set there.

## 6. Migrate

```bash
docker compose exec web python manage.py migrate
```

## 7. Create the shared DB cache table

```bash
docker compose exec web python manage.py createcachetable
```

Required because `DJANGO_DB_CACHE=True` — without this the login /
password-reset / AI-assistant / Telegram-link rate limiters only throttle
within a single gunicorn worker.

## 8. Collect static files

```bash
docker compose exec web python manage.py collectstatic --noinput
```

Writes into the `./staticfiles` bind mount (`docker-compose.yml`), which
Caddy serves directly in step 10.

## 9. Create the 4 initial production accounts

```bash
docker compose exec web python manage.py create_initial_production_accounts
```

Idempotent — safe to re-run. Creates exactly 1 admin, 1 curator, 1
volunteer, 1 client from the `INITIAL_*` vars in `.env`. **Never run
`seed_demo` in production** — it seeds fake business data and must stay a
local-dev-only tool.

## 10. Caddy reverse proxy with automatic HTTPS

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install -y caddy
```

Write `/etc/caddy/Caddyfile`:

```caddyfile
khayrkhoh.tj, www.khayrkhoh.tj {
    handle /static/* {
        root * /opt/khayrkhoh/staticfiles
        uri strip_prefix /static
        file_server
    }
    handle /media/* {
        root * /opt/khayrkhoh/media
        uri strip_prefix /media
        file_server
    }
    handle {
        reverse_proxy 127.0.0.1:8000
    }
}
```

```bash
sudo systemctl reload caddy
```

Caddy automatically obtains and renews a Let's Encrypt certificate for both
hostnames the first time it sees traffic — no extra TLS config needed, as
long as DNS for `khayrkhoh.tj` / `www.khayrkhoh.tj` already points at this
server's public IP before this step.

## 11. Firewall

```bash
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

Nothing else needs to be open — PostgreSQL (step 4) has no published port,
and the app container (step 5) is bound to loopback only.

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
sudo mkdir -p /var/log/khayrkhoh
crontab -e
```

Add:

```cron
# KhayrKhoh — alert curators/admins about tasks overdue past 3h, every 15 min.
*/15 * * * * cd /opt/khayrkhoh && docker compose exec -T web python manage.py check_overdue_tasks >> /var/log/khayrkhoh/cron.log 2>&1
# KhayrKhoh — alert about pending requests stuck without a volunteer past 48h, hourly.
0 * * * * cd /opt/khayrkhoh && docker compose exec -T web python manage.py check_stale_requests >> /var/log/khayrkhoh/cron.log 2>&1
```

## 14. Verify

```bash
curl -fsS https://khayrkhoh.tj/health/ready/
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
docker compose exec web python manage.py migrate
docker compose exec web python manage.py collectstatic --noinput
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
