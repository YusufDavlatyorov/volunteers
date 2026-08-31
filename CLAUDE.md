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
python manage.py seed_demo           # demo data: admins/curators/volunteers/clients/tasks
python manage.py run_telegram_bot    # long-polling Telegram bot (separate process, not part of the request cycle)

python manage.py test                                    # full suite (221 tests, ~50s)
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

### Roles

`accounts.Users` (`AbstractBaseUser` + `PermissionsMixin`) has three boolean flags —
`is_curator` / `is_volunteer` / `is_client` — mutually exclusive, enforced in `Users.clean()`.
Admin is `is_superuser`, not a flag. The `.role` property resolves to one of
`admin`/`curator`/`volunteer`/`client`/`guest`.

Access control is two decorators, both used together where needed:
- `@login_required` (Django's)
- `@role_required(*roles)` — defined in `myapp/views.py`; superusers pass any check that includes
  `"admin"`. **It is not wrapped with `functools.wraps`**, so wrapped views lose `__name__`.

Registering as "volunteer" no longer grants the role directly — it creates a pending
`VolunteerApplication`, reviewed by an admin (`approve()`/`reject()` flip `is_volunteer` and
timestamp the review). See `myapp/models.py::VolunteerApplication` and the
`volunteer_application*` views/URLs.

State-changing actions (`accept_task`, `complete_task`, application approve/reject, `logout`,
`check_overdue`, Telegram unlink) are `@require_POST`; templates must submit them as forms, not
`<a href>` links.

### Security & abuse controls

A hardening pass added a cross-cutting layer that is easy to regress — keep it intact:

- **Tokens hashed at rest.** `accounts.models.hash_token` (SHA-256) — only the *hash* of a
  password-reset / email-verification / Telegram-link token is stored; the raw value lives only
  in the emailed URL or a one-time UI reveal. `generate_*_token()` returns the raw token and
  persists the hash; every lookup hashes the incoming token before querying.
- **Cache-based rate limiting.** Login (per-IP **and** per-username counters), password reset
  (per-IP), and Telegram-link redemption (per-chat) throttle via `django.core.cache`. No
  `CACHES` setting is configured, so this is the process-local `LocMemCache` — fine for the
  single-process Telegram bot and the dev server, but a real multi-process web deployment needs
  a shared cache backend for the web throttles to actually bite.
- **Upload size cap.** `accounts.models.validate_file_size` (`MAX_UPLOAD_SIZE_MB` = 5) guards
  every `ImageField` (avatars, photo reports). `update_profile` calls it explicitly because
  `Model.save()` skips field validators.
- **Coordinate validation.** `myapp.services.geo.is_valid_coordinate` enforces real WGS84
  ranges in `HelpRequestForm` / `ProfileForm`; a bare `DecimalField(max_digits=9)` would accept
  nonsense like latitude 800.
- **Startup guards.** `server/settings.py` raises `ImproperlyConfigured` if `DJANGO_SECRET_KEY`
  is unset (no built-in fallback anymore) or if `DJANGO_DEBUG=False` with a `*` in
  `ALLOWED_HOSTS`. `DEBUG` now defaults to **False**; secure-cookie / HSTS / SSL-redirect
  settings switch on automatically whenever `not DEBUG`.
- **`accept_task_view` concurrency.** A per-volunteer `cache.add()` lock plus a conditional
  `UPDATE ... WHERE status='pending'` (not fetch-then-save) prevent double-accept races. This is
  deliberately **not** a DB constraint — CRM/admin dispatch and matching's workload scoring
  legitimately model a volunteer holding several active tasks; "one active task" is a rule of
  the self-service accept flow only.
- **`_can_view_task`** (`myapp/views.py`) gates task-detail access: a volunteer sees only their
  own assigned tasks plus pending tasks that would show up in their region-filtered
  `task_list` — not every `status="pending"` row.

### Geo / CRM layer (`myapp/services/`)

Business logic that needs to be unit-testable without the ORM or network lives here, kept out of
`views.py` on purpose:

- **`geo.py`** — pure `(lat, lng)` math (`haversine_km`) and the OSRM routing client
  (`get_route`). `get_route` **never raises**: any OSRM failure (timeout, no route, unset
  `OSRM_BASE_URL`) returns a `{"success": False, ...}` dict with a haversine-distance fallback,
  so a broken routing provider degrades gracefully instead of 500ing a view.
- **`matching.py`** — `recommend_volunteers(task)`: deterministic, explainable 0–100 scoring
  (distance/availability/workload/region/freshness, urgent tasks weight distance higher) for
  admin/curator dispatch. Read-only — it ranks candidates but never assigns a task or notifies
  anyone; that stays an explicit admin/curator action. Distinct from the Groq-based conversational
  AI assistant in `views.py::ai_chat_view` — do not conflate the two "AI"s.
- **`analytics.py`** — aggregate CRM dashboard queries (`dashboard_stats`, `recent_activity`),
  built with annotated `Count`/`Q` aggregates rather than per-row Python loops, so query count
  stays constant regardless of data volume.

`Profile` (on `accounts.models`) carries volunteer/client location (`latitude`/`longitude`,
`location_updated_at`) and `availability_status` (available/busy/offline) — offline volunteers
are excluded entirely from matching, not merely scored low. `HelpRequest` also carries its own
`latitude`/`longitude` for the map and routing.

`OVERDUE_THRESHOLD` (3 hours) is defined once in `myapp/models.py` and reused by
`HelpRequest.is_overdue`, `check_overdue_view`, and the analytics overdue count — don't
reintroduce a second magic number for it.

### Notifications

`myapp/notifications.py` centralizes email (falls back to console backend when SMTP env vars are
unset) and Telegram (`send_telegram_message`, silently no-ops without a bot token). Views call
`notify_users(...)` explicitly at the point of the event (task accepted, broadcast sent, etc.).
`myapp/signals.py` is **intentionally empty** — notification sends live in views, not signals, to
avoid duplicate sends on every model `.save()`. Don't move notification logic into signals.

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

`ai_chat_view` calls Groq's OpenAI-compatible endpoint (`llama-3.1-8b-instant` by default). The
Groq key is read from `GROQ_API_KEY`, falling back to `GEMINI_API_KEY` for historical reasons
(`server/settings.py`) — the `.env.example` variable is literally named `GEMINI_API_KEY` for a
Groq key. If no key is set, the view returns a canned fallback tip instead of failing. System
prompt tone differs by role (gentler for clients, practical for volunteers).

### Config

`server/settings.py` loads everything from `.env` via `python-dotenv` — see `.env.example` for
the full list (Django core, SMTP, Groq, Telegram incl. `TELEGRAM_BOT_USERNAME`, OSRM/Nominatim
for maps). Most vars have safe fallbacks and no real credentials are needed for local dev, but
`DJANGO_SECRET_KEY` is now mandatory with no built-in default (`.env.example` ships a
placeholder, so a checkout without a `.env` won't boot). See **Security & abuse controls** for
the other startup guards.

### Templates/static

All templates extend `templates/base.html` (navbar, theme toggle, toast messages, language
switcher). No CSS framework — `static/css/style.css` is a hand-written design system (light/dark
via `[data-theme]` CSS variables). `static/js/i18n.js` is a client-side EN/RU/TJ translation
table driven by `data-i18n` attributes — add new UI strings there, not as hardcoded template text,
if they need to support all three languages. `static/js/map.js` drives the Leaflet map view.
