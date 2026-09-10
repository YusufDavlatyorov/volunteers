# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**Generation Connect** — a Django (5.2, function-based views, MVT) platform connecting elderly
clients with volunteers across 5 regions of Tajikistan (Dushanbe, Sogd, Khatlon, GBAO, RRP),
coordinated by curators/admins. Custom user model with 4 roles (admin/curator/volunteer/client),
Groq-backed AI assistant, Telegram notifications, and a geo-aware CRM (map, volunteer matching,
routing) added in a later phase.

`README.md` has setup/env-var details. `PROJECT_OVERVIEW.md` is a deep, auto-generated snapshot
of the codebase — useful for context but **dated 2026-06-13 and stale**: it predates the CRM
dashboard, volunteer-application workflow, geo/matching services, and the test suite, all of
which now exist. Verify anything from it against the actual code before relying on it.

## Commands

```bash
source .venv/bin/activate            # venv already present in repo (Python 3.14)
python manage.py runserver           # dev server → http://127.0.0.1:8000/
python manage.py migrate             # apply migrations
python manage.py seed_demo           # demo data: admins/curators/volunteers/clients/tasks (also: coords, availability, priority, one overdue task, one stale pending request, a needed-items catalogue + in-kind donation offers, a lost/found pet board)
python manage.py run_telegram_bot    # long-polling Telegram bot (separate process, not part of the request cycle)
python manage.py geocode_missing [--limit N --dry-run --profiles]   # backfill lat/lng for HelpRequests (and --profiles) via Nominatim; sleeps 1.1s/row for the usage policy
python manage.py check_overdue_tasks [--dry-run]        # alert curators/admins about tasks overdue past 3h; idempotent, runs on cron (see README "Background jobs")
python manage.py check_stale_requests [--dry-run]       # mirror of the above for the *pending* side: alert about requests waiting >48h without a volunteer; idempotent, hourly cron
curl -s localhost:8000/health/ ; curl -s localhost:8000/health/ready/   # liveness / readiness (server/health.py) — public, no secrets

python manage.py test                                    # full suite (528 tests, ~150s)
python manage.py test myapp.tests.MatchingAlgorithmTests  # one test class
python manage.py test myapp.tests.MatchingAlgorithmTests.test_closer_volunteer_ranks_higher  # one test
python manage.py test accounts                           # one app
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
`gc-pin--emergency` markers in the staff branch of `map_data_view`. See `services/emergency.py`.

### Security & abuse controls

A hardening pass added a cross-cutting layer that is easy to regress — keep it intact:

- **Tokens hashed at rest.** `accounts.models.hash_token` (SHA-256) — only the *hash* of a
  password-reset / email-verification / Telegram-link token is stored; the raw value lives only
  in the emailed URL or a one-time UI reveal. `generate_*_token()` returns the raw token and
  persists the hash; every lookup hashes the incoming token before querying.
- **Cache-based rate limiting.** Login (per-IP **and** per-username counters), password reset
  (per-IP), Telegram-link redemption (per-chat) and the AI assistant (`ai_chat_view`, per-user)
  throttle via `django.core.cache`. `CACHES` defaults to the process-local `LocMemCache` — fine
  for the single-process Telegram bot and the dev server. A multi-process (Gunicorn) web
  deployment **must** set `DJANGO_DB_CACHE=True` (+ `createcachetable`) or point `CACHES` at
  Redis/Memcached, or the web throttles only bite within one worker.
- **Upload size cap.** `accounts.models.validate_file_size` (`MAX_UPLOAD_SIZE_MB` = 5) guards
  every `ImageField` (avatars, photo reports). `update_profile` calls it explicitly because
  `Model.save()` skips field validators. `update_profile` also validates `region` against
  `REGION_CHOICES` and coerces `age` (it has no ModelForm).
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
  `tile_layer()` (Leaflet config), `route()` (delegates to `geo.get_route`), `geocode(query,
  region=)` → `(lat, lng)|None`, `reverse_geocode()`, `provider_name()`. Switching providers is
  one `.env` change (`MAPS_PROVIDER`: `osm` default/keyless, `mapbox` raster + `MAPS_API_KEY`,
  `google` raises — needs the JS SDK). Same discipline as `geo.py`: pure I/O, never raises for an
  expected failure. **New map/route/geocode code calls `maps.*`, not `geo.*` directly.**
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
  `overdue` / `stale` / `matching` / `pets` — no aggregation is duplicated. Rendered by `accounts/profile.html`
  (the single dashboard; `dashboard_view` at `/myapp/dashboard/` just `redirect`s there) via
  `templates/myapp/dashboard/_<role>.html` partials. `admin_panel_view` stays the deep CRM
  console; the curator/admin dashboard is the attention-triage summary that links into it.
  `_public_volunteer()` is the only volunteer data a client's dashboard may show (name + region,
  never contact details). The curator dashboard's stale/unassigned queues get a
  `task.assignment_suggestion` attached — `_attach_assignment_suggestions()` is exactly
  `matching.recommend_volunteers(task, limit=1)` per task, read-only, so a curator sees the top
  pick inline without opening the dispatch shortlist.
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
- **`telegram_link.py`** — `redeem_link_code`, the verified-round-trip consumer for Telegram
  account binding (see **Telegram account linking**).

`myapp/context_processors.py::maps_config` pushes `maps.tile_layer()` into every template as
`maps_tile_config`; `base.html` renders it with `{{ maps_tile_config|json_script:"gc-maps-tile" }}`
(XSS-safe) and `static/js/map.js` reads that element instead of hard-coding a tile URL. A
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
(README → **Background jobs**), **not** Celery — there is no task queue in this project.

### Notifications

`myapp/notifications.py` centralizes email (falls back to console backend when SMTP env vars are
unset) and Telegram (`send_telegram_message`, silently no-ops without a bot token). Views call
`notify_users(...)` explicitly at the point of the event (task accepted, broadcast sent, etc.).
`volunteer_queryset_for_region(region)` and `staff_recipients()` (active curators + admins) are
the shared recipient querysets — reuse them rather than re-deriving the role filter.
`myapp/signals.py` is **intentionally empty** — notification sends live in views, not signals, to
avoid duplicate sends on every model `.save()`. Don't move notification logic into signals.
Email is sent **synchronously inside the request** (no queue) — `EMAIL_TIMEOUT` (default 10s,
`server/settings.py`) is what stops a hung SMTP server from pinning a Gunicorn worker; every
other outbound call (`geo.get_route`, `maps._nominatim`, `ai_chat_view` → Groq,
`send_telegram_message`) already has a finite `requests` timeout and a graceful fallback.

### Health checks & operational logging

- **`server/health.py`** — `GET /health/` (liveness, zero I/O, always 200) and
  `GET /health/ready/` (readiness: DB `SELECT 1` + config + shared-cache round-trip → 200/503).
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
  (default `llama-3.3-70b-versatile`, tool-capable) with a one-shot retry on `GROQ_MODEL`.
  Tests patch `myapp.services.ai.client.requests.post`.
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

### Templates/static

All templates extend `templates/base.html` (navbar, theme toggle, toast messages, language
switcher). No CSS framework — `static/css/style.css` is a hand-written design system (light/dark
via `[data-theme]` CSS variables). `crispy_forms` / `crispy_bootstrap5` and `django_filters` are in
`requirements.txt` and `INSTALLED_APPS` but **unused** (no `{% crispy %}`, no `FormHelper`, no
`django_filters` import anywhere) — forms render through the `templates/partials/_form.html` +
`_field.html` includes and list filtering is hand-rolled in views; don't reach for either.
`static/js/i18n.js` is a client-side EN/RU/TJ translation
table driven by `data-i18n` attributes — add new UI strings there, not as hardcoded template text,
if they need to support all three languages. `static/js/map.js` drives the Leaflet map view
(`GCMap`; `createOpsMap()` is the interactive split-view operations controller).

A UI harmonization pass added reusable includes —
`templates/partials/_page_header.html`, `_form.html`, `_field.html` (the last renders any widget,
incl. `checkboxselectmultiple` for the skills field) — and CSS components `.data-table` (CRM
lists render as real tables on desktop, stacked labelled cards on mobile), `.tab-bar`,
`.quick-actions`, `.page-header`, `.status-screen`, `.checkbox-group`. Branded error pages live
at `templates/{404,403,500,403_csrf}.html`. `task_detail.html`, `login.html`, `register.html`
and `ai_assistant.html` were left alone (test-coupled DOM ids / out of scope) — check
`myapp/tests.py::TaskDetailCrmIntegrationTests` before touching `task_detail.html`.

Stage 9 polish conventions (in `style.css`):
- **`--nav-h`** (3.85rem) is the sticky-topbar row height; anything else that sticks offsets
  from it. `.dash-rail` (the right column of `accounts/profile.html`) is `position: sticky;
  top: calc(var(--nav-h) + var(--space-4))` and drops back to static below 820px (the
  `.two-col` collapse point) so it never covers content on a phone.
- **Equal-height cards**: `.grid > .card` (and `.opportunity-card` / `.pet-card`) are
  `display: flex; flex-direction: column`; a trailing `.button-row`/`.card__foot` gets
  `margin-top: auto`. CSS-grid already stretches a row's cards to the tallest. Panels whose
  content length genuinely differs (admin-panel feed columns) opt out with `.grid.grid-top`
  (`align-items: start`) — don't force those equal.
- **`.clamp-text`** (line-clamp, `--clamp` default 3) + `.card__more` (a "View full →" link,
  i18n key `common.view_full`) replace `|truncatechars` so a long description can't stretch a
  card.
- The navbar link row is one horizontal scroller (`.nav-links`, `overflow-x: auto`, edge-fade
  mask kept in sync by `base.html` JS) that never wraps; below 960px it collapses to the
  tap-to-open `.menu-button` dropdown.
