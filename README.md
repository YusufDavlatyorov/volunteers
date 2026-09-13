# Generation Connect

**Generation Connect** is a Django web platform that connects elderly people who need help
(**clients**) with **volunteers** in their region, coordinated by **curators** and **admins**.
Built for the 5 regions of Tajikistan: Dushanbe, Sogd, Khatlon, GBAO and RRP.

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

- **Python 3.12+** (developed on 3.14) / **Django 5.2** (function-based views, MVT)
- **SQLite** database (default)
- **django-filter**, **django-crispy-forms** + **crispy-bootstrap5**
- **requests** (Groq AI + Telegram), **python-dotenv** (config), **Pillow** (image uploads)
- Custom user model `accounts.Users` with roles: admin / curator / volunteer / client

## Project layout

```
server/      Django project (settings, urls, wsgi/asgi)
accounts/    Users, Profile, auth views, registration, password reset
myapp/       Help requests, events, broadcasts, photo reports, rating, AI, Telegram bot
templates/   HTML templates (base + accounts/ + myapp/)
static/      CSS, JS, video
media/       User-uploaded avatars and photo reports (not committed)
```

## Getting started

### 1. Clone and create a virtual environment

```bash
git clone <your-repo-url>
cd generation-connect

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
| `DJANGO_ALLOWED_HOSTS` | Comma-separated hosts | `localhost,127.0.0.1` (a `*` wildcard is refused once `DEBUG=False`) |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Full HTTPS origins allowed to POST | empty (set your prod origin(s)) |
| `DJANGO_BEHIND_TLS_PROXY` | Trust `X-Forwarded-Proto` from a reverse proxy | `False` |
| `DJANGO_DB_NAME` / `DJANGO_DB_USER` / … | PostgreSQL connection | SQLite |
| `DJANGO_DB_CACHE` | Use a shared DB cache for rate-limit counters | `False` (per-process LocMemCache) |
| `DJANGO_LOG_LEVEL` | App logger level | `INFO` |
| `SMTP_USER`, `SMTP_PASSWORD` | Gmail SMTP credentials | emails print to console |
| `EMAIL_TIMEOUT` | Seconds before a synchronous SMTP send is abandoned | `10` |
| `GROQ_API_KEY` | Groq API key for the AI assistant (must start with `gsk_`) | AI returns a fallback tip |
| `GROQ_MODEL` / `GROQ_ASSISTANT_MODEL` | Groq model IDs (fallback / primary tool-calling model) | `qwen/qwen3.6-27b` / `qwen/qwen3.8-27b` |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token | Telegram features disabled |
| `OSRM_BASE_URL`, `NOMINATIM_USER_AGENT` | Routing / geocoding for the `osm` maps provider | public demo endpoints (rate-limited, not for production) |

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

## Background jobs (production)

There is **no Celery / Redis / task queue** — scheduled work is plain management
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

Add to the deploy user's crontab (`crontab -e`). Use **absolute paths** to the
project's own virtualenv Python and `manage.py` — cron runs with a bare
environment:

```cron
# Generation Connect — alert curators/admins about tasks overdue past 3h, every 15 min.
*/15 * * * * cd /srv/generation-connect && flock -n /run/lock/gc-overdue.lock /srv/generation-connect/.venv/bin/python manage.py check_overdue_tasks >> /var/log/generation-connect/cron.log 2>&1
# Generation Connect — alert about pending requests stuck without a volunteer past 48h, hourly.
0 * * * * cd /srv/generation-connect && flock -n /run/lock/gc-stale.lock /srv/generation-connect/.venv/bin/python manage.py check_stale_requests >> /var/log/generation-connect/cron.log 2>&1
```

Replace `/srv/generation-connect` with the deployment path (the directory
containing `manage.py`) and `.venv` with the virtualenv location. `settings.py`
loads `.env` by absolute path, so credentials are picked up regardless of cron's
working directory; the `cd` is for `manage.py` and the log path.

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
python manage.py test            # full suite (528 tests, ~145s)
python manage.py test myapp      # one app
python manage.py test myapp.tests.MatchingAlgorithmTests   # one class
```

The suite covers roles and auth, registration and password-reset (regression tests), the
client → volunteer request lifecycle, rating updates, the AI fallback path, the geo/matching
services, the CRM dashboards, emergency/SOS, overdue and stale-request monitoring, in-kind
donation offers, the Lost & Found board, the health endpoints, external-service failure
handling, list-view pagination bounds, and a set of concurrency / IDOR / rate-limit regression
tests.

## Deployment

There is no container or IaC in the repo; a conventional Gunicorn + nginx + systemd setup:

1. **App server** — `gunicorn server.wsgi:application --workers 3 --bind 127.0.0.1:8001`
   (run under systemd; `WorkingDirectory` = the dir with `manage.py`, `EnvironmentFile` = the
   `.env`).
2. **Reverse proxy (nginx)** terminates TLS and serves static/media directly:
   ```nginx
   location /static/ { alias /srv/generation-connect/staticfiles/; }
   location /media/  { alias /srv/generation-connect/media/; }
   location = /health/ { proxy_pass http://127.0.0.1:8001; access_log off; }
   location / {
       proxy_pass http://127.0.0.1:8001;
       proxy_set_header Host $host;
       proxy_set_header X-Forwarded-Proto $scheme;   # required by DJANGO_BEHIND_TLS_PROXY
       proxy_set_header X-Real-IP $remote_addr;
   }
   ```
3. **Before the first boot with `DEBUG=False`:**
   - set `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS` (real hosts, no `*`),
     `DJANGO_CSRF_TRUSTED_ORIGINS` (e.g. `https://your-host`), `DJANGO_BEHIND_TLS_PROXY=True`;
   - `python manage.py collectstatic --noinput` (WhiteNoise is **not** configured — nginx serves
     `staticfiles/`);
   - `python manage.py migrate`;
   - `DJANGO_DB_CACHE=True` and `python manage.py createcachetable` — otherwise the login /
     password-reset / AI / Telegram-link rate limiters only throttle within a single Gunicorn
     worker (they use the per-process LocMemCache);
   - for PostgreSQL: `pip install "psycopg[binary]"` and set `DJANGO_DB_*`.
4. **Cron** — add the background jobs (see above). Use absolute venv/`manage.py` paths.
5. **Logs** — the app logs to stderr (`myapp` / `accounts` loggers, level `DJANGO_LOG_LEVEL`);
   journald/Docker captures them. Server errors (`django.request`, `ERROR`) and the
   zero-recipient safety nets in the overdue / stale / emergency sweeps go to stderr too.
   Administrative state changes (task assign/complete, application approve/reject, emergency and
   donation and pet transitions, rate-limit hits) emit one `INFO` line each with IDs only.
6. **Verify** — `curl -fsS http://127.0.0.1:8001/health/ready/` should return `{"status":"ready",...}`.

`DEBUG=False` automatically switches on `SECURE_SSL_REDIRECT`, HSTS (1 year, preload),
`SESSION_COOKIE_SECURE` / `CSRF_COOKIE_SECURE`; `SECURE_CONTENT_TYPE_NOSNIFF` and
`X_FRAME_OPTIONS=DENY` are always on. `EMAIL_TIMEOUT` bounds the synchronous SMTP send in
request handlers (default 10s).

## Health checks

Two public, unauthenticated, no-secret endpoints (`server/health.py`):

| Endpoint | Meaning | Cost | Codes |
| --- | --- | --- | --- |
| `GET /health/` | **liveness** — the Django process can serve a request | zero I/O | `200` |
| `GET /health/ready/` | **readiness** — DB reachable, config sane, shared cache (if configured) round-trips | one `SELECT 1` (+ one cache op) | `200` ready / `503` not ready |

Point the load balancer / `systemd` watchdog / uptime monitor at `/health/ready/`; use `/health/`
for a bare "is the process up" check. External services (Groq, Telegram, OSRM, Nominatim) are
**not** part of readiness — the app degrades gracefully without them, so their outage must not
pull an instance out of rotation. Responses carry `Cache-Control: no-store`.

A `systemd` unit can gate restarts on it:
```ini
ExecStartPost=/bin/sh -c 'for i in $(seq 30); do curl -fsS http://127.0.0.1:8001/health/ready/ && exit 0; sleep 1; done; exit 1'
```

## Backup & recovery

The application's durable state is **two things**: the database and `media/` (user-uploaded
avatars, photo-report and pet-report images). Neither is in git. **No backups are automated by
this repository** — the deploy owner must schedule them.

### Database backup (PostgreSQL)

```bash
# nightly, via cron on the DB host or the app host:
pg_dump --format=custom --no-owner --dbname="$DJANGO_DB_NAME" \
    --file=/var/backups/gc/db-$(date +\%F).dump
find /var/backups/gc -name 'db-*.dump' -mtime +14 -delete    # keep ~2 weeks
```
Store the dumps off-box (another host / object storage / offline media) — a backup on the same
disk does not survive a disk loss. For SQLite (dev only) the equivalent is copying
`Gen_connect.sqlite3` while the app is stopped, or `sqlite3 Gen_connect.sqlite3 ".backup ..."`.

### Media backup

`media/` is append-mostly application data, not a cache. Back it up with the same cadence as the
DB (`rsync -a --delete media/ /var/backups/gc/media/`, or a filesystem/volume snapshot). A DB
restore without the matching media leaves image URLs pointing at missing files.

### Restore / disaster recovery

Backup and restore are **separate procedures**. Assumed disaster-recovery posture: a full host
loss is recovered from the latest off-box DB dump + media copy; point-in-time recovery (WAL
archiving) is **not** set up and is left to the deploy owner if the RPO requires it.

1. Provision the host; install Python 3.12+, PostgreSQL client, nginx.
2. `git clone` the repo at the deployed commit; create the venv; `pip install -r requirements.txt`
   (plus `psycopg[binary]`).
3. Restore `.env` from your secret store (it is never in git).
4. Restore the database: `createdb "$DJANGO_DB_NAME" && pg_restore --no-owner -d "$DJANGO_DB_NAME" db-YYYY-MM-DD.dump`.
5. Restore `media/` from the latest copy.
6. `python manage.py migrate` (no-op if the dump is already current; safe to run).
7. `python manage.py createcachetable` (if `DJANGO_DB_CACHE=True`); `python manage.py collectstatic --noinput`.
8. Start Gunicorn (systemd), then nginx.
9. `curl -fsS https://<host>/health/ready/` — expect `200 {"status":"ready"}`.
10. Spot-check: log in, open a dashboard, open the map, load `/myapp/pets/`.

### Known production limitations (intentional)

- **No task queue.** Scheduled work is cron + idempotent management commands, by design.
- **No object storage.** Uploads live on the local `media/` volume; back it up with the DB (above).
- **No automated backups in-repo.** The `pg_dump` / `rsync` above must be scheduled by the operator.
- **No WAL archiving / PITR.** Recovery granularity is "last nightly dump".
- **AI assistant** depends on Groq; with no key it always returns the canned fallback tip.
- **`osm` maps provider** uses public OSRM / Nominatim demo endpoints — self-host or use a paid
  provider before real traffic (see `OSRM_BASE_URL`).
- **Rate-limit counters** need `DJANGO_DB_CACHE=True` (or Redis) to be effective across Gunicorn
  workers; the default `LocMemCache` throttles per-process only.

## Notes

- The SQLite database and uploaded media are intentionally **not** tracked in git; run `migrate`
  (and optionally `seed_demo`) after cloning to recreate them.
- `django-filter` / `crispy-forms` / `crispy-bootstrap5` are in `requirements.txt` and
  `INSTALLED_APPS` but currently unused (forms render through hand-written partials; list
  filtering is hand-rolled in the views).
