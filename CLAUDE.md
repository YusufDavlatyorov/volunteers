# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**KhayrKhoh** (production: `khayrkhoh.tj` / `www.khayrkhoh.tj`; formerly branded "Generation
Connect" — that name is gone from user-facing text and docs but the Python package/app names
(`myapp`, `accounts`), DB filenames and internal identifiers were deliberately left unchanged, see
"Production (khayrkhoh.tj)" below) — a Django (5.2, function-based views, MVT) platform
connecting elderly clients with volunteers across 5 regions of Tajikistan (Dushanbe, Sogd,
Khatlon, GBAO, RRP), coordinated by curators/admins. Custom user model with 4 roles
(admin/curator/volunteer/client), Groq-backed AI assistant, Telegram notifications, and a
geo-aware CRM (map, volunteer matching, routing) added in a later phase.

`README.md` has setup/env-var details. `PROJECT_OVERVIEW.md` is a deep, auto-generated snapshot
of the codebase — useful for context but **dated 2026-06-13 and stale**: it predates the CRM
dashboard, volunteer-application workflow, geo/matching services, and the test suite, all of
which now exist. Verify anything from it against the actual code before relying on it.

## No Docker — a hard rule

**This project does not use Docker** — not in production, not locally, not in scripts, tests or
docs (removed 2026-10-05). Write every command and every piece of code for running **directly**:
a Python 3.14 venv (`.venv`) locally, and on the server venv + **systemd** units + host nginx +
PostgreSQL 16 / Redis from apt. Concretely:

- No `Dockerfile` / `docker-compose.yml` / container images, and no instructions that start with
  `docker …`. A new long-running process gets a systemd unit in `deploy/systemd/` (same
  `User=deploy`, `WorkingDirectory=/opt/khayrkhoh`, `EnvironmentFile=/opt/khayrkhoh/.env`,
  `Restart=always` as the others — `myapp/tests_deploy.py` locks this); a periodic job gets a line
  in `deploy/crontab.example`; a service setting goes into the matching `deploy/` config.
- Services talk over `127.0.0.1` (PostgreSQL :5432, Redis :6379, the app :8000) — never Docker
  service names like `db`, `redis`, `web`.
- PostgreSQL-specific checks (migration forward/backward rehearsals) run against a local Homebrew
  `postgresql@16` (`brew install postgresql@16`; throw-away cluster pattern in
  `scripts/verify_backup.sh`), or on the rehearsal VM of `deploy/MIGRATION.md` (step R).
- The only remaining mentions of Docker are history (FEATURES_PLAN/SPEC, PROJECT_OVERVIEW, the
  rehearsal history in `docs/DEPLOY_FEATURE_PACK_V2.md`) and `deploy/MIGRATION.md`, which moves the
  live server *off* Docker — until its step 12 has run, the old containers still exist there.

## Commands

```bash
source .venv/bin/activate            # venv already present in repo (Python 3.14)
python manage.py runserver           # dev server → http://127.0.0.1:8000/
python manage.py migrate             # apply migrations
DJANGO_DEMO_MODE=True DJANGO_SQLITE_PATH=demo_stand.sqlite3 python manage.py seed_demo [--reset]   # demo stand ONLY (see "Demo stand" below); refuses without DEMO_MODE, with khayrkhoh.tj in ALLOWED_HOSTS or on a PRODUCTION_DB_NAMES database
python manage.py recalculate_achievements [--dry-run --demo-only --notify]   # badges from the data for every volunteer; idempotent, never revokes
python manage.py backfill_activity [--dry-run]           # UserDailyActivity from real timestamps only (see activity.py below); idempotent
python manage.py run_telegram_bot    # long-polling Telegram bot (separate process, not part of the request cycle)
python manage.py backfill_image_variants [--dry-run --strip-metadata]   # WebP thumb/medium for avatars / photo reports / pet photos uploaded before milestone 6 (+ strip EXIF/GPS from old originals in place); idempotent, run once at deploy
python manage.py geocode_missing [--limit N --dry-run --profiles]   # backfill lat/lng for HelpRequests (and --profiles) via Nominatim; sleeps 1.1s/row for the usage policy
python manage.py check_overdue_tasks [--dry-run]        # alert curators/admins about tasks overdue past 3h; idempotent, runs on cron (see README "Background jobs")
python manage.py check_stale_requests [--dry-run]       # mirror of the above for the *pending* side: alert about requests waiting >48h without a volunteer; idempotent, hourly cron
python manage.py create_initial_production_accounts    # prod-only bootstrap: exactly 4 accounts (admin/curator/volunteer/client) from INITIAL_<ROLE>_* env vars; idempotent, never seeds business data, refuses the demo password — see README "Initial production accounts"
python manage.py check_demo_password                   # read-only audit: accounts accepting seed_demo's public password + demo usernames present; exit 1 if any (deploy/MIGRATION.md steps 1/8)
python manage.py check_demo_data                       # read-only: everything seed_demo writes (accounts, rows, byte-identical demo photos in media/); ids/counts only; exit 1 if any — found = tell the owner, delete nothing (deploy/MIGRATION.md steps 4.5/8/11; deploy/deploy.sh runs it after every redeploy)
python manage.py rename_legacy_uploads [--dry-run] [--quarantine-unreferenced DIR --verify-backup media.tgz]   # pre-milestone-6 avatars/reports/pet photos under personal file names → <uuid>.<ext>, rows updated, old files removed; once at deploy with the app stopped (deploy/MIGRATION.md 6.7), before backfill_image_variants. Unreferenced leftovers: only counted, or MOVED (never deleted) outside MEDIA_ROOT after verifying media.tgz holds them byte-identical (step 8; deleted 30 days later, step 13)
curl -s localhost:8000/health/ ; curl -s localhost:8000/health/ready/   # liveness / readiness (server/health.py) — public, no secrets

python manage.py test --parallel 4                       # full suite (see FEATURES_PROGRESS.md for the current count)
python manage.py test myapp.tests.MatchingAlgorithmTests  # one test class
python manage.py test myapp.tests.MatchingAlgorithmTests.test_closer_volunteer_ranks_higher  # one test
python manage.py test accounts                           # one app
# Test modules that need a temp MEDIA_ROOT use myapp/testing.py::TempMediaRoot, never mkdtemp + rmtree in
# tearDownModule: parallel workers share that path and one would delete it under another.

# Production (on the server, as the deploy user — see "Production" below):
/opt/khayrkhoh/deploy/deploy.sh                          # redeploy: pull → pip → .env check → collectstatic → migrate → restart → readiness
journalctl -u khayrkhoh-web -f                           # app logs (also khayrkhoh-celery, khayrkhoh-telegram-bot)
sudo systemctl restart khayrkhoh-web khayrkhoh-celery khayrkhoh-telegram-bot   # after a .env change
```

No linter/formatter is configured in this repo.

Demo login (seeded by `seed_demo`, shared password `Volunteer2026!`): `admin_demo` (admin),
`malika_s` (volunteer), `client_zamira` (client).

## Architecture

Two Django apps: **`accounts`** (custom user model, auth, profile) and **`myapp`** (everything
else — help requests, events, broadcasts, CRM, map, AI, Telegram). `server/urls.py` mounts
`accounts.urls` at `/` and `myapp.urls` at `/myapp/`.

`myapp/models/` is a **package** (one module per domain — `help_requests.py`, `events.py`,
`volunteer_applications.py`, `photo_reports.py`, `emergency.py`, `donations.py`, `pets.py`), with
`myapp/models/__init__.py` re-exporting every public name. Import from `myapp.models` as before
(`from myapp.models import HelpRequest, OVERDUE_THRESHOLD`); new domain areas add a module here
plus an `__all__` entry. Moving a model between modules of the same app is not a schema change —
no migration.

### Roles

`accounts.Users` (`AbstractBaseUser` + `PermissionsMixin`) has three boolean flags —
`is_curator` / `is_volunteer` / `is_client` — mutually exclusive, enforced in `Users.clean()`.
Admin is `is_superuser`, not a flag. The `.role` property resolves to one of
`admin`/`curator`/`volunteer`/`client`/`guest`.

Access control is two decorators, both used together where needed:
- `@login_required` (Django's)
- `@role_required(*roles)` — defined in `myapp/views.py`; superusers pass any check that includes
  `"admin"`. **It is not wrapped with `functools.wraps`**, so wrapped views lose `__name__`.
  An anonymous **GET** is sent to `login?next=<this page>` (like `@login_required`, 2026-10-15 —
  a visitor's "Request help" must land on the form after signing in); a POST goes to plain `login`.

**Curators are national coordinators — no CRM view is scoped to a curator's own region.** The
task/people/emergency/matching lists offer region as a *filter*, not a boundary; a curator in
Dushanbe can legitimately triage and dispatch a Sogd task. Region-scoping applies only to the
*volunteer* task feed (`task_list` / `map_data_view` volunteer branch) and to notification
fan-out (`volunteer_queryset_for_region`). What a curator can't do that an admin can:
see the curator directory (`people_list/curator`) and review volunteer applications. Curators
*do* review in-kind donation offers (`donations_admin` + `donation_update` are
`@role_required("admin", "curator")`) — there is no money on the platform, so nothing is a
"financial" view anymore; catalogue (`Product`) management stays in the Django admin.

Registering as "volunteer" no longer grants the role directly — it creates a pending
`VolunteerApplication`, reviewed by an admin (`approve()`/`reject()` flip `is_volunteer` and
timestamp the review). See `myapp/models/volunteer_applications.py::VolunteerApplication` and the
`volunteer_application*` views/URLs.

State-changing actions (`accept_task`, `complete_task`, `task_advance_stage`,
`task_assign_volunteer`, `task_notify_volunteer`, `emergency_report`, `emergency_update`,
application approve/reject, `logout`, `check_overdue`, Telegram unlink) are `@require_POST`;
templates must submit them as forms, not `<a href>` links.

### Task lifecycle (`HelpRequest`)

`status` is the coarse lifecycle (`pending` → `active` → `completed`/`cancelled`). Two fields
layer on top of `active`:

- **`priority`** (`normal`/`high`/`emergency`) is the graded urgency new code reads (map markers,
  dispatch, matching weights). The legacy **`is_urgent`** boolean is still a stored column —
  `HelpRequestForm.save()` keeps it in sync (`is_urgent = priority != "normal"`) so existing
  `.filter(is_urgent=...)` sites keep working. `HelpRequestForm.priority` is `required=False`
  with a `clean_priority` default of `normal` (tests build the form without it).
- **`work_stage`** (`assigned`/`en_route`/`arrived`/`in_progress`) is the assigned volunteer's
  on-the-ground progress — only meaningful while `status == "active"`, and it never touches
  `status`. Move it with `HelpRequest.advance_work_stage(target)`: forward-only (uses
  `WORK_STAGE_ORDER`), active-only, raises `ValueError` otherwise. The `task_advance_stage_view`
  is driven by the assigned volunteer; curator/admin can correct it. `en_route`/`arrived`
  notify the client.

On the `pending` side, **`stale_alert_sent`** (a stored boolean) layers on the same way
`alarm_sent` does for overdue: `HelpRequest.is_stale_pending` is `pending` + waiting past
`STALE_PENDING_THRESHOLD` (48h), derived not stored. See `services/stale.py` below.

**Photo-report review gates the reward (2026-10-17).** `PhotoReport.status` is `pending` (every
new upload) → `approved` | `rejected` (+ `reviewed_by` / `reviewed_at` / `rejection_reason`, required
on reject), model methods with the conditional-UPDATE guard; migration 0028 made all older reports
`approved`. `task_actions.complete` still completes the task (client notice, review prompt, chat
close) but pays the rating points + `task_completed` badges **only with an approved report by the
task's volunteer** — otherwise it sets `HelpRequest.reward_pending=True` and
`services/photo_reports.credit_task` pays once on approval (conditional `UPDATE … WHERE
reward_pending`). The public rating and the badge counters count completed tasks with
`reward_pending=False` only (pre-review tasks have False). CRM: `/myapp/crm/photo-reports/`
(`views_photo_reports.py`, admin + curator; default filter pending, newest first; pending count =
badge on the panel's "Photo reports" link); the author gets `photo_report_approved` /
`photo_report_rejected` (site + Telegram, the reason in the text) and sees the status on the
dashboard + `/myapp/reports/`. Public pages (landing, the reports list, sitemap lastmod) show
approved reports only (`photo_reports.public_reports()` / `visible_to(user)`). The Django admin keeps
the review fields read-only. **Delete** (admin + curator, any status, `data-confirm`; list + detail,
`POST …/<pk>/delete/`; the Django admin's delete goes through the same `photo_reports.delete`): row +
original + WebP variants gone, author notified `photo_report_deleted` (site + Telegram), one ID-only log
line. Deleting the approved report that paid its task (and no other approved one by that volunteer
remains) = `revoke_task_reward`: `reward_pending=True` again (conditional UPDATE), points subtracted
(floor 0), and `achievements.revoke_lost(user, before)` drops exactly the counter badges that task
earned — the one exception to "achievements never revoke". **Bulk**: list checkboxes (`form="prBulkForm"`,
rows keep their own delete form) → `POST crm/photo-reports/delete/` → `photo_reports.delete_many` (each
report exactly like a single delete, ≤ `BULK_DELETE_MAX` = 100, one JS confirm with the count, ONE notice
per author — the `many` variant with `{count}` for several). Pre-0028 reports are `approved` and go the
same way; files are deleted by the row's *current* name (`rename_legacy_uploads` already removed the
old-named file + variants) — test-locked with a real 0027 → 0028 migration in
`LegacyReportFromBeforeReviewTests`. Tests: `myapp/tests_photo_report_review.py`.
**Admin adds a report (2026-10-18)**: `/myapp/crm/photo-reports/new/` (`crm_photo_report_create_view`, **superuser only — a real 403 for everyone else**, not the `role_required` bounce; `?task=<id>` pre-selects; links on the CRM list + `task_detail` for admins) — any task, on behalf of **the task's assigned volunteer only** (the field's queryset is that one user, so nobody else validates; the select is rebuilt from `data-volunteer` / `data-volunteer-label` on each task option) or, left empty, as the admin. **"Сразу одобрить" (`approve_now`, on by default)** calls `photo_reports.approve` right after creating — the same approval as the review button. Both this form and the volunteer's `/reports/` form create through **`photo_reports.create(report, image, author=, uploaded_by=)`** (metadata-free image, `pending`, variants queued). `approve` / `reject` never notify the reviewer about their own report. `PhotoReport.uploaded_by` (migration 0029, null on older rows; the Django admin sets it on add) records who really uploaded; `added_by_admin` drives the "Добавлено администратором" mark (`_photo_report_admin_mark.html`: CRM list — a small wrapping plate under the name so the nowrap row keeps its title column — detail, public list). The reward rule is unchanged (author must be the task's volunteer), so a report as the admin never pays the admin. **Two dates (2026-10-18, migration 0030)**: `event_date` ("Дата проведения", `DateField`, default `photo_reports.local_today()` = **Asia/Dushanbe** day — `TIME_ZONE` is UTC; 0030 backfilled it from the local day of `created_at`, reverse = noop) is when the help took place; `created_at` ("Дата публикации") is the upload. Default ordering `[-event_date, -created_at]`. Site forms (`PhotoReportForm`, `AdminPhotoReportForm` via `_EventDateMixin`): `<input type=date>` with min/max, `clean_event_date` = not in the future, not older than `EVENT_DATE_MAX_AGE_DAYS` (30) (codes `event_date_future` / `event_date_too_old` → `err.*`); default = the task's completion day, else the event's day, when inside that window, else today (options carry `data-date`, `static/js/photo-report-date.js` pre-fills until the person edits). The Django admin edits `event_date` without the 30-day limit; `created_at` read-only, `date_hierarchy` / `list_filter` on `event_date`. **Public pages show only `event_date`** via `{% local_date %}` (`i18n_plural.py`: `|date:"j E Y"` under the server-render language + `data-i18n-date`, which `setLanguage()` relabels with `act.mg*`); CRM list/detail show both. Period logic uses `event_date` (CRM "from / to" filter, activity calendar); `created_at` stays for the CRM review-queue order, the volunteer dashboard's "my latest uploads" order and the sitemap `lastmod` (the page changes when a report is published). Tests: `myapp/tests_photo_report_event_date.py`. Tests: `myapp/tests_photo_report_admin_create.py`.

**Dispatch actions** (all admin/curator, on a `pending` task): `task_recommendations_view`
returns the ranked JSON shortlist (read-only); `task_notify_volunteer_view` pings one volunteer
but leaves the task `pending` (they still self-accept); `task_assign_volunteer_view` is the
direct-assign — it flips the task to `active` with the **same conditional `UPDATE ... WHERE
status='pending'`** guard as `accept_task_view`, so a curator assign and a volunteer self-accept
can't both win.

**Emergency / SOS** (`EmergencyReport`, always linked to one `HelpRequest`): the *assigned*
volunteer of an *active* task raises one from the danger button (`emergency_report_view`, POST,
`@role_required("volunteer")` + must be `task.volunteer` and `status="active"`). Lifecycle
`open → acknowledged → resolved` (+ `cancelled` from either open state). **`open → resolved`
directly is deliberately allowed** (`ALLOWED_TRANSITIONS`, test-locked in
`EmergencyModelTests.test_open_can_go_straight_to_resolved_or_cancelled`) — staff can close a
false-alarm SOS in one step without a mandatory acknowledge; the "normal" narrative path is
still `open → acknowledged → resolved`. Transitions are
`EmergencyReport` model methods, driven only by curator/admin through `emergency_update_view`
(`action=acknowledge|resolve|cancel`). `_can_view_emergency` gates the detail page: staff see
every report, a volunteer sees only their own (read-only, no controls). CRM list + tabs at
`/myapp/emergency/`; open count on the admin dashboard; open+located reports show as `--danger`
`gc-pin--emergency gc-pin--sos` (pulsing) markers in `map_data_view` — staff see all, a volunteer
those of their own region (no name, no link except their own report). **Live:** the service publishes
`sos.marker` (`realtime/publish.sos_marker`) to `staff` + `volunteers_<region>` on report /
acknowledge (point) and resolve / cancel (removal, no coordinates); `SosGuard` re-checks role + current
region per socket; region volunteers also get one `sos_nearby` notification (in-app + Telegram, no
location). The ops map adds/drops the marker without a reload; `?sos=<id>` centres on it. See `services/emergency.py`.

### Security & abuse controls

A hardening pass added a cross-cutting layer that is easy to regress — keep it intact:

- **Tokens hashed at rest.** `accounts.models.hash_token` (SHA-256) — only the *hash* of a
  password-reset / email-verification / Telegram-link token is stored; the raw value lives only
  in the emailed URL or a one-time UI reveal. `generate_*_token()` returns the raw token and
  persists the hash; every lookup hashes the incoming token before querying.
- **Cache-based rate limiting.** Login (per-IP **and** per-username counters), password reset
  (per-IP), Telegram-link redemption (per-chat) and the AI assistant (`ai_chat_view`, per-user)
  throttle via `django.core.cache`. Production uses **Redis** (`DJANGO_REDIS_URL` =
  `redis://127.0.0.1:6379/0`, the local redis-server; sessions are `cached_db` then), overriding
  `DJANGO_DB_CACHE`, which stays only as a dependency-free fallback. Without either, `CACHES` is
  the process-local `LocMemCache` (dev/tests), where the web throttles only bite within one worker.
  **The IP comes from `server/client_ip.py::client_ip()` — never `REMOTE_ADDR` directly**: behind
  nginx `REMOTE_ADDR` is the proxy's address for everyone (milestone 6 fix, found in the Docker era
  — all visitors shared one login/password-reset bucket). The uvicorn worker
  (`server/asgi_worker.py`) runs with `proxy_headers=False` on purpose, so this helper stays the
  only place that reads proxy headers. It trusts nginx's `X-Real-IP` only with
  `DJANGO_BEHIND_TLS_PROXY=True` and a loopback/private peer, and `CF-Connecting-IP` only with
  `DJANGO_BEHIND_CLOUDFLARE=True` and an address in `CLOUDFLARE_IP_RANGES` (`docs/CLOUDFLARE.md`).
- **Secrets never reach logs.** `server/log_filters.py::RedactSecretsFilter` (on the `console`
  handler) cuts Telegram bot tokens (`<id>:<secret>` — they live in the API URL) out of every log
  line and traceback; `httpx` (python-telegram-bot's client, logs URLs at INFO) is pinned to
  WARNING. Never log a `requests`/`httpx` exception object itself (its text has the URL).
  Both bots start polling only through `myapp/telegram/polling.py::run` — `bootstrap_retries=-1`
  (a network outage at start no longer kills the process) and PTB's `InvalidToken` (whose text
  contains the token, printed past the log filter if uncaught) becomes a clean `CommandError`
  with exit code 78 (`polling.EXIT_INVALID_TOKEN`, EX_CONFIG).
  `myapp/tests_telegram_resilience.py` runs both bots against a fake Bot API and checks their
  whole output for the token.
- **Local `.env` holds no delivery keys.** `server/dev_safety.py` prints a banner on every
  DEBUG run whose env has `SMTP_USER/PASSWORD` or a Telegram bot token (names only, never
  values); bot testing uses a separate test bot (`docs/TEST_BOT.md`).
- **Upload size cap.** `accounts.models.validate_file_size` (`MAX_UPLOAD_SIZE_MB` = 5) guards
  every `ImageField` (avatars, photo reports). `update_profile` calls it explicitly because
  `Model.save()` skips field validators. `update_profile` also validates `region` against
  `REGION_CHOICES` and coerces `age` (it has no ModelForm).
- **Uploaded photos carry no metadata (milestone 6).** Every avatar / photo-report / pet upload
  goes through `services/images.py::clean_original()` before it is stored — decoded (a non-image is
  rejected), re-encoded without EXIF/GPS/XMP, downscaled to `MAX_ORIGINAL_SIDE` = 2560 px on the long
  side (owner 2026-10-04; the backfill's in-place strip keeps old sizes), saved as `<uuid>.<ext>` (the user's file name can be
  their real name; media URLs are public). Photo reports appear on the anonymous landing page, so a
  phone original would publish where it was taken. Views: `update_profile`, `edit_profile_view`,
  `photo_reports_view`, `pet_report_create/edit_view`; Django admin via
  `images.CleanImageAdminMixin`. Chat photos had this since milestone 3 (`chat.send_message`).
  After saving, `images.queue_variants(obj)` builds WebP `thumb` 320 / `medium` 960 in Celery
  (`image_variants` JSON on `Profile`, `PhotoReport`, `PetReport`); templates render them with
  `{% responsive_image %}` (`myapp/templatetags/responsive.py`: `<picture>` + WebP `srcset`, the
  original stays the `<img>` fallback, so no variants yet = the old markup).
- **A volunteer sees the client's phone only after accepting** — `access.can_see_client_phone`
  (staff always, the client, the assigned volunteer); `task_detail.html` shows "appears after you
  accept" otherwise. The bot's `_task_card(show_contacts=…)` follows the same rule.
- **Coordinate validation.** `myapp.services.geo.is_valid_coordinate` enforces real WGS84
  ranges in `HelpRequestForm` / `ProfileForm`, in `PetReport.clean()` (closes the Django-admin
  edit path), and in `pets.possible_matches()` before any `haversine_km` (never trust a stored
  pin); a bare `DecimalField(max_digits=9)` would accept nonsense like latitude 800.
- **Donation quantity validation.** `Donation.quantity` carries `Min/MaxValueValidator`s
  (1..`MAX_DONATION_QUANTITY`) in addition to `Donation.clean()` and the service/form checks, so
  the bound holds on any `full_clean()` path (admin, shell), not only the donate flow. There is
  **no money field** — donations are physical goods (Stage 9).
- **Startup guards.** `server/settings.py` raises `ImproperlyConfigured` if `DJANGO_SECRET_KEY`
  is unset (no built-in fallback anymore) or if `DJANGO_DEBUG=False` with a `*` in
  `ALLOWED_HOSTS`. `DEBUG` now defaults to **False**; secure-cookie / HSTS (preload) /
  SSL-redirect settings switch on automatically whenever `not DEBUG`. `CSRF_TRUSTED_ORIGINS`
  (`DJANGO_CSRF_TRUSTED_ORIGINS`) and `SECURE_PROXY_SSL_HEADER` (`DJANGO_BEHIND_TLS_PROXY`) are
  env-driven for a reverse-proxy deployment. `LOGGING` routes the `myapp`/`accounts` loggers to
  stderr — without it the zero-recipient `ERROR` safety nets are silently dropped under `DEBUG=False`.
- **`accept_task_view` / `complete_task_view` concurrency.** `accept_task_view` uses a
  per-volunteer `cache.add()` lock plus a conditional `UPDATE ... WHERE status='pending'`;
  `complete_task_view` uses a conditional `UPDATE ... WHERE status='active'` (not
  `task.complete()`) so a double-click can't complete twice or award the rating points twice.
  Neither is a DB constraint — CRM/admin dispatch and matching's workload scoring legitimately
  model a volunteer holding several active tasks; "one active task" is a rule of the
  self-service accept flow only.
- **`PhotoReportForm.help_request`** is scoped to the volunteer's own tasks (staff see all) —
  its option labels carry the client's username, so an unscoped queryset enumerated every
  request in the system.
- **Map JSON bounds.** `map_data_view` and `pets.open_board_points()` cap each marker category
  (`MAP_TASK_LIMIT` / `MAP_VOLUNTEER_LIMIT` / `MAP_EMERGENCY_LIMIT` / `MAP_POINT_CAP`, ordered so
  the cap keeps the most relevant rows). Not a permission boundary — the querysets are already
  scoped — a bound on response size.
- **Emergency dedup.** `EmergencyReport` *does* carry a partial `UniqueConstraint` on
  `(help_request, volunteer)` for the open statuses — the LocMemCache cooldown alone can't span
  Gunicorn workers, and a duplicate SOS row / duplicate staff alert is a real failure. The
  service catches the resulting `IntegrityError` and returns the existing report.
- **Tajik phone numbers** (`myapp/validators.py`): `normalize_tj_phone` accepts `+992`/`992` + 9
  digits or 9 bare digits (spaces, dashes, dots, parentheses ignored) and stores `+992XXXXXXXXX`;
  `validate_tj_phone` is on `HelpRequest.phone` / `PetReport.contact_phone`, and both models'
  `clean()` normalize (so the Django admin is covered), forms' `clean_<field>` too. Error code
  `invalid_phone` → `err.invalid_phone` (EN/RU/TJ). Inputs use `forms.TelInput`
  (`type=tel`, `inputmode=tel`, `+992 XX XXX XX XX` placeholder, blur mask in `base.html`).
- **Name limits**: `Users.USERNAME_MAX_LENGTH` = 30, `FULL_NAME_MAX_LENGTH` = 60 (+ form `maxlength`,
  + `update_profile`, whose JSON error carries `error_i18n: err.max_length`). `Profile.full_name` is a
  **60-char column** since accounts 0013 (owner 2026-10-09) — that migration refuses to run (ids only,
  no truncation) while a longer name exists; `username` keeps its 150 column + a validator.
  **Users change their own username** on edit-profile (`UserUpdateForm`, 2026-10-17): registration's
  rules (trimmed, 4–30) + unique **ignoring case** (registration itself still checks exactly); an
  unchanged / not-posted username skips every check (legacy names don't block other edits); the view
  clears the rating cache. No URL takes a username (test-locked, `accounts/tests_username_change.py`). Long names never widen a page: list rows
  ellipsis (`.row-rank h3`), headings/detail wrap (`overflow-wrap: anywhere`), flex/grid items
  `min-width: 0`, `.request-row` uses `minmax(0, 1fr)`.
- **`_can_view_task`** (`myapp/views.py`) gates task-detail access: a volunteer sees only their
  own assigned tasks plus pending tasks that would show up in their region-filtered
  `task_list` — not every `status="pending"` row.

### Geo / CRM layer (`myapp/services/`)

Business logic that needs to be unit-testable without the ORM or network lives here, kept out of
`views.py` on purpose:

- **`geo.py`** — low-level, pure `(lat, lng)` math (`haversine_km`, `is_valid_coordinate`) and
  the OSRM routing client (`get_route`). `get_route` **never raises**: any OSRM failure (timeout,
  no route, unset `OSRM_BASE_URL`) returns a `{"success": False, ...}` dict with a
  haversine-distance fallback, so a broken routing provider degrades gracefully instead of 500ing
  a view. Its import surface is test-locked — don't change it; call `maps.py` from new code.
- **`maps.py`** — provider-agnostic facade over everything the product needs from a "maps API":
  `vector_style()` (the MapLibre vector style: `MAPS_VECTOR_STYLE` = `openfreemap` default/keyless,
  `maptiler` + `MAPTILER_API_KEY` (browser-side key by design), `off`), `tile_layer()` (raster
  config — the maps' fallback style), `route()` (OSRM via `geo.get_route`, or **OpenRouteService**
  when `ORS_API_KEY` is set / `ROUTING_PROVIDER=ors` — `routing_provider()`; ORS is called only
  server-side with the key in a header, its steps are mapped onto OSRM maneuver codes, and
  `manage.py test` blanks the key — and `NOMINATIM_BASE_URL` (geocoding off; geocoding tests
  opt in with `override_settings`), so **no test reaches the network**; **walking always goes to ORS `foot-walking` when a key exists**,
  else to `OSRM_FOOT_BASE_URL` — default FOSSGIS' foot server, because
  `router.project-osrm.org` is car-only and silently answers "foot" with the car route).
  `route(..., preference="fastest"|"shortest", alternatives=True)`: ORS `preference` +
  `alternative_routes` (retried without alternatives on ORS error 2004); on OSRM, `alternatives=3`
  and the pick by distance/time. Results carry `routes` (≤3, preferred first) and, on failure,
  `reason` (`no_route`/`too_far`/`unavailable`/`not_configured`); walking > `WALKING_MAX_KM`
  (40 km straight line) is never requested. `routing.cache_key` includes profile + preference +
  alternatives. The task page draws the chosen variant blue (dashed on foot), the others grey and
  clickable (`gc-route-alt`); `geocode(query,
  region=)` → `(lat, lng)|None`, `reverse_geocode()`, `provider_name()`. Switching providers is
  one `.env` change (`MAPS_PROVIDER`: `osm` default/keyless, `carto` keyless, `mapbox` raster +
  `MAPS_API_KEY`, `google` raises — needs the JS SDK). Same discipline as `geo.py`: pure I/O, never
  raises for an expected failure. **New map/route/geocode code calls `maps.*`, not `geo.*` directly.**
  `tile_layer()` also returns a keyless CARTO `fallback`: `static/js/map.js` `createMap()` walks
  vector style → raster `tile_layer()` → CARTO after repeated errors (e.g. OSM's 403 "Access
  blocked"), then shows a localized `.map-error` (`map.error_load`) if that fails too. OSM is the single host
  `tile.openstreetmap.org` and **needs a Referer** — hence `SECURE_REFERRER_POLICY =
  "strict-origin-when-cross-origin"` (Django's default `same-origin` sent none). The maps are
  **MapLibre GL 5** (v6 is ESM-only — stay on the UMD 5.x line), loaded via
  `partials/_maplibre_css.html` / `_maplibre_js.html`: unpkg (SRI) first, then the identical
  `static/vendor/maplibre-gl-5.24.0/` copy (sourceMappingURL stripped) only if unpkg fails
  (version + SRI: `myapp/templatetags/maplibre.py`, the one place to bump).
  3D buildings = the style's `building` fill-extrusion (OpenMapTiles `render_height`), 2D/3D
  toggle remembered in `localStorage['gc-map-3d']`. The route line is two GeoJSON line layers
  added with `beforeId` = the first building layer (never over a house), blue `#2A3BA5` + white
  casing; a failed road route draws **no** line — task-route.js shows `route.road_unavailable`.
  Every map container is `.gc-map` (`position: relative; z-index: 0; isolation: isolate`) under
  the `.topbar` (`z-index: 1000`) — test-locked in `myapp/tests_map3d.py`. Marker roots must not
  get a CSS `position` (MapLibre positions them absolutely).
- **`matching.py`** — two directions, one algorithm. `recommend_volunteers(task)`: deterministic,
  explainable 0–100 scoring (distance/availability/workload/region/**skills**/freshness, urgent
  tasks weight distance higher) for admin/curator dispatch. `recommend_tasks(volunteer)`: the
  **mirror adapter** for the volunteer dashboard — reuses the exact same primitives
  (`_distance_score`, `_skill_score`, `haversine_km`, the km caps) from the volunteer's point of
  view over *pending* tasks, dropping the components that aren't task signals and adding priority +
  wait-time. Both are read-only and never assign/notify. Both sort with a final `id` tie-breaker
  so identically-scored candidates never swap order between calls, and both bound the candidate
  set with the same SQL proximity pre-filter (`CANDIDATE_CAP` / `TASK_REC_CANDIDATE_CAP`, kept
  generous) so one call can't haversine an unbounded directory. `location_freshness_label()` is a
  public wrapper reusing the freshness thresholds. Distinct from the Groq-based conversational AI
  assistant in `views.py::ai_chat_view` — do not conflate the two "AI"s.
- **`analytics.py`** — aggregate CRM dashboard queries (`dashboard_stats`, `recent_activity`,
  `users_by_role`, `region_task_breakdown`, `platform_totals`, availability/task breakdowns),
  built with annotated `Count`/`Q` aggregates rather than per-row Python loops, so query count
  stays constant regardless of data volume. The overdue/stale counts reuse
  `overdue.currently_overdue_q()` / `stale.currently_stale_pending_q()` — the one predicate each,
  also used by the sweeps, the dashboard queues and the `crm/tasks/?overdue=1|?stale=1` filters,
  so the threshold comparison lives in exactly one place per side.
- **`dashboard.py`** — `for_user(user)`: the one role-aware dashboard payload, dispatched on
  `user.role`, **every query scoped to the passed user**. Composes `analytics` / `emergency` /
  `overdue` / `stale` / `matching` / `pets` — no aggregation is duplicated. Rendered by `myapp/dashboard.html`
  (`dashboard_view`, **`/dashboard/`**, "what do I do now" — `LOGIN_REDIRECT_URL`, `/` for a signed-in user,
  every "no access" bounce; the old `/myapp/dashboard/` redirects there) via
  `templates/myapp/dashboard/_<role>.html` partials + Quick Actions + the application-status card + a
  stale-location banner. **Dashboard design (2026-10-16, `static/css/dashboard.css`, `dash-` classes,
  tokens only — no hex/rgba, no inline styles; `myapp/tests_dashboard_design.py`)**: normal site navbar
  (**no sidebar** — owner reversed it the same day), one `.section` container (1200px). Staff: hero
  (greeting, SOS/overdue alert plate only when there is one, 6-column KPI strip from `dashboard_stats()` —
  which gained `tasks_total` / `tasks_cancelled` / `tasks_created_week` / `events_upcoming`, same
  aggregate) → readiness / donations / region doughnut (3→2→1 columns) → request queue 2/3 (one text line
  per row, `table-layout: fixed`, cells cut with "…" + `title`; `DASH_QUEUE_LIMIT` = 11 rows cut in the
  service, urgent then longest-waiting; category tabs count the WHOLE pending queue
  (`_queue_categories`) and link to `crm/tasks/?status=pending&help_type=…`, which paginates by 20 and
  keeps the filters; "shown X of N" only when N > 11) + activity feed & rating leader 1/3 → two rows of
  three same-size compact cards (SOS, overdue, applications [admin]; offers, Lost & Found, 14-day
  chart) built from one header + item component (`_card_head.html`, `_list_card.html`,
  `_list_item.html`: two fixed lines, `CARD_LIST_LIMIT` = 4, centred empty state) → Quick Actions tiles
  (incl. the CRM pages the navbar lacks). Other blocks: **real data only — nothing to show, not
  rendered.** Doughnut + 14-day line are Chart.js + `GCChartTheme` in `static/js/dashboard.js` (no
  colours in the page script; the old SVG sparkline is gone). Volunteer/client: same hero + 4-KPI strip + their lists. **`/profile/` is a separate "who I am" page** (2026-10-15, `accounts.views.profile_view`
  → `accounts/profile.html`: user card, activity calendar, achievements, saved-location card, community
  card, volunteer review summary) — it never calls `for_user()`; `myapp/tests_dashboard_split.py` locks the split.
  Links: actions on tasks/applications/login → `dashboard`; profile edit / Telegram / language / location →
  `profile`. `admin_panel_view` stays the deep CRM
  console; the curator/admin dashboard is the attention-triage summary that links into it.
  `_public_volunteer()` is the only volunteer data a client's dashboard may show (name + region,
  never contact details). The curator dashboard's stale/unassigned queues get a
  `task.assignment_suggestion` attached — `attach_assignment_suggestions()` gives the same answer as
  `matching.recommend_volunteers(task, limit=1)` per task, but from ONE candidate query for the whole list
  (`matching.best_volunteer_per_task`: same filters, same `CANDIDATE_CAP` nearest, same `_rank`; above
  `BULK_POOL_CAP` eligible volunteers it falls back to one call per task — equality test-locked in
  `myapp/tests_admin_panel.py`), read-only, so a curator sees the top pick inline without opening the
  dispatch shortlist.
  **`/myapp/panel/` (`admin_panel_view`, 2026-10-16) is built from the same components** — loads
  `dashboard.css` + `static/css/panel.css` (panel-only extras), reuses `_alert.html` / `_queue_row.html` /
  `_feed.html` / `_list_card.html` (+ `event` / `broadcast` item kinds): hero (title, alert plate, primary
  "Task management" + icon buttons in `_panel_links.html`, a `<details>` "More" menu below 768px; "Check
  overdue" stays a POST form), three KPI bands (SOS / people / tasks, each number the old filter link,
  red when open SOS / overdue / urgent > 0), cards (SOS, applications [superuser] or events [curator],
  broadcasts; events + activity), then ONE request table: `?tab=free|busy`, the `HelpRequestFilterForm`
  filters (finally rendered), `dashboard.panel_requests()` = both tab counts in one aggregate +
  `PANEL_PAGE_SIZE` = 20 rows per page (Paginator, count reused from the aggregate) + batched candidates;
  tab / page / "open in task management" links keep the filters. Constant query count (25 vs the old 29).
- **`overdue.py`** — `sweep_overdue_tasks()`: the one overdue-detection + alerting path, shared
  by `check_overdue_view` (admin button) and the `check_overdue_tasks` cron command. Claims each
  task with a conditional `UPDATE ... WHERE alarm_sent=False` before notifying `staff_recipients()`,
  so concurrent sweeps never double-alert; idempotent by design. `find_overdue_tasks()` is the
  sweep's "still needs a first alert" set (`alarm_sent=False`); `currently_overdue_tasks()` is the
  broader "overdue right now" set for the CRM/dashboard.
- **`stale.py`** — the exact mirror of `overdue.py` for the *pending* side. `sweep_stale_pending()`
  alerts curators/admins about `pending` requests waiting past `STALE_PENDING_THRESHOLD` (48h)
  without a volunteer, claiming each with a conditional `UPDATE ... WHERE stale_alert_sent=False`
  first; idempotent, driven only by the `check_stale_requests` cron (no admin button — unlike
  overdue). `find_stale_pending()` is the "still needs a first alert" set; `currently_stale_pending()`
  the broader "stuck right now" set for the curator dashboard and `crm/tasks/?stale=1`.
- **`emergency.py`** — volunteer SOS reports (`myapp/models/emergency.py::EmergencyReport`). A
  volunteer on an *active* task raises one via the danger button; `report_emergency()` dedupes
  in **three layers** — a fast check-then-create, a partial `UniqueConstraint` on
  `(help_request, volunteer)` over the open statuses (a cross-worker guarantee, not just the
  LocMemCache lock), and an `IntegrityError` handler that returns the row that won the race —
  so a double-click / retry / concurrent POST never makes a second row or a second staff alert.
  It resolves a location (explicit coords → task → volunteer profile → none, via
  `geo.is_valid_coordinate` — invalid coords are dropped, never stored) and fans out to
  `staff_recipients()` once (`notify_staff`, idempotent via `EmergencyReport.notified_at`). If
  delivery reaches 0 recipients it's logged `ERROR` and the report stays `open`/visible; staff
  have a manual **"Re-alert staff"** button (`notify_staff(force=True)`). State transitions are
  **model methods** (`.acknowledge()` / `.resolve()` / `.cancel()`, raising `ValueError` on an
  illegal move — `open → acknowledged → resolved`, `cancelled` from either open state, both
  terminal); the service wraps them to also notify the reporter. Curator/admin only for
  transitions; `check_overdue`-style CRM at `/myapp/emergency/`.
- **`donations.py`** — **in-kind** donations: material assistance, **no money anywhere**
  (Stage 9 replaced the money model). `myapp/models/donations.py`: `Product` is a catalogue of
  *needed items* (name / `category` / `unit`, no price); `Donation` is an *offer of physical
  goods* (`donor_type` individual|business + `organization_name`, `product` **or** `item_name`,
  `category`, `quantity`, `unit`, `fulfilment` pickup|dropoff, `location`, `region`,
  `assigned_volunteer`). Lifecycle `pending → approved → ready → received → distributed`
  (+ `cancelled` from any open state), forward-only model methods (`approve` / `mark_ready` /
  `mark_received` / `mark_distributed` / `cancel`) using the same conditional
  `UPDATE … WHERE status=<old>` guard as `accept_task_view`; `assign_volunteer()` is a separate
  save allowed only while `approved`/`ready`. `create_offer()` (service) is the one creation
  path; `offer_summary()` gives item-count / businesses-helping metrics (never money).
  Donor-facing pages (`donate`, `my_donations`, `donation_detail`) are `@login_required` with a
  donor/assigned-volunteer/staff object gate (`_can_view_donation`); the assigned volunteer may
  POST only `received`/`distributed`. `donations_admin` + `donation_update` are
  `@role_required("admin", "curator")` — **curators review offers, approve, and assign
  volunteers**. `assigned_donations` (`/myapp/donations/assigned/`) is the volunteer's delivery
  queue. `ProductAdmin`/`DonationAdmin` in Django admin stay read-only for `Donation`.
  **"Donate goods" page** (`/myapp/donate/goods/`, `donate_goods_view` + `DonationGoodsForm`,
  2026-10-17; the navbar's "Donate goods" link + a dashboard Quick Action point here, the older
  `/donate/` catalogue form still exists) uses the login/register `.auth-scene` look and creates an
  ordinary `Donation` through `create_offer` (+ `condition`, `contact_phone`, up to
  `MAX_DONATION_PHOTOS` = 5 `DonationPhoto`s — migration 0027). Statuses as above (new = pending,
  accepted = approved, handed over = distributed). Donation photos are **private** like chat photos:
  metadata-free `<uuid>.<ext>` under `donations/`, nginx refuses `/media/donations/`, served only by
  `donation_photo_view` behind `_can_view_donation`. Tests: `myapp/tests_donate_goods.py`.
- **`pets.py`** — the Lost & Found board (`myapp/models/pets.py::PetReport`: a `lost`/`found`
  flag, optional photo + location, lifecycle `open → matched → resolved`/`closed` via model
  methods `mark_matched` / `reopen` / `resolve` / `close`, `ValueError` on an illegal move).
  **Privacy is the point**: `public_point()` / `public_card()` are the only shapes that reach a
  non-owner/non-staff viewer or the map JSON — never the reporter's identity or `contact_phone`
  (reporter + curator/admin only). `create_pet_report()` alerts staff once and pings the
  reporters of obvious counterpart reports. `possible_matches()` is a small **deterministic**
  suggestion (opposite type, same species, within `MATCH_RADIUS_KM` when both located, else same
  region) — read-only, bounded, **not** a second matching engine. Reporter may `resolve`/`close`
  their own report; `match`/`reopen` are staff-only (`STAFF_ONLY_ACTIONS`, gated in the view).
  Pages: `/myapp/pets/` (board), `pets/new`, `pets/mine`, `pets/<pk>/` (+ `/edit`, `/delete`,
  `/status`), all `@login_required`; state-changing routes are `@require_POST`. Pet markers ride
  the shared `map_data_view` (`kind: "pet"`); `PetReportAdmin` keeps `status` read-only so the
  transition rules aren't bypassed.
- **`rating.py`** — the public rating `/myapp/rating/` **with places** (owner 2026-10-14 — this
  *reverses* the 2026-10-03/09 rule "one list by name, no places, no points"). ONE national,
  **all-time** ranking (the Month/Year/All switch and the 4 summary cards were removed the same
  day — an old `?period=` is simply ignored): completed requests ↓ → `Profile.review_avg` ↓ (visible
  reviews, nulls last) → `review_count` ↓ → user id. Page header = breadcrumbs, "updated N min ago",
  the title — then the **podium** (`rating.podium(rows)`: places 1–3 with ≥1 completed request; fewer →
  as many as there are, none → no podium; #2 · #1 · #3 on desktop, a #1/#2/#3 column on phones;
  first page only) and the table from the next place ("Показано 4–23 из 81" = overall places). No
  search / region filter / sort (removed 2026-10-14) — old `?q=` / `?region=` / `?sort=` links are
  ignored like `?period=`. Ranked = active `is_volunteer` whose `VolunteerApplication`, if any, is not
  pending/rejected (admin/bootstrap volunteers without one count — unlike `network.py`'s strict
  counts). One annotated query (`build`), cached under `RATING_CACHE_KEY` with `computed_at`
  ("updated N min ago"). **Merit score (2026-10-18, replaces the lexicographic order above):**
  `ranked_queryset()` = ORM annotations, ORDER BY `score` ↓ (10 × completed + Bayesian review points
  (prior 4.0 × 3 reviews, ±10 per star, `floor(x+0.5)` — same on SQLite/PG) + 2 × badges via a
  subquery — **automatic badges only**: `Achievement.is_manual` (mentor) is shown, earns nothing), then `has_photo` ↓ (**tie-break only — a photo never adds points**), then profile
  `completeness` ↓ (`COMPLETENESS_FIELDS`), then id; all weights at the top of `rating.py`. Badges
  awarded by `achievements.evaluate` clear the cache too. Tests: `myapp/tests_demo_seed.py`
  (`RatingTieBreakTests`). `accounts.models.clear_rating_cache()` drops it +
  the About leaders — called by `Profile.add_points` (= task completion), review stats, application
  approve/reject. "You" + "Кабинет" and the staff-only "Профиль"
  (→ `crm_volunteer_detail`; there is no public volunteer page) are decided per viewer in
  `rating_view`, never cached. No "kindness points" — `Profile.rating` isn't shown. Plurals
  ("1 заявка / 3 заявки / 5 заявок") via `{% plural_i18n "<base>" n %}` (`myapp/templatetags/i18n_plural.py`:
  Russian form server-side, `data-i18n-plural` → i18n.js re-picks with `Intl.PluralRules`;
  every `<base>.one|few|many|other` key exists in all three languages). Tests: `myapp/tests_rating.py`.
- **`activity.py`** — the GitHub-style activity calendar (own profile + CRM volunteer card;
  **volunteers and clients only**, `shows_calendar`). Counted from existing rows, no model:
  volunteer accepted / completed (status completed) / photo report / donation `received_at` and
  `distributed_at` (two actions); client created request (cancelled too) / review (hidden too);
  both: donation offer, pet report. One `TruncDate(tzinfo=Asia/Dushanbe)` + `Count` query per kind
  (query count fixed per role — test-locked in `myapp/tests_activity.py`), **from `START_DATE`
  (1 Sep 2026) — at most 365 days — to today**, Monday-first grid as many weeks wide as that
  (`--act-weeks`), levels = quartiles of the user's own max, cached 10 min per user+day. **Plus the
  per-day counter `UserDailyActivity`** (`myapp/models/activity.py`, unique user+date, `is_demo`):
  `DailyActivityMiddleware` → `activity.record_request` writes the first page of the day (a cache
  flag makes later page views free) and +1 (`F()`) per successful POST (`ACTIVITY_IGNORED_URL_NAMES`
  skip pings / mark-read); `USER_ACTIVITY_TRACKING` off under `manage.py test` (tests opt in).
  A day's count = max(counter, the rows' kinds) — the surplus shows as kind `site`; a summary line
  shows active days + current streak (`act.days.*` / `act.streak.*`). The grid starts at
  max(START_DATE, the user's registration day) — no squares before registration.
  **`manage.py backfill_activity [--dry-run]`** (`services/activity_backfill.py`) restores rows from
  real timestamps only (`SOURCES`: one (timestamp, actor) pair per table), recounted not added
  (idempotent), no row for a day without events, `source` = backfill / live / merged / demo, never
  lowers a live row, never touches demo rows. Events done *for* someone are not theirs: the
  registration of an imported account (e-mail `@khayrkhoh.invalid` — the only import marker in the
  DB), an application created+approved in one go, a location stamped with the account.
  Russian is rendered server-side from the i18n.js table (`translation_table`); `static/js/activity.js`
  re-labels with `Intl.PluralRules` (`act.count.*` / `act.total.*` exist in every form in all three
  languages — Tajik uses the same text for each).
- **`analytics_v2.py`** — the CRM analytics page (`/myapp/crm/analytics/`, admin + curator;
  views in `myapp/views_analytics.py`). **`parse_filters(request.GET)` is the one place the
  filters are read** — every KPI, chart, CSV link and the heatmap gets the same `Filters`:
  `?period=7d|30d|3m|1y|custom` (default 30d; `from`/`to` only for custom) `&region=`; days
  are Asia/Dushanbe days, `to` includes its whole day; invalid input (bad token, from > to, a
  future date) → the last 30 days + `Filters.error` (the page shows `an.err.url`), never a 500;
  old `?days=` links still work. `Filters.previous()` = the same-length period right before.
  The panel's JS (`static/js/analytics-filters.js`) only enables/validates the fields and builds
  the URL. `UI_STRINGS` there mirrors the `an.*` keys of i18n.js (test-locked).
  Card headers: `headlines(f)` = each chart's period number (read from the same cached chart
  dicts) + % change vs `f.previous()` (`None` → "—" when the previous period is empty, never
  "+∞%"; `availability` is live → "current state"; `UP_IS_BAD` charts turn red when they grow).
  How a card draws is `DISPLAY` in `views_analytics.py` (line / bars / rank = horizontal sorted /
  stack / "share" = doughnut ≤ 5 slices else rank); box heights + the empty state are server-rendered.
  Layout (2026-10-16, `static/css/analytics.css`): KPI strip = 7 tiles in one row (`.analytics-kpis`, 4 + 3
  below 1100px, 2-up below 768px; the shared `.kpi-row` is untouched). Chart grid = 2 equal columns (1 below
  900px); every non-wide card (`.chart-card--half`) has the same structure — one-line title (ellipsis +
  translated `title`), a 2-line note slot (empty when the chart has no note), ONE 18rem `.chart-box` for every
  display, the table toggle at the bottom — so a row is one size by construction (`align-items: stretch`, no
  `grid-auto-rows: 1fr`: it would size every row like the heatmap). A ranking longer than fits scrolls inside
  the box (`--rows` × `--row-px` on `.chart-plot`); wide cards keep `--box-h`. Test-locked in `tests_charts.py`.
- **Chart look = `static/js/charts-theme.js` (`GCChartTheme`)** — the one place for the palette
  (primary `#2A3BA5`, accent `#F2A23A`, teal/rose/slate, stepped in lightness; lighter in dark),
  `ENTITY` (one meaning = one colour on every chart: completed teal, emergency rose, …), fonts,
  horizontal-only grid, HTML tooltip, legend, number/date formatting (month names from i18n.js
  `act.m*`/`act.mg*`) and the doughnut-total / bar-value plugins. Page scripts never set colours
  (test-locked in `myapp/tests_charts.py`); the dashboard trend legend dots and the heatmap ramp
  (= activity calendar scale) mirror it, also test-locked.
- **`telegram_link.py`** — `redeem_link_code`, the verified-round-trip consumer for Telegram
  account binding (see **Telegram account linking**).

`myapp/context_processors.py::maps_config` pushes `maps.tile_layer()` into every template as
`maps_tile_config`; `base.html` renders it with `{{ maps_tile_config|json_script:"gc-maps-tile" }}`
(XSS-safe) and `static/js/map.js` reads that element instead of hard-coding a tile URL. **Maps follow the site theme**:
`vector_style()["dark_url"]` (OpenFreeMap `dark` / MapTiler `streets-v2-dark`) and `tile_layer()["dark"]`
(CARTO `dark_all`) are the dark twins; map.js swaps the style live on a theme toggle
(`setStyle(..., {diff: false})` → `style.load` → every `onStyle()` layer — buildings, route, accuracy,
heat — comes back; DOM markers survive). Route/building colours are theme-aware too. A
misconfigured provider degrades to `{}` and map.js falls back to OSM — a bad `MAPS_PROVIDER`
never 500s an unrelated page.

`Profile` (on `accounts.models`) carries volunteer/client location (`latitude`/`longitude`,
`location_updated_at`), `availability_status` (available/busy/offline) — offline volunteers are
excluded entirely from matching, not merely scored low — and `skills` (a `JSONField` list of
`HELP_TYPE_CHOICES` keys, `Profile.has_skill(...)`), which feed the matching `skills` component
(match / empty-is-neutral / mismatch). `HelpRequest` also carries its own `latitude`/`longitude`
for the map and routing.

`OVERDUE_THRESHOLD` (3 hours) is defined once in `myapp/models/help_requests.py` and reused by
`HelpRequest.is_overdue`, `services/overdue.py`, and the analytics overdue count — don't
reintroduce a second magic number for it. `STALE_PENDING_THRESHOLD` (48 hours), in the same
module, is its pending-side counterpart (`HelpRequest.is_stale_pending`, `services/stale.py`,
the analytics stale count) — same rule, don't duplicate it. Automated monitoring is the
`check_overdue_tasks` (~15-min cron) and `check_stale_requests` (hourly cron) management commands
(README → **Background jobs**) — still plain cron, deliberately; Celery is used only for notification
*delivery* (see **Notifications**), not for scheduling.

### Demo stand (`seed_demo`, 2026-10-18)

`settings.DEMO_MODE` (`DJANGO_DEMO_MODE`) turns on the "Демонстрационный стенд" banner
(`base.html`, `demo.banner` in 3 languages) and is the first `seed_demo` guard; the others refuse
`khayrkhoh.tj` (or a subdomain) in `ALLOWED_HOSTS` and a database name in `PRODUCTION_DB_NAMES`
(`DJANGO_PRODUCTION_DB_NAMES`) — all checked before any query (`seed_demo.refusal_reason`).
`DJANGO_SQLITE_PATH` puts a stand in its own SQLite file. `is_demo` (default False, additive) on
`Users`, `HelpRequest`, `TaskReview`, `UserDailyActivity`; `task_actions.create_request` /
`feedback.submit_review` copy it from the demo client / request, so a demo account never makes a
non-demo row. The seed: a fixed, seeded plan (`SEED`, horizon `TIMELINE_END`) replayed up to now
**through the services** with `django.utils.timezone.now` patched to each step's time (create /
accept / work stages / complete / review); the one-active-request rule holds over the whole
timeline; demo requests have no photo report, so `photo_reports.credit_task` releases their reward.
`_quiet()` swaps `notify` / `publish.send_to_group` / e-mail / Telegram for no-ops during the run.
**It never writes photo reports or media files** (test-locked). Idempotent (users by username,
requests by (client, created_at), reviews per task, activity per user+date), one transaction;
`--reset` deletes `is_demo=True` rows only and refuses if a photo report or a real request /
review would be touched (it keeps demo rows a photo report points at). Existing accounts keep
login / password / e-mail / photo; a real account holding a demo username is skipped. Rows an
older seed wrote (its usernames with exactly its e-mail, its request texts) are adopted as demo.
`check_demo_data` also reports every `is_demo=True` row. Tests: `myapp/tests_demo_seed.py`.

New badges (migrations 0032 `Achievement.is_manual` + 0033 data; thresholds on the `Achievement` rows): `tasks_5`, `tasks_25`,
`high_rating` (≥ 5 visible reviews, average ≥ `TRUSTED_MIN_AVG`), `regular` (completed requests in
N local weeks in a row — weekly, never a daily streak; the code avoids the banned word "streak"),
`long_term` (N days since registration **and** ≥ 1 counted request — `NEEDS_A_TASK`).

### Notifications

`myapp/notifications.py` centralizes email (falls back to console backend when SMTP env vars are
unset) and Telegram (`send_telegram_message`, silently no-ops without a bot token). Views call
`notify_users(...)` explicitly at the point of the event (task accepted, broadcast sent, etc.).
`volunteer_queryset_for_region(region)` and `staff_recipients()` (active curators + admins) are
the shared recipient querysets — reuse them rather than re-deriving the role filter.
`myapp/signals.py` is **intentionally empty** — notification sends live in views, not signals, to
avoid duplicate sends on every model `.save()`. Don't move notification logic into signals.
**Delivery is background (Celery + Redis broker).** `notify_users` queues one
`myapp/tasks.py::send_email_task` per email address and one `send_telegram_task` per linked chat
(so recipients never share a `To:` line and a failure retries only that delivery — 5 retries,
exponential backoff + jitter; final failure → one `ERROR` log with task id only). Its return value
is now "deliveries **queued**" — still 0 when nobody is reachable, so the overdue / stale /
emergency zero-recipient `ERROR` safety nets are unchanged; the idempotency claims (conditional
UPDATEs, `notified_at`) happen before queuing, as before. `queue_email()` does the same for the
transactional mails (verification, password reset, welcome signal in `accounts/signals.py`).
Without `CELERY_BROKER_URL` (tests, dev) tasks run **eagerly inline**; if the broker is down at
queue time `_dispatch` delivers inline instead of losing it. The worker uses `--pool=threads`
(Celery's prefork pool crashed every task on Python 3.14). `EMAIL_TIMEOUT` (default 10s) still
bounds an inline send; every other outbound call (`geo.get_route`, `maps._nominatim`,
`ai_chat_view` → Groq, `send_telegram_message`) has a finite `requests` timeout and a graceful
fallback.

### Health checks & operational logging

- **`server/health.py`** — `GET /health/` (liveness, zero I/O, always 200) and
  `GET /health/ready/` (readiness: DB `SELECT 1` + **migrations** (all applied, none applied that the
  code lacks — `MigrationExecutor`) + **schema** (one `UNION ALL` of `SELECT cols FROM t WHERE 1 = 0`
  probes over every model table — SQLite probes use `[brackets]`: it reads an unknown `"quoted"`
  name as a string literal) + config + shared-cache round-trip → 200/503).
  Public, no secrets, no tracebacks, `Cache-Control: no-store`. External services are **not**
  readiness dependencies (the app degrades without them). A failing readiness poll logs one
  `WARNING` line and sets `response._has_been_logged` so Django's request logger doesn't repeat it.
- **Structured transition logs** — administrative state changes emit one `INFO` line with **IDs
  only** (never message bodies, tokens, or PII): emergency ack/resolve/cancel
  (`services/emergency.py`), donation approve/ready/received/distributed/cancel/assign
  (`services/donations.py`), pet
  moderation (`services/pets.py::apply_action`), volunteer-application approve/reject + task
  self-accept / direct-assign / completion (`myapp/views.py`), login / password-reset throttle
  hits (`accounts/views.py`). The overdue/stale/emergency **zero-recipient** failures already log
  `ERROR`. This log stream (→ stderr → journald) **is** the operational audit trail.
- **No `AuditLog` model.** The `reviewed_by` / `*_by` + `*_at` fields on `VolunteerApplication`,
  `EmergencyReport`, `Donation`, `PetReport` (and `volunteer` + `accepted_at` / `completed_at` on
  `HelpRequest`) already make every sensitive admin action reconstructible from the row itself;
  the structured logs cover the "who/when" trail for everything else. A generic audit table was
  evaluated and deliberately **not** added (Stage 8) — don't introduce one without a concrete
  requirement it can't meet.

### Sign in with Google (`accounts/google_oauth.py`)

OpenID Connect authorization-code flow with PKCE, hand-rolled on `requests` (no allauth — one
provider, and the custom `Users` model / role flags / `VolunteerApplication` sign-up would all need
adapters). Routes `/auth/google/` → Google → `/auth/google/callback/` (single-use `state` + `nonce`
in the session, per-IP throttle, ID token claims checked: iss/aud/exp/iat/nonce) →
login by `Users.google_sub`, else link an existing account by **Google-verified** email (never
re-links an email already tied to another `google_sub`), else `/auth/google/signup/` (role /
username / region; the identity waits in the session, nothing is written before the form).
Google-created accounts have an unusable password and `is_email_verified=True`. Lands like the
password login: the safe `?next=` (checked at `/auth/google/`, kept in the session flow; the button
carries it) else `dashboard`; a new non-client never lands on the request form. Its flash texts
are in `FLASH_KEYS` (`flash.google_*`). Migration `accounts/0014_users_google_sub`. Off unless
`GOOGLE_OAUTH_CLIENT_ID` + `_SECRET` are set (button hidden, routes 404); `manage.py test`
blanks both. Tests (`accounts/tests_google.py`) mock `accounts.google_oauth.requests.post`.
`/auth/` is in `ROBOTS_DISALLOW`.

### Telegram account linking

Binding a Telegram chat to an account is a verified round-trip, not a form field. The logged-in
user mints a one-time code in `accounts.views.telegram_link_view` (POST, 10-min TTL, only the
hash stored); they send `/link <code>` to the bot **from the chat they want bound**;
`myapp/services/telegram_link.py::redeem_link_code` validates and consumes it (single
conditional UPDATE, per-chat throttle) and never raises for an expected failure — the bot relays
`outcome.message` verbatim. Because of this, `telegram_id` is **not** a field on
`RegistrationForm` / `UserUpdateForm` / `update_profile` anymore — don't re-add it. The old
`/link <username>` command (which bound any account from just a guessable username) is gone.
`run_telegram_bot` also strips the `@BotName` suffix Telegram appends to group-chat commands.
Since Feature Pack v2 milestone 5 the bots are python-telegram-bot apps under `myapp/telegram/`:
`main_bot/core.py` (transport-agnostic, unit-tested without Telegram) + `main_bot/app.py` (thin
PTB adapter), the same for `community_bot/`; texts in `myapp/telegram/i18n.py`. Bot actions call
the same services as the website (`services/task_actions.py` holds accept / complete / assign /
create request / applications / broadcast, extracted from the views). `manage.py test` blanks the
bot tokens (`server/settings.py`), so a real token in a developer `.env` is never used by tests.

### AI assistant

A **role-based operational assistant**, not a chatbot. All logic lives in the
`myapp/services/ai/` package; `ai_chat_view` (URL name `ai_chat`, `@require_POST`, `@login_required`)
is a thin transport that parses `{message, history, lang}` (or `{confirm, history, lang}`), keeps
the per-user rate limit, builds the trusted context and calls the service. Distinct from
`services/matching.py` (the deterministic scorer) — do not conflate the two "AI"s.

- **`context.py`** — `build_user_context(request.user)` is the ONLY source of role/identity;
  a role claimed in a message changes nothing. Carries the real `Users` for scoped ORM,
  but only `prompt_dict()` (role, id, display name, region) reaches the model.
- **`policies.py::classify_message`** — deterministic multilingual guard that flags
  `medical` / `political` / `injection` / `off_topic` **before any Groq call**, so the boundary
  is cheap and test-locked (`myapp/tests_ai.py`). The system prompt is the second layer.
- **`prompts.py`** — English system prompt (standardized policy) that instructs the model to
  reply in the user's language; `detect_lang(text, hint)` (message script wins over the UI
  `lang` hint); localized RU/TJ/EN fallback + refusal strings.
- **`client.py`** — Groq HTTP client, never raises. `GROQ_ASSISTANT_MODEL`
  (default `qwen/qwen3.8-27b`, tool-capable) with a one-shot retry on `GROQ_MODEL`
  (default `openai/gpt-oss-20b`, also tool-capable — fixed 2026-09-30, the
  prior `qwen/qwen3.6-27b` default 404'd `model_not_found` for this account).
  Groq periodically retires model IDs, and availability varies by account (the
  original llama-3.x defaults started 404ing on 2026-09-11) — if the assistant
  degrades to the fallback message, hit `GET /openai/v1/models` with the
  configured key to see this account's actual current catalog before assuming
  the code is broken; don't guess a replacement model ID. Tests patch
  `myapp.services.ai.client.requests.post`.
- **`tools.py` / `read_tools.py` / `actions.py`** — the tool registry. Every tool is gated by
  the caller's role (`ToolSpec.roles`) **and** re-checks per-object visibility (`access.py`
  mirrors the view gates). Reads delegate to `analytics` / `overdue` / `stale` / `emergency` /
  `matching` / `maps`; results are count-first, paginated (≤50), and never contain
  passwords/tokens/phone/email. Roles are cumulative (client ⊂ —, volunteer ⊂ —, curator ⊃
  staff-reads, admin ⊃ curator + user directory).
- **Actions** (all need confirmation): `create_help_request` (client), `escalate_task_to_staff`
  (task party or staff), `notify_volunteer_about_task` (staff, task stays pending),
  `advance_work_stage` (assigned volunteer/staff). The LLM **never executes** — `dispatch`
  only validates and `confirmations.py` stashes a server-issued single-use token bound to the
  user (5-min cache TTL); the action runs only when the frontend POSTs `{confirm: <id>}`.
  `actions.advance_work_stage_for` / `notify_volunteer_recommendation` are shared with
  `task_advance_stage_view` / `task_notify_volunteer_view` — one copy of each rule.
- **`assistant.py`** — `run_conversation` (classifier gate → ≤4-round tool loop → confirmation)
  and `confirm_action`. Bounds history to 8 turns / 6000 chars. Any Groq or tool failure
  degrades to a localized string (200, never 5xx).

If no key is set the assistant returns the localized "temporarily unavailable" message.
The endpoint stays login-only; the ANONYMOUS policy exists in code but is not exposed.

### Config

`server/settings.py` loads everything from `.env` via `python-dotenv` — see `.env.example` for
the full list (Django core, SMTP + `EMAIL_TIMEOUT`, Groq, Telegram incl. `TELEGRAM_BOT_USERNAME`,
maps `MAPS_PROVIDER` / `MAPS_API_KEY` / `OSRM_BASE_URL` / `NOMINATIM_USER_AGENT`, prod
`DJANGO_CSRF_TRUSTED_ORIGINS` / `DJANGO_BEHIND_TLS_PROXY` / `DJANGO_DB_*` / `DJANGO_DB_CACHE` /
`DJANGO_LOG_LEVEL`). Most vars have safe fallbacks and no real credentials are needed for local
dev (`MAPS_PROVIDER=osm` is fully keyless), but `DJANGO_SECRET_KEY` is mandatory with no built-in
default (`.env.example` ships a placeholder, so a checkout without a `.env` won't boot). The
PostgreSQL branch (`DJANGO_DB_NAME` set) also enables `CONN_MAX_AGE=60` + `CONN_HEALTH_CHECKS`.
See **Security & abuse controls** for the other startup guards, and README → **Deployment** /
**Health checks** / **Backup & recovery**.

Several staff list views that grow with platform size are paginated (page-size constants near the
top of `myapp/views.py`: `ARCHIVE_PAGE_SIZE`, `PEOPLE_PAGE_SIZE`, `APPLICATIONS_PAGE_SIZE`,
`PHOTO_REPORTS_PAGE_SIZE`; `event_list`/`broadcast_list` use a plain recent-N slice). The
volunteer/curator dashboards, CRM lists, map JSON and matching candidate set were already bounded
in Stages 5–7.

### Production (khayrkhoh.tj)

Ubuntu 24.04 host, **no Docker** (see the rule at the top). Everything production-specific is in
`deploy/`:

- **One ASGI app for pages and WebSockets**: `khayrkhoh-web.service` = gunicorn with
  `server.asgi_worker.UvicornWorker` (a subclass of `uvicorn_worker.UvicornWorker` — the old
  `uvicorn.workers` module is deprecated — keeping `ws_max_size=16384`, `proxy_headers=False`, no
  access log, `lifespan="off"`) serving `server.asgi:application` on `127.0.0.1:8000`, 3 workers;
  `deploy/gunicorn.conf.py` forces `DJANGO_DB_CONN_MAX_AGE=0` for it (persistent DB connections are
  unsafe under ASGI). nginx (`deploy/nginx/khayrkhoh.conf`) proxies `/` and `/ws/` (Upgrade) there
  and serves `/static/`, `/media/` and the `internal` `/protected-media/` from disk. Channels fan-out
  across the workers goes through Redis DB 2; consumer state is per-connection, so several workers
  are safe.
- `khayrkhoh-celery.service` (`--pool=threads`), `khayrkhoh-telegram-bot.service`,
  `khayrkhoh-community-bot.service` (disabled until configured). Only one poller per bot token.
  The bot units have `RestartPreventExitStatus=78` = `polling.EXIT_INVALID_TOKEN` (a rejected
  token stops the unit as `failed` instead of restart-looping; `tests_deploy` keeps them equal).
- PostgreSQL 16 and Redis from apt, localhost only, tuned by `deploy/postgresql/` (sized for the
  3.8 GB host) and `deploy/redis/` (AOF, 128 MB, `volatile-lru`; DB 0 cache, 1 Celery, 2 Channels).
  Logs → journald (capped, `deploy/journald/`); `cron.log` and nginx rotate (`deploy/logrotate/`).
- `.env` is read twice — by systemd (`EnvironmentFile=`) and python-dotenv (`manage.py`, cron);
  `deploy/check_env.py` (run via `systemd-run`) proves both read the same values and that the
  production ones are right, printing names only.
- **Redeploy = `deploy/deploy.sh`** (never seeds anything; collectstatic before migrate; waits for
  `/health/ready/`; read-only `check_demo_data` at the end). Fresh server = `DEPLOY.md`. The
  one-time move of the live server off Docker, together with the Feature Pack v2 rollout, rollback
  plan and the mandatory dress rehearsal = `deploy/MIGRATION.md`.

Production prep (2026-09) also added `STATIC_ROOT` (`server/settings.py`, required for
`collectstatic`) and `create_initial_production_accounts` (see **Commands**) as the only
sanctioned way to get accounts onto a production DB — `seed_demo` must never run there. This
project has **no single `DATABASE_URL` variable** — PostgreSQL connection info stays as the
discrete `DJANGO_DB_NAME` / `_USER` / `_PASSWORD` / `_HOST` / `_PORT` vars; don't introduce one
without a concrete reason, it would duplicate the existing mechanism. The About Us hero is a **photo**
(the looping video was removed 2026-10-14): `static/images/about/about-hero-{768,1280,1672}.{webp,jpg}`
in a `<picture>` (WebP `srcset` + JPEG fallback, `sizes="100vw"`; 1672 px is the original's width —
**don't upscale**), `.hero-photo` = absolute + `object-fit: cover`, `object-position: right center` (70% ≤768px so the
hand-over stays in a phone crop). The photo stays bright (owner 2026-10-14): faint brand scrim
(`.hero::before`, opacity 0.15) + a light black gradient (`.hero::after`, ≤ 0.3 alpha) + a soft
`text-shadow` on the hero copy — don't go back to a heavy overlay. It is the
LCP element: eager, `fetchpriority="high"`, never `loading="lazy"` (`AboutHeroPhotoTests`). See `DEPLOY.md` for the full command
sequence and README → **Initial production accounts** /
**Demo data — NEVER in production** for the account-bootstrap rules.

### SEO (`server/seo.py`, `server/i18n_prerender.py`)

- **`PUBLIC_PAGES`** (`server/seo.py`) is the one list of indexable pages — `home`, `about`,
  `rating`, `register`, `login` — with each page's Russian `<title>` / meta description /
  canonical URL / sitemap entry. `seo_context` feeds `base.html`'s head: public pages get
  title/description/canonical/OG/Twitter (+ NGO/WebSite JSON-LD on home/about); **everything
  else is `noindex, nofollow`** automatically. Public templates must **not** override
  `{% block title %}` (the registry supplies it); private pages keep their own title block.
  `robots.txt` and `sitemap.xml` (only entries with a `sitemap` dict) are derived from the same
  registry: an `Allow` line per public page first, then `ROBOTS_DISALLOW` (private non-CRM paths)
  plus `private_myapp_prefixes()` — one `Disallow: /myapp/<segment>/` per **non-public** first
  segment in `myapp/urls.py`, so a new CRM route is blocked automatically. `/myapp/` is **not**
  blocked wholesale (about/rating live under it). `SitemapRobotsConsistencyTests` locks that every
  sitemap URL is robots-allowed (Google longest-match *and* first-match parsers), returns 200
  anonymously, has no noindex, and is its own canonical.
  Absolute URLs use `settings.SITE_URL` (`DJANGO_SITE_URL`, default `https://khayrkhoh.tj`),
  never the request Host. `CONTACT_PHONE` must match the footer phone in `base.html`.
- **`/` is the landing page**: `home_view` renders the About content for anonymous visitors
  and redirects signed-in users to `dashboard` (it used to be the login form, a duplicate of
  `/login/`). `/myapp/about/` renders the same content, so its canonical is
  `https://khayrkhoh.tj/` and it is **not** in the sitemap (no duplicate); it stays reachable
  (nav links) and explicitly `Allow`ed in robots.txt so Google can see that canonical.
  The logo + brand name (navbar and footer, one `brand_url` in `base.html`) lead to About: a signed-in
  user → `about` (never `home`, which would bounce them to the dashboard), a visitor → `home` (`/`, the
  canonical) — `myapp/tests_ux_flows.py::BrandLinkTests`.
- **Server-side i18n pre-render.** `ServerSideI18nMiddleware` fills every plain-text
  `data-i18n` element (and `data-i18n-ph` placeholder, with `data-i18n-args` interpolation)
  with the `SERVER_RENDER_LANGUAGE` (`ru`) string parsed from `static/js/i18n.js` itself — no
  second copy of the translations; crawlers get Russian HTML, `<html lang="ru">`, and i18n.js
  (now defaulting to `ru`) still switches EN/RU/TJ client-side. Elements with nested markup are
  left alone (i18n.js replaces `textContent` anyway). Brand assets (favicons, 1200×630
  `og-image.png`, `logo-512.png`) live in `static/images/brand/`.
- One canonical host: nginx 301s `www.khayrkhoh.tj` and plain HTTP to `https://khayrkhoh.tj`
  (`deploy/nginx/khayrkhoh.conf`).

### Templates/static

All templates extend `templates/base.html` (navbar, theme toggle, toast messages, language
switcher). **One font**: Inter via `--font-sans` (body, headings, numbers, form controls, maps, Chart.js,
Django admin via `templates/admin/base_site.html`); the only exception is the logo wordmark
(`--font-brand`, Cormorant Garamond). Playfair is gone — never add a `font-family` other than these vars.
**Palette (2026-10-09)**: soft tints; brand blue `#2A3BA5` / orange `#F2A23A` (`--brand-blue`,
`--brand-orange`) for accents and primary buttons only; dark theme = neutral dark greys (`#18191d`
ground), no navy/black; `--danger-line` for soft alert borders; all text pairs ≥ 4.5:1. **Long text**:
`body { overflow-wrap: anywhere }` (+ `td, th { overflow-wrap: break-word }` so table columns don't
shrink to letters), `min-width: 0` on common flex/grid children, `.truncate` + `title` for one-liners —
no page scrolls horizontally at 320 px with a 60-char name / 100-char email / 200-char word. No CSS framework — `static/css/style.css` is a hand-written design system (light/dark
via `[data-theme]` CSS variables). `crispy_forms` / `crispy_bootstrap5` and `django_filters` are in
`requirements.txt` and `INSTALLED_APPS` but **unused** (no `{% crispy %}`, no `FormHelper`, no
`django_filters` import anywhere) — forms render through the `templates/partials/_form.html` +
`_field.html` includes and list filtering is hand-rolled in views; don't reach for either.
`static/js/i18n.js` is a client-side EN/RU/TJ translation
table driven by `data-i18n` attributes — add new UI strings there, not as hardcoded template text,
if they need to support all three languages. Server-rendered form errors follow the same path:
`accounts/templatetags/form_i18n.py::error_i18n_attrs` maps a `ValidationError.code` (the
password-validator codes + `password_mismatch`) to an `err.*` i18n key plus `data-i18n-args`
(`{placeholder}` interpolation in `setLanguage`), used by `partials/_field.html` / `_form.html`
and `accounts/register.html` — so forms must re-raise `exc.error_list`, **not** `exc.messages`,
or the codes are lost. `static/js/map.js` drives the MapLibre GL map views
(`GCMap`; `createOpsMap()` is the interactive split-view operations controller).

A UI harmonization pass added reusable includes —
`templates/partials/_page_header.html`, `_form.html`, `_field.html` (the last renders any widget,
incl. `checkboxselectmultiple` for the skills field) — and CSS components `.data-table` (CRM
lists render as real tables on desktop, stacked labelled cards on mobile), `.tab-bar`,
`.quick-actions`, `.page-header`, `.status-screen`, `.checkbox-group`. Branded error pages live
at `templates/{404,403,500,403_csrf}.html`. `task_detail.html`, `login.html`, `register.html`
and `ai_assistant.html` were left alone (test-coupled DOM ids / out of scope) — check
`myapp/tests.py::TaskDetailCrmIntegrationTests` before touching `task_detail.html`.

**Function-first UX pass (2026-10-15, `myapp/tests_ux_flows.py`)** — conventions to keep:
- **Flash messages** (`messages.*`) render in `base.html` as typed toasts (icon + coloured edge +
  text, `role=alert` for error/warning else `status`, close button). Errors/warnings stay until
  closed; confirmations 10 s (paused on hover/focus); ≤560 px they sit in the page flow under the
  topbar (`.flash-stack`, never over the heading) and stay. They're translated through
  `myapp/templatetags/flash.py::FLASH_KEYS` (Russian text/regex → `flash.*` key, dynamic parts as
  `data-i18n-args`): **a new `messages.*("…")` literal in a view needs an entry there** —
  `FlashI18nTests` scans the views. Never claim a delivery that didn't happen: the request-created
  message depends on `create_request(...).volunteers_notified` (`notify()`'s reached count).
- **Relative times**: `{% time_ago dt %}` (`i18n_plural.py`) — never `|timesince` (Django renders
  it in English, `LANGUAGE_CODE='en-us'`; test-locked).
- **Irreversible actions ask first**: `<form data-confirm="i18n.key">` (handler in `base.html`) —
  completing a request, rejecting an application, deleting a pet notice.
- **GET `.filter-bar` forms collapse** behind a "Filters (n)" button below 600 px (base.html JS);
  desktop unchanged. A regional volunteer's task feed has no region filter (it could only empty it).
- **The client's own request** uses client wording: `_task_status_tag.html` with `for_client=True`
  ("Looking for a volunteer" …) + a "what happens next" note on their pending task; staff and
  volunteers keep the normal labels. Status values / colours are unchanged.
- **Sign-up path**: `/register/?role=client|volunteer` pre-selects; `?next=` is honoured after
  registration (same-site only, `accounts.views._safe_next`); the login page shows a "new here?"
  box with a client sign-up link when `next` is the request form.
- **Forms**: `_field.html` ties hints/errors to the widget (`described_widget`: aria-describedby +
  aria-invalid). Hints/errors are ≥ 14 px. `.reveal` hides content only when JS runs
  (`html.gc-reveal`, 3 s fail-safe) and never under `prefers-reduced-motion`.
- Staff `.data-table`s on phones: label beside value (floated into the cell padding).
- New TJ strings of this pass: `docs/TJ_REVIEW_UX_2026-10-15.md` (for a native speaker's check).

**Login / register scene (2026-10-16)**: `login.html` / `register.html` wrap their card in
`.auth-scene` — own warm palette as `--auth-*` tokens on `:root` in `static/css/auth-tokens.css` (light
`#FAF7F2` + accent `#D97A3E` with dark ink on the fill; dark `#0B1020` gradient + `#C9B8FF`), a 360/400px
frosted card, and `static/js/auth-bg.js` (canvas: leaves / shooting stars, follows `data-theme` live,
still under reduced motion, paused when hidden). Forgot/reset/google-signup keep the older auth.css
rules. The site theme now defaults to `prefers-color-scheme` until the visitor picks one (`gc-theme`).

**FAQ (`/faq/`, 2026-10-16)**: `myapp/models/faq.py` (`FAQCategory` / `FAQItem`) edited in the Django
admin (questions inline in the category, `order` / `is_active` editable in the lists). **Translated DB
text = one column per UI language** (`*_ru` required, `*_tj` / `*_en` optional → Russian fallback) — no
modeltranslation / gettext; the page renders all three and `faq.css` shows the one matching `html[lang]`,
so the i18n.js switch needs no reload. `services/faq.py`: only active categories with ≥ 1 active
question (2 queries), FAQPage JSON-LD (Russian, `seo._json_ld` escaping). Public page in `PUBLIC_PAGES`
(sitemap + robots Allow). `<details>` accordion + live search in `static/js/faq.js`. Tests:
`myapp/tests_faq.py`.

**Template comments:** `{# … #}` is single-line in Django — a multi-line one is printed on the
page as text (it leaked on About, 2026-10-14). Multi-line notes use `{% comment %}…{% endcomment %}`;
`TemplateCommentLeakTests` (myapp/tests_about_sections.py) scans every template for it.

Stage 9 polish conventions (in `style.css`):
- **`--nav-h`** (3.85rem) is the sticky-topbar row height; anything else that sticks offsets
  from it.
- **Equal-height cards**: `.grid > .card` (and `.opportunity-card` / `.pet-card`) are
  `display: flex; flex-direction: column`; a trailing `.button-row`/`.card__foot` gets
  `margin-top: auto`. CSS-grid already stretches a row's cards to the tallest. Panels whose
  content length genuinely differs (admin-panel feed columns) opt out with `.grid.grid-top`
  (`align-items: start`) — don't force those equal.
- **`<img width height>` needs `height: auto`** in its CSS class when the class sizes the image
  with `width: 100%` + `aspect-ratio` (`.volunteers-photo`, `.report-img`):
  the attributes otherwise act as a fixed pixel height and stretch the image (the 2026-09-30
  SEO regression). The landing `<h1>` starts with a `.visually-hidden` "KhayrKhoh — " (SEO) so the
  hero looks unchanged.
- **CSS/JS go through `{% asset 'css/x.css' %}`** (`myapp/templatetags/assets.py`), not
  `{% static %}`: without the manifest it appends `?v=<content hash>`; with
  `DJANGO_STATIC_MANIFEST=True` (production `.env`) it returns the
  `ManifestStaticFilesStorage` hashed name. Hashed files get a 1-year immutable cache in nginx.
  Consequence: **collectstatic must run before the app (re)starts** — `deploy/deploy.sh` runs it
  (before migrate) and only then restarts the units; a missing static file referenced by
  a template raises under the manifest, so add new static files to the repo, not just to prod.
- **Fonts (Google Fonts, `preconnect` + `display=swap`) and MapLibre GL (unpkg, SRI) stay on their
  CDNs on purpose** — measured 2026-09-30 they reach visitors 5–12× faster than this origin's
  uplink (~8–13 KB/s vs 46–156 KB/s); self-hosting them was tried and made first paint slower.
  Don't move them onto the origin (or add font preloads) unless the origin's bandwidth changes
  (e.g. a CDN in front). `myapp/tests_perf.py` allows exactly those CDN hosts; anything else —
  like the old expiring fbcdn photo hot-link — is self-hosted. Large photos are `<picture>` with
  WebP `srcset` + JPEG fallback resized to their display size (`picture { display: contents }`
  keeps the `<img>` laid out exactly as before).
- **Landing-page aggregates are cached** (`about_view`: `ABOUT_STATS_CACHE_KEY`, 5 min, DB cache
  in prod) — `/` and `/myapp/about/` run 1 query per hit. The About sections after the hero
  (2026-10-14): how-it-works (static), help types (one card per `HELP_TYPE_CHOICES` →
  `create_request?help_type=<key>`, visitors via `login?next=` — `login_view` follows only a
  same-host `next`), **leaders** (`views._about_leaders` = `rating.top(4)` — the rating's
  own all-time top with its places, see `rating.py` above; no second ranking) and **testimonials** (`feedback.landing_testimonials`: the one
  exception to "review comments are never public" — staff opt-in `TaskReview.show_on_landing`,
  default off, reset when the client edits the comment; 5★ + text + not hidden; client shown as
  "First L."). Both under `ABOUT_COMMUNITY_CACHE_KEY` (`accounts/models.py`), cleared by
  `clear_rating_cache()` / `set_landing`; sections with no rows aren't rendered.
  **Volunteer network** (`#network`, replaced "Why we are stronger" 2026-10-14,
  `services/network.py`): region list + map are public; the per-region **volunteer counts** reach the
  context only when `network.access_level(user) == GRANTED` — curator/admin, or an active
  `is_volunteer` whose `VolunteerApplication.status == approved` (**strictly** the application:
  volunteers created by `seed_demo` / `create_initial_production_accounts` / the admin without one
  don't count and don't see; clients never — anyone can register as one). Counts = one
  `values("region").annotate(Count)` query, shared cache `ABOUT_NETWORK_CACHE_KEY` (cleared by
  `approve()`/`reject()`), read only after the access check. The map loads **lazily**
  (`static/js/about-network.js`, IntersectionObserver) from `partials/_maplibre_lazy.html`; MapLibre's
  version / URLs / SRI live once in `myapp/templatetags/maplibre.py` (all three partials read them).
- **`.clamp-text`** (line-clamp, `--clamp` default 3) + `.card__more` (a "View full →" link,
  i18n key `common.view_full`) replace `|truncatechars` so a long description can't stretch a
  card.
- **Contrast tokens (milestone 6, axe-core WCAG AA clean):** text on a filled `--primary` /
  `--primary-2` / `--danger` surface (buttons, active tabs/pills, lang switch) uses
  `color: var(--on-primary)` — white in light, dark ink `#0b1220` in dark (the dark fills are
  light; white on them was 2.5:1). Never hard-code `white`/`#fff` there. `--muted` is `#5b6473`
  in light (≥ 4.8:1 on every tinted plate).
- **Theme toggle** (`.theme-toggle` in `base.html`): two inline SVG icons (no emoji — their
  glyph boxes sit off-centre per OS font), chosen by `[data-theme]` in CSS; `min-height: 0` +
  `padding: 0` + `aspect-ratio: 1` keep it a true 2.3rem circle (the global `button` rule's
  2.7rem min-height made it an oval).
- The navbar link row is one horizontal scroller (`.nav-links`, `overflow-x: auto`, edge-fade
  mask kept in sync by `base.html` JS) that never wraps; below 960px it collapses to the
  tap-to-open `.menu-button` dropdown.
