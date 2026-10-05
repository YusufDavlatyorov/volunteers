# KhayrKhoh

**KhayrKhoh** is a Django web platform that connects elderly people who need help
(**clients**) with **volunteers** in their region, coordinated by **curators** and **admins**.
Built for the 5 regions of Tajikistan: Dushanbe, Sogd, Khatlon, GBAO and RRP.

Production domain: **https://khayrkhoh.tj** (and `https://www.khayrkhoh.tj`).

## How it works

1. A **client** creates a help request (groceries, medical escort, transport, household, documents,
   emotional support, etc.).
2. **Volunteers** see the free requests in their region and accept one at a time.
3. On completion the volunteer earns rating points, and the client is notified.
4. **Curators / admins** manage events, send broadcasts to volunteers, review the archive and flag
   overdue tasks.
5. A volunteer working an active request can raise an **SOS / danger report** — curators and
   admins are alerted immediately and manage it through the emergency CRM
   (`open → acknowledged → resolved`).
6. Participants are notified by **email and Telegram**.
7. An **AI assistant** (Groq, OpenAI-compatible API) gives context-aware advice — gentle for clients,
   practical for volunteers — and gracefully falls back to a canned tip when no API key is set.
8. A **Telegram bot** lets users link their chat ID to receive notifications.

## Tech stack

- **Python 3.14** / **Django 5.2** (function-based views, MVT)
- **SQLite** (local dev / tests) / **PostgreSQL 16** (required in production — see "Deployment")
- **django-filter**, **django-crispy-forms** + **crispy-bootstrap5**
- **requests** (Groq AI + Telegram), **python-dotenv** (config), **Pillow** (image uploads)
- **gunicorn** + **uvicorn** workers (ASGI: pages + WebSockets via Django Channels), **Celery** +
  **Redis**, **psycopg[binary]** (production — see "Deployment")
- **No Docker** — production runs directly on Ubuntu: venv + systemd + nginx
- Custom user model `accounts.Users` with roles: admin / curator / volunteer / client

## Project layout

```
server/      Django project (settings, urls, wsgi/asgi)
accounts/    Users, Profile, auth views, registration, password reset
myapp/       Help requests, events, broadcasts, photo reports, rating, AI, Telegram bot
templates/   HTML templates (base + accounts/ + myapp/)
static/      CSS, JS, images, the About Us background video
media/       User-uploaded avatars and photo reports (not committed)
deploy/      Production files: systemd units, nginx, PostgreSQL/Redis/journald/logrotate configs,
             crontab, deploy.sh (redeploy), check_env.py, MIGRATION.md (the one-time move off Docker)
DEPLOY.md    Exact command sequence for a fresh Ubuntu server (venv, PostgreSQL, Redis, systemd, nginx + certbot, cron)
```

## Getting started

### 1. Clone and create a virtual environment

```bash
git clone <your-repo-url>
cd khayrkhoh

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

Copy the example file and edit the values:

```bash
cp .env.example .env
```

Generate a Django secret key:

```bash
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

Most variables are optional for local development and have safe fallbacks. The
one exception is `DJANGO_SECRET_KEY` — the app refuses to start without it (there
is **no** insecure built-in fallback). `.env.example` ships a placeholder so a
fresh checkout boots; replace it with a real key.

| Variable | Purpose | If unset |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | Django secret key | **startup error** (required) |
| `DJANGO_DEBUG` | Debug mode (`True`/`False`) | `False` |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated hosts | `localhost,127.0.0.1` (a `*` wildcard is refused once `DEBUG=False`). Production: `khayrkhoh.tj,www.khayrkhoh.tj` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Full HTTPS origins allowed to POST | empty. Production: `https://khayrkhoh.tj,https://www.khayrkhoh.tj` |
| `DJANGO_BEHIND_TLS_PROXY` | Trust `X-Forwarded-Proto` from a reverse proxy; rate limits read the visitor IP from nginx's `X-Real-IP` | `False`. Production (behind nginx): `True` |
| `DJANGO_BEHIND_CLOUDFLARE` | Visitor IP from `CF-Connecting-IP` when the request came from a Cloudflare range (`docs/CLOUDFLARE.md`) | `False` |
| `DJANGO_CLOUDFLARE_IP_RANGES` | Override the built-in Cloudflare CIDR list (comma-separated) | empty = built-in list |
| `DJANGO_DB_NAME` / `DJANGO_DB_USER` / `DJANGO_DB_PASSWORD` / `DJANGO_DB_HOST` / `DJANGO_DB_PORT` | PostgreSQL connection — **required in production** (local PostgreSQL 16, `DJANGO_DB_HOST=127.0.0.1`), see "Deployment" | SQLite (dev only) |
| `DJANGO_REDIS_URL` | Redis cache (rate limits, cached sessions, public-page aggregates) | unset. Production: `redis://127.0.0.1:6379/0` (local redis-server; wins over `DJANGO_DB_CACHE`) |
| `CELERY_BROKER_URL` | Celery broker for background email/Telegram delivery | unset → tasks run inline. Production: `redis://127.0.0.1:6379/1` |
| `DJANGO_CHANNEL_REDIS_URL` | Channels layer for WebSockets (fan-out across gunicorn workers and Celery) | unset → in-process layer. Production: `redis://127.0.0.1:6379/2` |
| `DJANGO_STATIC_MANIFEST` | Content-hashed static names (nginx caches `/static/` for a year); collectstatic must run before the app starts | `False`. Production: `True` |
| `DJANGO_MEDIA_X_ACCEL` | Private chat photos streamed by nginx via `X-Accel-Redirect` after the app's permission check | `False`. Production: `True` |
| `DJANGO_DB_CACHE` | Use a shared DB cache for rate-limit counters | `False`. A dependency-free shared cache for setups without Redis (`createcachetable` once); production uses Redis instead and keeps this `False` — a multi-worker deployment needs *some* shared cache for the rate limiters to work at all |
| `DJANGO_LOG_LEVEL` | App logger level | `INFO` |
| `SMTP_USER`, `SMTP_PASSWORD` | SMTP credentials | emails print to console |
| `EMAIL_TIMEOUT` | Seconds before a synchronous SMTP send is abandoned | `10` |
| `GROQ_API_KEY` | Groq API key for the AI assistant (must start with `gsk_`) | AI returns a fallback tip |
| `GROQ_MODEL` / `GROQ_ASSISTANT_MODEL` | Groq model IDs (fallback / primary tool-calling model) — verify against `GET /openai/v1/models` before changing, don't guess | `openai/gpt-oss-20b` / `qwen/qwen3.8-27b` |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token | Telegram features disabled |
| `OSRM_BASE_URL`, `NOMINATIM_USER_AGENT` | Routing / geocoding for the `osm` maps provider | public demo endpoints (rate-limited, not for production) |
| `INITIAL_ADMIN_EMAIL` / `_PASSWORD` / `_USERNAME` (and `_CURATOR_`, `_VOLUNTEER_`, `_CLIENT_`) | The 4 initial production accounts, see "Initial production accounts" below | none — required only when running `create_initial_production_accounts` |

> **Never commit your real `.env`.** It is already listed in `.gitignore`.

### 4. Apply migrations

```bash
python manage.py migrate
```

### 5. (Optional) Load demo data

Creates admins, curators, volunteers, clients, requests, events and photo reports:

```bash
python manage.py seed_demo
```

Demo login (all demo users share the same password):

- `admin_demo` / `Volunteer2026!` (admin)
- `malika_s` / `Volunteer2026!` (volunteer)
- `client_zamira` / `Volunteer2026!` (client)

**`seed_demo` is dev/demo-only — never run it against production.** Production gets its
accounts from `create_initial_production_accounts` instead; see "Initial production accounts"
below.

### 6. Run the server

```bash
python manage.py runserver
```

Open http://127.0.0.1:8000/ . To create your own admin instead of demo data:

```bash
python manage.py createsuperuser
```

### 7. (Optional) Run the Telegram bot

Requires `TELEGRAM_BOT_TOKEN` in `.env`:

```bash
python manage.py run_telegram_bot
```

Locally, use a **separate test bot** from @BotFather — never the production token (a second
process with the same token steals the production bot's updates). Step-by-step for the owner:
`docs/TEST_BOT.md`. A DEBUG run with a bot token or SMTP login in `.env` prints a warning banner.

## Background jobs (production)

Scheduling has **no Celery beat** — scheduled work is plain management
commands driven by the system cron. Each command is idempotent and safe to run
repeatedly.

### Overdue help-request monitoring

`check_overdue_tasks` finds help requests that have been *active* for more than
3 hours (`OVERDUE_THRESHOLD`) without completing, emails/Telegrams the curators
and admins once per task, and marks it (`alarm_sent`) so it is not reported
again. This is the automated equivalent of the "Check overdue" button on the
admin dashboard — both call `myapp.services.overdue.sweep_overdue_tasks()`.

```bash
python manage.py check_overdue_tasks            # run the sweep
python manage.py check_overdue_tasks --dry-run  # list overdue tasks, send nothing
```

### Stale pending-request monitoring

`check_stale_requests` is the mirror of the above for the *pending* side: it
finds help requests that have waited more than 48 hours
(`STALE_PENDING_THRESHOLD`) without a volunteer accepting them, alerts the
curators and admins once per request, and marks it (`stale_alert_sent`) so it is
not reported again. All logic lives in `myapp.services.stale`; the curator
dashboard and `crm/tasks/?stale=1` show the same "stalled" set.

```bash
python manage.py check_stale_requests            # run the sweep
python manage.py check_stale_requests --dry-run  # list stale requests, send nothing
```

In production both run from the deploy user's crontab together with the other periodic
sweeps — the full list is [`deploy/crontab.example`](deploy/crontab.example) (`crontab
deploy/crontab.example`). Each line uses the project's own virtualenv Python by absolute path —
cron runs with a bare environment:

```cron
*/15 * * * * cd /opt/khayrkhoh && flock -n /tmp/khayrkhoh-overdue.lock $PY manage.py check_overdue_tasks >> $LOG 2>&1
0 * * * * cd /opt/khayrkhoh && flock -n /tmp/khayrkhoh-stale.lock $PY manage.py check_stale_requests >> $LOG 2>&1
```

`settings.py` loads `.env` by absolute path, so credentials are picked up regardless of cron's
working directory; the `cd` is for `manage.py`.

**`flock -n` is belt-and-braces, not load-bearing.** Both sweeps are already
race-safe on their own — each task/request is claimed with a conditional
`UPDATE ... WHERE alarm_sent=False` (resp. `stale_alert_sent=False`) *before* its
alert is sent, so two overlapping runs can never double-notify. `flock` just
skips a redundant run if the previous one is somehow still going (a very slow DB,
say). Both commands are idempotent, exit non-zero only on an unhandled error,
support `--dry-run`, and log every alert (and any zero-recipient failure, at
`ERROR`) to stderr → the cron log.

## Running tests

```bash
python manage.py test --parallel 4   # full suite (current count: FEATURES_PROGRESS.md)
python manage.py test myapp      # one app
python manage.py test myapp.tests.MatchingAlgorithmTests   # one class
```

The suite covers roles and auth, registration and password-reset (regression tests), the
client → volunteer request lifecycle, rating updates, the AI fallback path, the geo/matching
services, the CRM dashboards, emergency/SOS, overdue and stale-request monitoring, in-kind
donation offers, the Lost & Found board, the health endpoints, external-service failure
handling, list-view pagination bounds, and a set of concurrency / IDOR / rate-limit regression
tests.

No database server or Docker is needed: the suite runs on SQLite. Migration rehearsals against
PostgreSQL 16 use a local Homebrew `postgresql@16` (see `scripts/verify_backup.sh` for the
throw-away-cluster pattern) or the rehearsal VM in `deploy/MIGRATION.md` (step R).

## Demo data — NEVER in production

`seed_demo` creates fake admins/curators/volunteers/clients, help requests, events, donations,
pet reports, photo reports and emergency reports. It exists **only** for local development and
demos. **Do NOT run `seed_demo` against the production database.** Production starts from a
fresh PostgreSQL database with all migrations applied and gets its only accounts from
`create_initial_production_accounts` (below) — zero demo business data.

## Initial production accounts

`create_initial_production_accounts` creates exactly 4 accounts — 1 admin, 1 curator, 1
volunteer, 1 client — from environment variables, and nothing else:

```bash
python manage.py create_initial_production_accounts
```

Reads `INITIAL_ADMIN_EMAIL` / `INITIAL_ADMIN_PASSWORD` (and the matching `_CURATOR_`,
`_VOLUNTEER_`, `_CLIENT_` pairs) from `.env` — see `.env.example` for the full list, including
the optional `_USERNAME` (defaults to `admin`/`curator`/`volunteer`/`client`) and `_REGION`
(defaults to `dushanbe`) overrides. It:

- **never invents a password** — missing email/password env vars fail the command with a list of
  exactly which ones are missing, before creating anything;
- runs every password through Django's normal `AUTH_PASSWORD_VALIDATORS` and rejects a weak one
  with the same validation messages `register` would show, never logging the password itself;
- refuses `seed_demo`'s shared demo password (it is public in this README) before creating anything;
- is **idempotent** — an account that already exists (matched by username or email) is left
  untouched, so re-running it after someone has changed their password in the UI is safe;
- uses normal Django password hashing (`set_password`), same as every other account.

`python manage.py check_demo_password` is the read-only audit for a live database: it lists every
account that accepts the demo password and any demo username present, prints usernames only, never
re-saves a hash, and exits 1 if anything is found (run at every deploy).
`python manage.py check_demo_data` is the broader read-only audit (the deploy runbook runs it before
and after a deploy): it includes the password check and also reports every other kind of row
`seed_demo` writes (requests, events, broadcasts, photo reports, SOS, catalogue, donation offers,
pets, chat lines) and media files byte-identical to the demo photos — ids and counts only, exit 1 if
any. If it finds something, nothing is deleted: the owner decides. `seed_demo` itself refuses to run
with `DEBUG=False`.

## Deployment

**Production runs without Docker**: Python 3.14 in a venv, PostgreSQL 16 and Redis from apt,
four systemd units, host nginx + certbot. Everything is in [`deploy/`](deploy/):

| File | What |
|---|---|
| `deploy/systemd/khayrkhoh-web.service` | gunicorn + uvicorn workers serving `server/asgi.py` — pages **and** WebSockets (`/ws/`) — on `127.0.0.1:8000` |
| `deploy/systemd/khayrkhoh-celery.service` | Celery worker (`--pool=threads`): notification delivery, image variants |
| `deploy/systemd/khayrkhoh-telegram-bot.service` / `-community-bot.service` | the two long-polling bots (community bot enabled only once configured) |
| `deploy/gunicorn.conf.py` | workers, timeouts, logging; no persistent DB connections under ASGI |
| `deploy/nginx/khayrkhoh.conf` | TLS (certbot paths), canonical host, gzip/brotli, static/media from disk, `/ws/` upgrade, private chat photos |
| `deploy/postgresql/`, `deploy/redis/`, `deploy/journald/`, `deploy/logrotate/` | service tuning and log limits |
| `deploy/crontab.example` | the 9 periodic sweeps |
| `deploy/deploy.sh` | routine redeploy: pull → deps → `.env` check → collectstatic → migrate → restart → readiness |
| `deploy/check_env.py` | `.env` read identically by systemd and python-dotenv + production values (names only) |
| `deploy/MIGRATION.md` | the one-time move of the live server off Docker (with the Feature Pack v2 rollout, rollback plan) |

**Fresh server:** follow `DEPLOY.md` step by step. **Redeploy after a code change:**
`/opt/khayrkhoh/deploy/deploy.sh` as the `deploy` user. Demo data never reaches production —
no script in `deploy/` seeds anything, and `seed_demo` refuses to run with `DEBUG=False`.

`DEBUG=False` automatically switches on `SECURE_SSL_REDIRECT`, HSTS (1 year, preload),
`SESSION_COOKIE_SECURE` / `CSRF_COOKIE_SECURE`; `SECURE_CONTENT_TYPE_NOSNIFF` and
`X_FRAME_OPTIONS=DENY` are always on. `EMAIL_TIMEOUT` bounds an inline SMTP send (default 10s).
None of this needs a code change per environment — it all reads from `.env`.

**Logs** — the app logs to stderr (`myapp` / `accounts` loggers, level `DJANGO_LOG_LEVEL`) →
journald: `journalctl -u khayrkhoh-web` (likewise `-celery`, `-telegram-bot`), capped at 500 MB
(`deploy/journald/`). Server errors (`django.request`, `ERROR`) and the zero-recipient safety nets
in the overdue / stale / emergency sweeps go there too. Administrative state changes (task
assign/complete, application approve/reject, emergency/donation/pet transitions, rate-limit hits)
emit one `INFO` line each with IDs only — never message bodies, tokens, or PII. The cron jobs
write to `/var/log/khayrkhoh/cron.log` (logrotate, size-capped).

## Health checks

Two public, unauthenticated, no-secret endpoints (`server/health.py`):

| Endpoint | Meaning | Cost | Codes |
| --- | --- | --- | --- |
| `GET /health/` | **liveness** — the Django process can serve a request | zero I/O | `200` |
| `GET /health/ready/` | **readiness** — DB reachable; the DB schema is the one this code expects (every migration applied, none applied that the code doesn't have, every model table and column present); config sane; shared cache (if configured) round-trips | ≈ 4 short queries, 1–8 ms (+ one cache op) | `200` ready / `503` not ready |

Point an uptime monitor at `/health/ready/` (`deploy/deploy.sh` polls it after every restart); use `/health/`
for a bare "is the process up" check. External services (Groq, Telegram, OSRM, Nominatim) are
**not** part of readiness — the app degrades gracefully without them, so their outage must not
pull an instance out of rotation. Responses carry `Cache-Control: no-store`. A failing check is
logged as one `WARNING server.health: readiness: …` line (which migration / which table); the
public body only says `ok` / `error` / `skipped` per check.

Polled from the server itself (straight to gunicorn), a probe needs the public Host and the
proto header nginx would add — otherwise Django answers 400 (unknown host) or 301 (HTTPS redirect):
```bash
curl -fsS -H "Host: khayrkhoh.tj" -H "X-Forwarded-Proto: https" http://127.0.0.1:8000/health/ready/
```

## Backup & recovery

The application's durable state is **two things**: the database and `media/` (user-uploaded
avatars, photo-report and pet-report images). Neither is in git. **No backups are automated by
this repository** — the deploy owner must schedule them.

### Database backup (PostgreSQL)

Nightly, from the deploy user's crontab. The password comes from `~/.pgpass` (created once,
mode 600 — `127.0.0.1:5432:<DJANGO_DB_NAME>:<DJANGO_DB_USER>:<DJANGO_DB_PASSWORD>`), never from the
command line:

```cron
# KhayrKhoh — nightly DB dump, keep ~2 weeks.
15 2 * * * pg_dump -h 127.0.0.1 -U <DJANGO_DB_USER> -Fc <DJANGO_DB_NAME> > /var/backups/khayrkhoh/nightly/db-$(date +\%F).dump && find /var/backups/khayrkhoh/nightly -name 'db-*.dump' -mtime +14 -delete
```
(`sudo install -d -o deploy -g deploy -m 700 /var/backups/khayrkhoh/nightly` once.)
Store the dumps off-box (another host / object storage / offline media) — a backup on the same
disk does not survive a disk loss. `scripts/verify_backup.sh` (on the Mac) proves a dump really
restores. For SQLite (dev only) the equivalent is copying `Gen_connect.sqlite3` while the app is
stopped, or `sqlite3 Gen_connect.sqlite3 ".backup ..."`.

### Media backup

`media/` is append-mostly application data, not a cache. Back it up with the same cadence as the
DB (`rsync -a --delete media/ /var/backups/khayrkhoh/media/`, or a filesystem snapshot). A DB
restore without the matching media leaves image URLs pointing at missing files.

Since Feature Pack v2 milestone 6 every avatar / photo report / pet photo has WebP variants next
to it (`<name>_thumb.webp`, `<name>_medium.webp`, listed in the row's `image_variants`). They can
always be rebuilt from the originals: `python manage.py backfill_image_variants`.

**CDN:** putting Cloudflare in front of the site is an owner step — `docs/CLOUDFLARE.md`.

### Restore / disaster recovery

Backup and restore are **separate procedures**. Assumed disaster-recovery posture: a full host
loss is recovered from the latest off-box DB dump + media copy; point-in-time recovery (WAL
archiving) is **not** set up and is left to the deploy owner if the RPO requires it.

1. Provision the host per `DEPLOY.md` steps 1–5 (packages, clone + venv, `.env` from your secret
   store — it is never in git, PostgreSQL role + empty database, Redis).
2. Restore the database: `pg_restore --no-owner --no-privileges --exit-on-error -h 127.0.0.1 -U "$DJANGO_DB_USER" -d "$DJANGO_DB_NAME" db-YYYY-MM-DD.dump`.
3. Restore `media/` from the latest copy.
4. `.venv/bin/python manage.py collectstatic --noinput && .venv/bin/python manage.py migrate`
   (`migrate` is a no-op if the dump is already current; safe to run).
5. `DEPLOY.md` steps 7–10 (systemd units, nginx + certbot, firewall, cron).
6. `curl -fsS https://<host>/health/ready/` — expect `200 {"status":"ready"}`.
7. Spot-check: log in, open a dashboard, open the map, load `/myapp/pets/`.

### Known production limitations (intentional)

- **Celery is for notification delivery only** (email/Telegram + image variants, `khayrkhoh-celery` + local Redis). Scheduled work stays cron + idempotent management commands, by design — no celery beat.
- **No object storage.** Uploads live on the local `media/` volume; back it up with the DB (above).
- **Backups are cron jobs the operator installs** (the `pg_dump` / `rsync` above), not part of `deploy/crontab.example`.
- **No WAL archiving / PITR.** Recovery granularity is "last nightly dump".
- **AI assistant** depends on Groq; with no key it always returns the canned fallback tip.
- **`osm` maps provider** uses public OSRM / Nominatim demo endpoints — self-host or use a paid
  provider before real traffic (see `OSRM_BASE_URL`).
- **Rate-limit counters** need the shared Redis cache (`DJANGO_REDIS_URL`) to be effective across
  gunicorn workers; the default `LocMemCache` (dev/tests) throttles per-process only.

## Notes

- The SQLite database and uploaded media are intentionally **not** tracked in git; run `migrate`
  (and optionally `seed_demo`) after cloning to recreate them.
- `django-filter` / `crispy-forms` / `crispy-bootstrap5` are in `requirements.txt` and
  `INSTALLED_APPS` but currently unused (forms render through hand-written partials; list
  filtering is hand-rolled in the views).
