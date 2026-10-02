# Feature Pack v2 — Plan (milestone 0)

Branch `feature-pack-v2`, local only. Brief: `FEATURES_SPEC.md`. All `CLAUDE.md`
rules still apply. Status tracking: `FEATURES_PROGRESS.md`. This file is the design;
decisions marked **[OWNER]** need an answer before the milestone that uses them.

Baseline (2026-10-02): 657 tests green; production = `2467bc5` (web + telegram_bot +
celery_worker + redis, external shared_postgres). Server: 1 vCPU, 3.8 GB RAM, idle.
The bottleneck is the origin's network path (≈270 ms RTT, low throughput), not CPU.

---

## 1. Architecture decisions

### 1.1 ASGI / Channels topology (feature 12)
Recommended: **keep HTTP on the existing WSGI gunicorn (`web`) and add one small
`ws` service** that runs the same image with `server/asgi.py`
(`ProtocolTypeRouter`: `websocket` → Channels URLRouter, `http` → Django), uvicorn,
1 worker. nginx sends `/ws/` to `ws`, everything else to `web`.

- Why: "keep the current WSGI behavior working identically" — the HTTP path stays
  byte-for-byte the same (sync gunicorn workers, same timeouts, same middleware
  order). WS gets an event loop of its own; a WS bug can't take HTTP down.
- Same app/codebase/settings — `server/asgi.py` serves both protocols, so the `ws`
  container can also take HTTP later if we ever move fully to ASGI.
- Alternative (single server, `gunicorn -k uvicorn.workers.UvicornWorker`): one
  container fewer, but every sync Django view then runs through `sync_to_async`
  thread hops on a 1-vCPU box, and it changes the HTTP stack we've already tuned.
  **[OWNER]** confirm the separate `ws` service (my recommendation).

Channel layer: `channels_redis` on the existing Redis, DB 2 (cache = 0, Celery = 1).

### 1.2 Where code lives
Follows the existing layout (domain modules in `myapp/models/`, logic in
`myapp/services/`), with the new packages the spec names:

| Area | Location |
|---|---|
| WS consumers, routing, groups, auth helpers, rate limit | `myapp/realtime/` (`consumers.py`, `routing.py`, `groups.py`, `events.py`, `ratelimit.py`) |
| Realtime publish helper used by services | `myapp/realtime/publish.py` (`send_to_user`, `send_to_task`, `send_to_staff`) — safe no-op when no channel layer |
| Notification model + service | `myapp/models/notifications.py`; service `myapp/services/notify.py` (`notify(user, type, ...)`) — the existing `myapp/notifications.py` (`notify_users`, Celery delivery) becomes the email/Telegram transport underneath |
| Presence / availability | `myapp/services/presence.py` |
| Tracking | `myapp/models/tracking.py`, `myapp/services/tracking.py` |
| Routing / navigation | `myapp/services/routing.py` on top of `maps.route()` (geo.py import surface stays test-locked) |
| Chat | `myapp/models/chat.py`, `myapp/services/chat.py` |
| Feedback | `myapp/models/feedback.py`, `myapp/services/feedback.py` |
| Achievements | `myapp/models/achievements.py`, `myapp/services/achievements.py`, `static/images/badges/*.svg` |
| CRM analytics | `myapp/services/analytics_v2.py` (charts data; reuses `analytics.py` predicates) |
| Images | `myapp/services/images.py` (WebP variants) + Celery task |
| Telegram | `myapp/telegram/` package (`main_bot/`, `community_bot/`, shared `client.py`, `keyboards.py`, `i18n.py`, `state.py`); commands `run_telegram_bot` (kept name) and `run_community_bot` |

No new Django apps → no app-label churn; every new model gets an `__all__` entry in
`myapp/models/__init__.py` (orchestrator-owned file).

### 1.3 Library choices
| Need | Choice | Note |
|---|---|---|
| WebSockets | `channels`, `channels-redis`, `uvicorn[standard]` | pinned; Python 3.14 compatibility verified in milestone 1 before anything else |
| Telegram bots | `python-telegram-bot` v21+ (asyncio, inline keyboards, ConversationHandler) **or** keep the current raw-HTTP style | **[OWNER]** I recommend PTB: menus/wizards/callback idempotency are much less code; services stay sync and are called via `sync_to_async` |
| Charts | Chart.js 4 **self-hosted** (spec) + `leaflet.heat` self-hosted | only on staff CRM pages; hashed + 1-year cache. Note: the origin is slow (CLAUDE.md "fonts/Leaflet stay on CDN"), but these are staff-only, cached after first load — acceptable |
| Images | Pillow (already installed) via Celery | |
| Web Push | `pywebpush` + VAPID keys | optional — **[OWNER]** |

---

## 2. Feature designs

### 12. Realtime foundation
- `/ws/` → `RealtimeConsumer` (`AsyncJsonWebsocketConsumer`), `AuthMiddlewareStack`
  + `AllowedHostsOriginValidator`. Anonymous → close(4401).
- On connect: join `user_<id>`; staff (curator/admin) also `staff` and `map_staff`.
  `task_<id>` only via `subscribe` after the same check as the HTTP views
  (`_can_view_task`-equivalent moved to `myapp/services/access.py` so HTTP, WS and the
  bot share one rule).
- Protocol: section 3. Inbound rate limits: section 3.4.
- Client `static/js/realtime.js`: one shared connection per tab, exponential backoff
  (1 s → 30 s, jitter), heartbeat every 25 s, resubscribe after reconnect, event bus
  (`GCRealtime.on(type, fn)`), and **polling fallback** (`/myapp/realtime/poll/`
  returning the same events since a cursor) when WS fails 3× or is blocked.
- Publishing from sync code (views/services/Celery): `realtime.publish.*` uses
  `async_to_sync(channel_layer.group_send)`; wrapped so a Redis hiccup never breaks the
  request (log WARNING, continue — same philosophy as `_dispatch`).

### 11. Notifications
- Models: `Notification(recipient, type, title_key, body_key, params JSON, link,
  read_at, created_at)`, index `(recipient, read_at, -created_at)`;
  `NotificationPreference(user, type, channel, enabled)` unique together; defaults
  come from a code table (`NOTIFICATION_TYPES` with default channels per role), rows
  only store overrides. Optional `PushSubscription(user, endpoint, keys, created_at)`.
- Service `notify(recipients, type, params, link, *, dedupe_key=None)`:
  1. resolves recipients (users or querysets),
  2. bulk-creates `Notification` rows (in-app is always on),
  3. publishes `notification.new` to `user_<id>`,
  4. for email/Telegram channels per preference → existing `notify_users`-style
     Celery tasks (one email per recipient, retries, no shared `To:`),
  5. returns the count reachable on *any* channel → zero-recipient `ERROR` logs
     in overdue/stale/emergency keep working unchanged.
  `dedupe_key` (e.g. `sos:<report_id>`) + unique partial index prevents duplicates
  on retries.
- Text: i18n keys (`ntf.<type>.title/body`) rendered client-side from `i18n.js` with
  params (same mechanism as `data-i18n-args`); email/Telegram bodies rendered
  server-side in the recipient's language (`Profile.language`, new field, default `ru`).
- UI: navbar bell + unread badge, dropdown (latest 10), "mark all read", page
  `/myapp/notifications/` with filters, preferences page (type × channel), toasts for
  live events.
- Migration of existing sends: each `notify_users(...)` call site moves to
  `notify(...)` with a type; guarantees preserved (claims before notify, ID-only logs).
- Pruning: `prune_notifications` management command (cron daily) deletes > 90 days.

### 3. Automatic availability
- `Profile.last_seen_at` (new, indexed). Seen = WS connect/heartbeat, authenticated
  page view (throttled to one DB write per user per 60 s via cache), Telegram bot
  interaction.
- `presence.recompute(user)`: `busy` if any active task as volunteer; else
  `available` if approved volunteer and `last_seen_at` within `AVAILABILITY_IDLE_MINUTES`
  (default 30, setting); else `offline`. Called on accept/assign/complete/cancel,
  WS connect/disconnect, heartbeat, bot activity; plus `sweep_availability` command
  (cron every 5 min) for the "went quiet" transition.
- Remove the selector from `ProfileForm`/`edit_profile.html`; staff keep seeing the
  status everywhere they see it today. Field stays → matching unchanged
  (offline still excluded).
- **[OWNER]** "Do not disturb / on vacation" switch (would only exclude from matching)
  — add it or not? Not built unless you say yes.

### 7. Live tracking (only during an active task)
- `TrackingConsent` = `Profile.tracking_consent_at` (first-time modal explaining
  who sees what, when it stops; declining keeps tasks working without live location).
- Session: starts when the volunteer's task is `active` and consent exists; the
  task page/dashboard runs `watchPosition` → WS `location.update` every 10–15 s or
  > 25 m moved. Telegram live-location updates feed the same service.
- `tracking.accept_point(volunteer, task, lat, lng, accuracy, ts)`: active-task +
  assigned-volunteer check, `is_valid_coordinate`, drops points with accuracy > 200 m
  or implied speed > 150 km/h, writes latest to `HelpRequest.volunteer_last_lat/lng/at`
  and a row in `TaskLocationPoint` (trail), publishes `location.updated` to
  `task_<id>` (client + staff only) and a marker to `map_staff`.
- Stop: complete/cancel → `tracking.end(task)` deletes the trail and clears latest;
  volunteer "stop sharing" button → `tracking.stopped` + staff notification.
  `purge_tracking` command (cron hourly) deletes any trail older than 24 h.
- Indicator on volunteer UI: "Ваше местоположение видят клиент и куратор" + stop.
  Privacy text added to About page (retention: trail deleted at task end, ≤ 24 h).

### 2. Route + navigation
- `routing.route(origin, dest, profile="driving"|"walking", steps=True)` → `maps.route()`
  extended with `profile` and `steps` (OSRM `steps=true&overview=full`); never raises,
  haversine fallback unchanged. Cache in Redis per
  `(round(from, 3 dp ≈ 100 m), to, profile)` for 2 min.
- Task detail (assigned volunteer, client, staff) + ops map: route polyline,
  distance, ETA, driving/walking toggle.
- In-app navigation: step list with localized maneuver text (`nav.*` keys,
  EN/RU/TJ, built from OSRM `maneuver.type/modifier` + street name), current step
  highlighted from live positions (feature 7); re-route when > 50 m off the polyline
  (client-side distance check, server re-fetch, debounced 15 s).
- Deep links: Google Maps (`https://www.google.com/maps/dir/?api=1&destination=lat,lng`),
  Yandex Navigator (`yandexnavi://build_route_on_map?lat_to=…&lon_to=…`) with Yandex
  Maps web fallback, 2GIS (`dgis://2gis.ru/routeSearch/rsType/car/to/lng,lat`) with web
  fallback.
- Self-hosted OSRM (optional): `osrm` compose profile, Geofabrik
  `tajikistan-latest.osm.pbf`, `scripts/osrm_build.sh` (extract/partition/customize,
  MLD) for car + foot; `OSRM_BASE_URL` / `OSRM_FOOT_BASE_URL`. **[OWNER]** see memory
  budget — preprocessing peaks well above runtime RAM; I propose building the data
  locally (or on a bigger box) and copying it to the server.

### 8. Chat
- Models: `ChatMessage(task, sender, kind text|image|system, body ≤ 1000 chars, image,
  image_variants, is_staff_post, hidden_at, created_at)`, `ChatReadState(task, user,
  last_read_id)`, `ChatReport(message, reporter, reason, created_at, handled_by/at)`.
- Access (`access.can_chat`): client + assigned volunteer write while `active`; staff
  read always, post as "Куратор"; read-only after completion; archived (hidden from
  lists, still in DB for staff) after `CHAT_ARCHIVE_DAYS` (default 30).
- WS: `chat.send` / `chat.typing` / `chat.read`; HTTP fallback POST for send + paginated
  history GET (cursor by id). Images via HTTP upload (≤ 5 MB, validate_file_size) →
  Celery WebP thumb → `chat.message` event.
- Quick replies ("Я в пути", "Я на месте", "Позвоните мне") localized; "large text"
  toggle (stored per user, localStorage). Links rendered as plain text for clients.
  Rate limit 20 msgs/min/user/task. Report → staff notification.
- Telegram relay (feature 4) per user preference.

### 9. Feedback
- `TaskReview(task 1:1, client, volunteer, stars 1–5, comment ≤ 1000, tags JSON from a
  fixed list, created_at, updated_at, hidden_at, hidden_by)`; editable by the client
  for 24 h after creation; only on `completed` tasks they own.
- Volunteer profile: average + count (aggregate, cached); comments visible to the
  volunteer and staff only; public cards/rating show only the average.
- Staff hide/unhide (`@require_POST`, ID-only INFO log). Stars ≤ 2 → `notify` staff.
- Matching signal: computed and exposed (`Profile.review_avg`, `review_count`
  denormalized) but **not** added to matching weights. **[OWNER]** approve a weight
  later if wanted.

### 6. Achievements
- `Achievement(code unique, tier, icon, sort_order, criteria JSON, is_active)` — titles
  /descriptions are i18n keys `ach.<code>.title/desc`; `UserAchievement(user,
  achievement, awarded_at, progress_snapshot)` unique together.
- `achievements.evaluate(user, event)` with one small evaluator per code (task count,
  streaks, help-type coverage, region top-of-month, donations delivered, pets matched,
  SOS handled, points ≥ 500, mentor = community bot activity count); idempotent via the
  unique constraint. Called from services on events (not signals); `backfill_achievements`
  command; monthly "region hero" via a cron command.
- Badges: original SVGs in the site palette (`static/images/badges/<code>.svg`), CSS
  shine animation on hover (respects `prefers-reduced-motion`), locked = greyscale +
  progress "17/25". Profile grid → click → accessible modal (`<dialog>`). Rating/public
  cards show top 3. New badge → `notify(type="achievement")` + Telegram + community
  shout-out (feature 5).

### 10. CRM analytics
- New tab `/myapp/crm/analytics/` (admin + curator) + compact charts on the staff
  dashboard. Filters: 7/30/90 days, custom range, region.
- `analytics_v2.series(kind, start, end, region)` — one aggregated query per chart
  (`TruncDate` + `Count`/`Avg` with `filter=Q(...)`), constant query count; cached 5 min
  in Redis per (kind, range, region). Charts listed in the spec, incl. funnel, time to
  accept/complete, overdue/stale trends, availability split (live via WS), emergencies
  response time, donations, feedback, registrations by role.
- Heatmap: `leaflet.heat` layer fed by `/myapp/crm/analytics/heat.json` (rounded coords,
  capped points, no PII).
- CSV export per chart (`?format=csv`, same query, staff only).

### 4. Telegram main bot
- Rewrite `run_telegram_bot` into `myapp/telegram/main_bot/` with the same entry
  command. Linked accounts only (`/link` flow unchanged); language = `Profile.language`.
- Role menus exactly as the spec (volunteer / client / staff), all actions call the
  **same services** as the website (accept → same conditional UPDATE; assign → matching
  top 3 then the shared assign service; SOS → `emergency.report_emergency`; etc.).
- Client "create request" wizard: state in Redis (TTL 30 min) — type → description →
  address or shared location → phone (validated with `normalize_tj_phone`) → priority
  → confirm. Uses `HelpRequestForm` for validation.
- Callback idempotency: callback data carries a short nonce; processed nonces kept in
  Redis 10 min; every mutating action goes through the existing conditional-UPDATE
  services anyway.
- Per-chat rate limit (Redis), ID-only logs. Long polling kept (documented: no public
  webhook endpoint to secure, works behind the slow uplink, one process).

### 5. Community bot
- Separate process `run_community_bot` (`TELEGRAM_COMMUNITY_BOT_TOKEN/_USERNAME`,
  `TELEGRAM_COMMUNITY_CHAT_IDS`). Models: `CommunityInvite(user, chat_id,
  invite_link_hash, expires_at, used_at)`, `CommunityMember(user, chat_id, joined_at,
  left_at)`, `ModerationAction(chat_id, target_tg_id, actor, action, reason, created_at)`.
- Invite: approved volunteer + linked Telegram → `createChatInviteLink`
  (member_limit = 1, expire_date = +24 h), link shown once on the site or sent by the
  main bot. `chat_member` updates bind the joiner to the invite; unknown joiners are
  removed.
- Revocation: volunteer rejected/deactivated → `banChatMember` + `unbanChatMember`
  (kick without permanent ban) via Celery task.
- Moderation: flood limit, link filter for non-staff, `/report`, staff `/warn /mute /ban`,
  rules on join, `/help`. Mirrors website broadcasts/events as pinned posts; weekly
  top volunteers + new achievements. Never posts client data.
- `docs/COMMUNITY_BOT.md` with owner steps.

### 1. Speed (photos, video, CDN-ready)
- `images.make_variants(field_file)` → `thumb` 320 px + `medium` 960 px WebP via Celery
  on upload (avatars, photo reports, pet photos, chat images); variants stored next to
  the original, paths in a `image_variants` JSON field on each model;
  `{% responsive_image obj.image obj.image_variants sizes="…" %}` tag renders
  `<picture>`/`srcset` + `loading="lazy"` + width/height **without layout change**
  (same CSS boxes, verified by screenshot diff). `backfill_image_variants` command.
- nginx: `/media/` 7–30 d cache, `open_file_cache`, already-hashed `/static/` stays
  1 year immutable; no cookies set on static/media.
- Cloudflare-ready: `DJANGO_BEHIND_CLOUDFLARE` → real client IP from
  `CF-Connecting-IP` (only when the request came from a Cloudflare range — documented),
  used by every rate limiter (one `client_ip()` helper replaces the local ones).
  `docs/CLOUDFLARE.md` with NS-switch steps (owner does DNS).
- Report before/after page weight and image sizes.

---

## 3. WebSocket protocol (summary — full text in `docs/WS_PROTOCOL.md`, milestone 1)

### 3.1 Envelope
`{"type": "<domain.event>", "payload": {...}, "ts": "<ISO8601 UTC>", "id": "<optional client id>"}`

### 3.2 Client → server
| type | payload | who |
|---|---|---|
| `ping` | `{}` | any |
| `presence.heartbeat` | `{visible: bool}` | any |
| `subscribe` / `unsubscribe` | `{topic: "task", id}` | task parties + staff |
| `location.update` | `{task_id, lat, lng, accuracy, speed?, heading?}` | assigned volunteer, active task, consent given |
| `tracking.stop` | `{task_id}` | assigned volunteer |
| `chat.send` | `{task_id, text, client_msg_id}` | client / assigned volunteer (active), staff |
| `chat.typing` | `{task_id}` | chat parties |
| `chat.read` | `{task_id, last_id}` | chat parties + staff |
| `notification.read` | `{ids: [...]}` or `{all: true}` | recipient |

### 3.3 Server → client
`hello {user_id, role}`, `pong`, `error {code, ref}`, `notification.new {id, type,
title_key, body_key, params, link, created_at}`, `notification.read {ids|all}`,
`task.updated {task_id, status, work_stage}`, `location.updated {task_id, lat, lng,
ts, eta_min?}`, `tracking.stopped {task_id, reason}`, `chat.message {task_id, id,
sender_display, is_staff_post, kind, body, thumb_url?, created_at}`, `chat.typing
{task_id, sender_display}`, `chat.read {task_id, user_display, last_id}`,
`crm.counters {...}`, `map.marker {kind, id, lat, lng, status}`,
`availability.changed {user_id, status}` (staff only).
**No payload ever contains phone or email.** Display names only (`_public_volunteer`
style).

### 3.4 Rate limits (Redis, per user)
`location.update` 1 per 5 s; `chat.send` 20/min/task; `chat.typing` 1 per 2 s; all
other inbound 60/min; message size ≤ 4 KB → close(4408) on abuse.

---

## 4. Permission matrix (new endpoints and WS events)

| Endpoint / event | Admin | Curator | Volunteer | Client | Anonymous |
|---|---|---|---|---|---|
| `/ws/` connect | ✓ | ✓ | ✓ | ✓ | ✗ (4401) |
| subscribe `task_<id>` | ✓ | ✓ | assigned / visible pending | own task | ✗ |
| groups `staff`, `map_staff` | ✓ | ✓ | ✗ | ✗ | ✗ |
| notifications list/bell/read/preferences | own | own | own | own | ✗ |
| `location.update` / stop sharing | ✗ | ✗ | assigned volunteer, active task | ✗ | ✗ |
| see live location / trail | ✓ | ✓ | own | own active task | ✗ |
| route + navigation (task) | ✓ | ✓ | assigned | own task | ✗ |
| chat read | ✓ | ✓ | assigned | own task | ✗ |
| chat write | ✓ (as Куратор) | ✓ (as Куратор) | assigned, active | own, active | ✗ |
| chat report message | ✓ | ✓ | party | party | ✗ |
| leave/edit review (24 h) | ✗ | ✗ | ✗ | own completed task | ✗ |
| see review comments | ✓ | ✓ | about self | own | ✗ (average only on public cards) |
| hide review | ✓ | ✓ | ✗ | ✗ | ✗ |
| achievements (own profile) | ✓ | ✓ | ✓ | ✓ | public cards: top badges |
| CRM analytics + CSV + heatmap | ✓ | ✓ | ✗ | ✗ | ✗ |
| community invite link | ✗ | ✗ | approved volunteer + linked TG | ✗ | ✗ |
| bot staff menus (SOS, assign, broadcast) | ✓ | ✓ (no applications) | ✗ | ✗ | ✗ |
| bot approve applications | ✓ | ✗ | ✗ | ✗ | ✗ |

All mutating HTTP endpoints: `@require_POST` + `@login_required` + role/object checks
in the shared `access.py`; WS handlers call the same functions.

---

## 5. Models & migrations (all additive; no data loss)

| Model / field | Milestone | Notes |
|---|---|---|
| `Profile.last_seen_at`, `Profile.language`, `Profile.tracking_consent_at` | 1 / 2 | nullable; `language` default `ru` |
| `Notification`, `NotificationPreference`, (`PushSubscription`) | 1 | index (recipient, read_at, -created_at); partial unique on dedupe_key |
| `HelpRequest.volunteer_last_lat/lng/at`, `TaskLocationPoint` | 2 | trail deleted at task end / 24 h |
| `ChatMessage`, `ChatReadState`, `ChatReport` | 3 | |
| `TaskReview`, `Profile.review_avg/review_count` | 3 | |
| `Achievement` (+ data migration with the starter set), `UserAchievement` | 4 | |
| `image_variants` JSON on `Profile`, `PhotoReport`, `PetReport`, `ChatMessage` | 6 (chat in 3) | |
| `CommunityInvite`, `CommunityMember`, `ModerationAction` | 5 | |

No `DATABASE_URL`, no new Postgres; `seed_demo` extended (demo reviews, chat, badges)
for local use only.

---

## 6. Memory budget (1 vCPU, 3.8 GB; measured today: web 205 MB, telegram_bot
61 MB, celery_worker 76 MB, redis 3 MB, shared_postgres 39 MB ≈ 0.4 GB)

| New / changed container | Estimate | Limit to set |
|---|---|---|
| `ws` (uvicorn, 1 worker, Channels) | 90–130 MB | 256 MB |
| `telegram_bot` (PTB rewrite) | +20–40 MB → ~100 MB | 300 MB (unchanged) |
| `community_bot` (PTB) | 70–100 MB | 200 MB |
| `celery_worker` (+ image variants, Pillow) | peaks +60–120 MB during resizes | 384 MB (from 300) |
| `redis` (+ channel layer, route cache) | +10–30 MB | 192 MB (unchanged) |
| **Subtotal without OSRM** | **≈ 0.7–0.8 GB total** | |
| optional `osrm_car` + `osrm_foot` (MLD, Tajikistan) | runtime ≈ 150–400 MB each (to measure) | 512 MB each |
| OSRM **preprocessing** (extract/partition) | peak 1.5–3 GB | done off-server or with swap |

Every new container's real RSS is measured with `docker stats` in the milestone
report. If the total ever exceeds ~2.5 GB we drop the optional OSRM foot profile first.

---

## 7. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Channels/uvicorn/PTB on **Python 3.14** (prefork already broke Celery) | first task of milestones 1 and 5: install + smoke test before writing features; pin versions |
| Slow origin uplink (CLAUDE.md) | WS payloads tiny; self-hosted Chart.js/heat only on staff pages, hashed + 1 y cache; images get WebP variants |
| WS on 1 vCPU | single uvicorn worker, low connection counts expected; load sanity test in milestone 6 |
| Privacy (location, chat, phones) | one `access.py` for HTTP/WS/bot; payload builders that whitelist fields; tests assert no phone/email in any WS payload or public shape |
| Duplicate notifications after migrating call sites | `dedupe_key` unique index; existing claims kept before `notify` |
| Shared-file conflicts between agents | orchestrator owns `models/__init__.py`, `urls.py`, `settings.py`, `base.html`, `style.css`, `i18n.js`, `docker-compose.yml`, `requirements.txt`; agents hand over snippets |
| **No cancel flow exists** (only Django admin sets `cancelled`) — spec relies on cancel events | **[OWNER]** add a "cancel request" action: client (while pending) and staff (any open state)? Needed for tracking stop / chat close semantics |
| Existing test suite (657) | full suite after every merge into the branch; no milestone closes red |
| Second bot / Cloudflare need owner actions | documented in `docs/COMMUNITY_BOT.md`, `docs/CLOUDFLARE.md`; nothing switched by us |

---

## 8. Milestones & agent assignment

| # | Scope | Agents (parallel where independent) |
|---|---|---|
| 1 | Realtime foundation (12) + notifications (11) + availability (3) | realtime-engineer (ASGI, consumers, notify service) ∥ geo-engineer (presence service) → frontend-qa (bell, toasts, realtime.js UI, prefs page) |
| 2 | Tracking (7) + routes/navigation (2) + optional OSRM | geo-engineer (tracking, routing, map.js, OSRM) + realtime-engineer (WS location events) → frontend-qa |
| 3 | Chat (8) + feedback (9) | realtime-engineer (chat) ∥ engagement-engineer (feedback) → frontend-qa |
| 4 | Achievements (6) + CRM charts (10) | engagement-engineer → frontend-qa (badges SVG/modal, chart pages) |
| 5 | Telegram main bot (4) + community bot (5) | telegram-engineer |
| 6 | Speed (1) + full QA + docs | frontend-qa (+ small fixes from owners of each area) |
| 7 | Deploy — only after "deploy" | orchestrator |

Each milestone ends with: full suite green, new tests, memory numbers, Russian report.

---

## 9. Owner decisions (answered 2026-10-02)

1. **Separate `ws` service** (uvicorn, 1 worker); regular pages stay on gunicorn. ✅
2. **python-telegram-bot** for both bots. ✅
3. **Do not disturb / on vacation** — yes: excludes the volunteer from matching and from
   new-task notifications only; shown clearly in their profile and to staff; optional end
   date after which it switches off automatically (`Profile.is_away` + `away_until`,
   predicate `Profile.is_away_now` / `presence.not_away_q()`).
4. **Request cancellation** — yes: client while `pending` (reason optional), staff any open
   state (reason required). Notify the other parties, stop tracking, close chat, ID-only
   log, tests. Built in milestone 1 (`myapp/services/cancellation.py`); tracking/chat hooks
   are filled in by milestones 2/3.
5. **No Web Push** for now — in-app + email + Telegram. The channel registry stays
   extensible so a `webpush` channel can be added later without touching call sites.
6. **Self-hosted OSRM** — milestone 2: build the Tajikistan data (car + foot) locally on the
   owner's Mac, copy the prepared files to the server, which only runs `osrm-routed`.
   Measure RAM. Haversine fallback stays.
7. Reviews are **display-only** (no matching weight). ✅
8. Defaults accepted: offline after 30 min, chat archive 30 days, review edit 24 h,
   trail ≤ 24 h, notifications pruned after 90 days.
- Chart.js self-hosted on staff pages only. ✅

---

## 10. Milestone 1 contracts (shared APIs — agents code against these)

Already in place (orchestrator): `channels`, `channels-redis`, `uvicorn[standard]` pinned
and smoke-tested on Python 3.14; `INSTALLED_APPS += channels`; `ASGI_APPLICATION`;
`CHANNEL_LAYERS` (Redis DB 2 via `DJANGO_CHANNEL_REDIS_URL`, else in-memory);
`AVAILABILITY_IDLE_MINUTES`, `NOTIFICATION_RETENTION_DAYS` settings; compose `ws` service;
migrations `accounts 0008` (`Profile.last_seen_at`, `is_away`, `away_until`, `language`)
and `myapp 0016` (`HelpRequest.cancelled_at/_by`, `cancel_reason`).

**`myapp/realtime/publish.py`** (sync, never raises, returns bool):
`send_to_user(user|id, type, payload)`, `send_to_task(task|id, …)`, `send_to_staff(…)`,
`send_to_map_staff(…)`, `send_to_group(group, …)`; groups `user_<id>`, `task_<id>`,
`staff`, `map_staff`; channel message `{"type": "realtime.event", "event": <envelope>}`.
Not for use inside the consumer's event loop (use `await channel_layer.group_send`).

**`myapp/services/presence.py`** (geo-engineer):
- `not_away_q(prefix="profile__")` — exists.
- `compute_status(*, is_volunteer, has_active_task, last_seen_at, now)` → `busy|available|offline` (pure).
- `recompute(user) -> str|None` — saves only on change; on change publishes
  `availability.changed {user_id, status}` to `staff`. No-op (None) for non-volunteers.
- `mark_seen(user, *, source)` — `source` ∈ `web|ws|telegram`; writes `last_seen_at`
  at most once per 60 s per user (cache), then `recompute` if the status could change.
- `on_task_change(task)` — recompute the task's volunteer (accept/assign/complete/cancel).
- `set_away(user, away, until=None)`, `sweep() -> int` (+ `sweep_availability` command:
  idle → offline, expired `away_until` → off).

**`myapp/services/notify.py`** (realtime-engineer):
`notify(recipients, type, *, params=None, link="", dedupe_key=None, subject=None,
message=None) -> int` — recipients = users / queryset / iterable; in-app row + WS
`notification.new` always; email/Telegram per `NotificationPreference` and the type's
defaults; returns deliveries on any channel (0 when nobody reachable → callers'
zero-recipient `ERROR` logs keep working). `subject`/`message` override the template
(free-text broadcasts). Channel registry is extensible (Web Push later).

**`myapp/services/cancellation.py`** (orchestrator): `cancel_request(task, actor,
reason="") -> CancelOutcome(ok, code)`; conditional `UPDATE … WHERE status IN (…)`;
calls `presence.on_task_change`, `notify(type="task_cancelled")`, publishes
`task.updated`; tracking/chat hooks added in milestones 2/3.

---

## 11. Original open questions (kept for history)

1. ASGI: separate `ws` service (recommended) vs. switching `web` to uvicorn workers?
2. Telegram bots on `python-telegram-bot` (recommended) vs. extending the raw-HTTP bot?
3. "Do not disturb / on vacation" switch for volunteers — yes/no?
4. A cancel-request flow (client while pending, staff any open state) — OK to add?
5. Web Push (VAPID) — build it, or in-app + email + Telegram only?
6. Self-hosted OSRM — build data off-server and copy it, or skip and keep the public
   demo + haversine fallback?
7. Feedback as a matching signal — keep it display-only for now?
8. Defaults OK? Availability idle 30 min, chat archive 30 days, review edit 24 h,
   tracking trail ≤ 24 h, notifications pruned after 90 days.

---

## 12. Milestone 2 contracts (tracking + routes + navigation)

Owner notes (2026-10-02): manual assign of a do-not-disturb volunteer stays allowed but
needs a clear warning ("Волонтёр в режиме «не беспокоить» до …") + explicit confirmation,
an ID-only log line and an "urgent manual assignment" notice to the volunteer
(orchestrator). Permission re-check before every location delivery (and chat in
milestone 3). OSRM data (car + foot) is prepared on the owner's Mac with Docker; the
server only runs `osrm-routed`; RAM measured.

Already in place (orchestrator): `Profile.tracking_consent_at` (accounts 0009);
`HelpRequest.volunteer_last_lat/lng/at`, `tracking_stopped_at` and
`TaskLocationPoint` (myapp 0018, `myapp/models/tracking.py`);
`access.can_see_location(user, task)` (staff / client / assigned volunteer, active task
only) and `access.can_view_route(user, task)`.

**`myapp/services/tracking.py`** (geo-engineer) — never raises for expected failures:
- `give_consent(user)` / `revoke_consent(user)`; `has_consent(user)`.
- `can_share(user, task)` — assigned volunteer, `status == "active"`, consent given.
- `accept_point(volunteer, task_id, lat, lng, *, accuracy=None, speed=None, heading=None,
  source="web", recorded_at=None) -> PointOutcome(ok, code, payload)` — codes `ok`,
  `forbidden`, `invalid`, `inaccurate` (> 200 m), `jump` (> 150 km/h implied), `too_soon`
  (< 5 s since last accepted, also protects the Telegram path). On ok: writes
  `volunteer_last_*`, appends a trail row, clears `tracking_stopped_at`, publishes
  `location.updated {task_id, lat, lng, ts, accuracy}` to `task_<id>` and
  `map.marker {kind:"volunteer_live", task_id, lat, lng, ts}` to `map_staff`, calls
  `presence.mark_seen(volunteer, source=...)`.
- `stop(volunteer, task, reason="volunteer")` — sets `tracking_stopped_at`, deletes the
  trail, publishes `tracking.stopped {task_id, reason}`, notifies staff
  (`notify(type="tracking_stopped")`, in-app only by default).
- `end(task)` — task completed/cancelled: delete trail, clear `volunteer_last_*`,
  publish `tracking.stopped {task_id, reason:"task_ended"}`. Called from
  `complete_task_view` and `cancellation._after_cancel` (orchestrator wires).
- `latest(task)` → `{lat, lng, ts}` or None; `trail(task, limit=500)`.
- `purge(max_age=24h) -> int` + `purge_tracking` command (hourly cron).

**`myapp/services/routing.py`** (geo-engineer): `route(origin, dest, *, profile="driving"|"walking",
steps=True) -> dict` on top of `maps.route()` (geo.py import surface unchanged), never
raises, Redis/cache 2 min keyed by (origin rounded 3 dp, dest, profile). Result adds
`steps: [{maneuver, modifier, name, distance_m, duration_s, location:[lat,lng]}]`
(maneuver/modifier are OSRM codes → client-side `nav.*` i18n keys), `profile`,
`deep_links {google, yandex_maps, yandex_navi, dgis}`. `OSRM_BASE_URL` (car) and
`OSRM_FOOT_BASE_URL` (foot; unset → walking uses the haversine fallback with a
walking-speed ETA).
`task_route_view` uses it (`?profile=`), permissions via `access.can_view_route`.

**WS (realtime-engineer)**: inbound `location.update {task_id, lat, lng, accuracy,
speed?, heading?}` → `tracking.accept_point`; `tracking.stop {task_id}` →
`tracking.stop`. Outbound `location.updated` / `tracking.stopped` / `map.marker` are
delivered to a socket only if `access.can_see_location` holds for that socket's user
*now* (per-socket cache, invalidated on every `task.updated` for that task; a socket
that lost task visibility is unsubscribed with `unsubscribed {reason:"forbidden"}`).

---

## 13. Milestone 3 contracts (chat + reviews + profile location)

Owner notes (2026-10-02): the `$` problem was only local (`DJANGO_SECRET_KEY`, now
single-quoted; the server `.env` has no `$`). Keep matching on the saved profile point;
add "Обновить моё местоположение" (one tap, browser permission, validated, saved with
`location_updated_at`, clear text that matching uses it) + optional reminder when the
saved point is older than 30 days. Chat deliveries are re-checked like location.

Already in place (orchestrator): models `ChatMessage` (task, sender|null, kind
text|image|system, body ≤ 1000, image ≤ 5 MB, `image_variants`, `is_staff_post`,
`client_msg_id` unique per (task, sender) when set, `hidden_at/by`), `ChatReadState`
(task, user, last_read_id), `ChatReport` (message, reporter unique, reason, handled_*),
`TaskReview` (task 1:1, client, volunteer, stars 1–5, comment ≤ 1000, tags from
`REVIEW_TAGS`, hidden_*), `Profile.review_avg/review_count/location_reminded_at`
(migrations accounts 0010, myapp 0019); settings `CHAT_ARCHIVE_DAYS` (30),
`REVIEW_EDIT_HOURS` (24), `LOCATION_STALE_DAYS` (30); `access.can_read_chat`,
`access.can_write_chat`, `access.chat_archived`, `access.chat_ended_at`.

**`myapp/services/chat.py`** (realtime-engineer): `send_message(user, task, *, text=None,
image=None, client_msg_id="") -> Outcome(ok, code, message)` (rate 20/min/user/task,
length, staff → `is_staff_post`, dedupe on client_msg_id), `history(user, task, before_id=None,
limit=30)`, `mark_read(user, task, last_id)`, `unread_counts(user, task_ids)`,
`report(user, message, reason)` (→ `notify(staff, "chat_reported")`), `hide(staff, message)`,
`close(task)` (system message on complete/cancel), `message_payload(message, viewer)` —
display names only ("Куратор" for staff posts), no phone/email; links never auto-linked for
clients (frontend). WS: `chat.send/typing/read` in; `chat.message/typing/read/hidden` out
on `task_<id>`, each guarded by `can_read_chat`. Notification `chat_message` to the other
party when they are not viewing (Telegram per preference). Image thumbs: WebP via
`myapp/services/images.py` (Celery task; reused by feature 1 in milestone 6).

**`myapp/services/feedback.py`** (engagement-engineer): `can_review(user, task)`,
`submit_review(client, task, stars, comment, tags)` (create or edit within 24 h),
`hide/unhide(staff, review)` (ID-only log), `recompute_volunteer_stats(volunteer)`,
`public_summary(volunteer)` → `{avg, count}` only, `reviews_for(viewer, volunteer)` (comments
only to the volunteer and staff). stars ≤ 2 → `notify(staff, "review_low")`; every new review →
`notify([volunteer], "review_received")`; task completion → `notify([client], "review_request")`
(prompt). No matching weight.
