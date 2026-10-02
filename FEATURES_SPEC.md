# KhayrKhoh — Feature Pack v2 (12 features, multi-agent)

> Brief for Claude Code, working in the EXISTING Django project (the live khayrkhoh.tj
> codebase). Read `CLAUDE.md`, `README.md`, `DEPLOY.md` first — all their rules still
> apply (services layer, `@role_required`, `@require_POST` for mutations, notifications
> sent explicitly from services not signals, hashed tokens, rate limits, structured
> ID-only logs, i18n via `static/js/i18n.js` EN/RU/TJ, hand-written CSS design system,
> no new CSS framework, never `seed_demo` in prod, no `DATABASE_URL`).
> Reports to the owner: short, in **Russian**. Code/docs/commits: English.

---

## 0. Ground rules

1. Work **locally** on branch `feature-pack-v2`. Do NOT push to `main` and do NOT
   touch the server until the owner writes **"deploy"**.
2. Stop after each milestone (section 4) with a Russian report: what's done, how to
   check it locally (commands, URLs, demo logins), test counts, open questions.
3. Every feature: permissions per role, localized EN/RU/TJ strings, mobile-first,
   light/dark theme, tests (unit + view/consumer), no N+1, no regressions — the full
   existing suite (657+) must stay green.
4. Server constraints: **1 vCPU, 3.8 GB RAM**. Prefer light solutions. Measure memory
   of every new container and report it.
5. Privacy first: the platform serves elderly people and young volunteers. Location,
   chat and phone data are visible only to the parties of an active task and staff
   where stated. Never expose phone/email in public shapes or WS payloads.
6. Secrets only via `.env` (+ placeholders in `.env.example`). Never print them.

---

## 1. Team

Create subagents in `.claude/agents/` (frontmatter `name`, `description`, `tools`;
body = responsibilities, owned files, rules from this spec). The main session is the
**orchestrator**: plans, runs agents in parallel where independent, reviews diffs,
resolves conflicts, keeps `FEATURES_PROGRESS.md` updated, reports to the owner.

| Agent | Owns | Features |
|---|---|---|
| `realtime-engineer` | ASGI/Channels setup, `myapp/realtime/`, consumers, notifications app, chat app | 11, 12, 8 (backend), WS parts of 7 |
| `geo-engineer` | `myapp/services/geo.py`, `maps.py`, `matching.py`, tracking + routing services, `static/js/map.js` | 2, 3, 7 |
| `telegram-engineer` | `myapp/telegram/` (bot package), management commands for both bots | 4, 5 |
| `engagement-engineer` | achievements, feedback, CRM analytics/charts | 6, 9, 10 |
| `frontend-qa` | templates, `style.css`, `i18n.js`, JS for new UI, e2e/visual checks, performance, nginx/Docker/deploy docs | 1, all UI integration, final QA |

Agents stay inside their files; shared files (`models/__init__.py`, `urls.py`,
`settings.py`, `base.html`, `style.css`, `i18n.js`, `docker-compose.yml`) are edited
through the orchestrator or with explicit coordination to avoid conflicts.

---

## 2. Foundation first: WebSockets (feature 12)

- **Django Channels** + **channels-redis** (Redis already runs for cache/Celery).
  Serve via an ASGI server (uvicorn workers under gunicorn, or daphne) — HTTP and WS
  from the same app. Keep the current WSGI behavior working identically.
- One authenticated WS endpoint, e.g. `/ws/`, session-auth, origin check
  (`AllowedHostsOriginValidator`), per-user group `user_<id>`, plus scoped groups:
  `task_<id>` (task parties + staff), `staff`, `map_staff`. Join only after the same
  permission checks as the HTTP views (`_can_view_task` etc.).
- Small JSON event protocol `{type, payload, ts}` documented in `docs/WS_PROTOCOL.md`.
- Client: one shared `static/js/realtime.js` — auto-reconnect with backoff, heartbeat,
  resubscribe, graceful fallback to polling if WS is unavailable.
- Rate limits on inbound WS messages (Redis). Payloads never contain phone/email.
- nginx: `location /ws/` with `Upgrade`/`Connection` headers, long `proxy_read_timeout`.
- Everything below that is "live" uses this layer: notifications, chat, tracking,
  task status changes, CRM counters, map markers.

---

## 3. Features

### 1. Faster photos and video for everyone
Measured: server responds in ~80–90 ms but network RTT to users is ~270 ms, so
payload size and request count dominate.
- On upload (avatars, photo reports, pet photos): generate resized **WebP** variants
  (thumb ~320 px, medium ~960 px) via Celery, keep the original; templates use
  `srcset`/`sizes` + `loading="lazy"` + width/height **without changing the layout**.
  Backfill command for existing media.
- nginx: long cache + `immutable` for hashed static, 7–30 d for media, gzip/brotli for
  text, HTTP/2, `open_file_cache`. Video: keep WebM + MP4 + mobile variant,
  `preload="metadata"`, pause off-screen.
- Prepare for a CDN: make all static/media URLs work behind **Cloudflare** (correct
  cache headers, no cookies on static/media, `CSRF_TRUSTED_ORIGINS` and real client IP
  via `CF-Connecting-IP` when `DJANGO_BEHIND_CLOUDFLARE=True`, rate limits use the real
  IP). Write `docs/CLOUDFLARE.md` with exact steps for the owner (switching NS at
  navjucloud). Do not switch DNS yourself.
- Report before/after page weight and image sizes.

### 2. Route from volunteer to client + navigation
- On task detail (assigned volunteer, client, staff) and the ops map: draw the
  **shortest driving route** (and walking toggle) from the volunteer's current
  location to the task location using OSRM via `maps.route()`; show distance + ETA.
- Turn-by-turn **in-app navigation**: step list from OSRM `steps=true` (localized
  maneuver text EN/RU/TJ), current step highlighted as the volunteer moves (uses
  feature 7 positions), auto re-route when off-route > ~50 m.
- "Open in navigator" buttons: Google Maps, Yandex Maps / Yandex Navigator, 2GIS deep
  links with destination coords.
- Production routing: the public OSRM demo is rate-limited. Add an optional
  **self-hosted OSRM** service (Tajikistan extract from Geofabrik, car + foot
  profiles) in compose with a build script; measure RAM; `OSRM_BASE_URL` switches to it.
  Routing must never raise (existing haversine fallback stays).
- Cache routes briefly (Redis) per (from-rounded, to, profile).

### 3. Automatic availability (remove manual status)
- Remove the manual availability selector from the UI. `availability_status` becomes
  **system-computed**:
  - `busy` — has an active task;
  - `available` — approved volunteer, no active task, seen recently (WS connection,
    page activity heartbeat, or Telegram bot interaction within N minutes, default 30);
  - `offline` — not seen for longer than N minutes.
- Update on events (accept/assign/complete/cancel, WS connect/disconnect, heartbeat,
  bot activity) + a periodic sweep. Keep the field so matching keeps working
  (offline still excluded). Staff can still see the status, nobody sets it manually.
- Optional volunteer "do not disturb / on vacation" switch is allowed ONLY if it maps
  to excluding from matching — ask the owner before adding it.

### 4. Telegram bot → almost a full platform
Extend the existing bot (`run_telegram_bot`), reusing the same service functions as the
website (no duplicated rules). Inline keyboards, localized RU/TJ/EN (user's language),
works only for linked accounts (existing `/link` flow), role-aware menus:
- **Volunteer:** nearby/region pending tasks (paged), task details, accept, advance
  work stage, complete, SOS button, my active task, send live location (Telegram
  location messages feed feature 7), my rating & achievements, notifications.
- **Client:** create help request step by step (type → description → address or shared
  location → phone → priority, with confirmation), my requests + status, volunteer's
  public info and ETA, chat relay with the volunteer (feature 8), leave feedback
  (feature 9).
- **Curator/Admin:** overdue / stale / unassigned / open emergencies lists, acknowledge
  / resolve SOS, recommend & assign a volunteer (matching top 3), approve volunteer
  applications (admin), approve donation offers, broadcast to a region.
- All notifications (feature 11) can also be delivered to Telegram per user preference.
- Per-chat rate limits, idempotent callbacks (no double accept), every action logged
  with IDs only. Long polling stays (no webhook) unless webhook is clearly better —
  document the choice.

### 5. Community bot (second bot) for volunteers
- A **separate bot** (`TELEGRAM_COMMUNITY_BOT_TOKEN`, `_USERNAME` — the owner creates
  it in @BotFather) that manages **volunteer community groups**: one general
  supergroup + optional per-region topics/groups (`TELEGRAM_COMMUNITY_CHAT_IDS`).
- Access: only **approved volunteers** with a linked Telegram get a **one-time invite
  link** (generated by the bot, expires, single use) from the website or the main bot.
  When volunteer status is revoked or the account is deactivated, the bot removes them.
- Features: welcome message with rules, moderation (anti-spam/flood limits, link
  filter, `/report` to staff, staff `/warn` `/mute` `/ban`), pinned announcements
  mirrored from website broadcasts and new events, weekly top volunteers + new
  achievements shout-outs, `/help`.
- Privacy: never post client data in the community. Document the setup steps for the
  owner in `docs/COMMUNITY_BOT.md` (create bot, create supergroup, add bot as admin,
  get chat id, fill `.env`).

### 6. Achievements with cool badges
- Models: `Achievement` (code, title/description EN/RU/TJ via i18n keys, criteria
  description, icon, tier bronze/silver/gold/legendary, sort order) and
  `UserAchievement` (user, achievement, awarded_at, unique together).
- Award engine in a service, triggered from events (task completed, feedback received,
  SOS handled, donation delivered, streaks) + backfill command for existing data.
  Starter set (owner can tweak): first help, 5/25/100 tasks, emergency helper, 5-star
  streak, night owl/early bird, all help types, region hero (top of region in a month),
  donation courier, pet finder, 30-day streak, mentor (community bot activity),
  legendary (500 points).
- **Original SVG badges** designed in the site's style (no copyrighted art), animated
  shine on hover, locked state greyed with progress (e.g. 17/25).
- Profile shows earned badges; **click → modal** with name, what it's for, date earned,
  progress for locked ones. Public volunteer cards/rating show top badges.
- New achievement → realtime notification (feature 11) + Telegram + community shout-out.

### 7. Real-time tracking — only while the volunteer has an active task
- Starts automatically when a volunteer's task becomes **active** (after a clear
  in-app consent prompt the first time, explaining who sees the location and when),
  stops automatically on complete/cancel. Visible indicator "Ваше местоположение видят
  клиент и куратор" with a stop button (stopping notifies staff).
- Browser `navigator.geolocation.watchPosition` → WS every ~10–15 s or on >25 m move;
  Telegram live location also accepted. Validate coords, drop jumps/implausible speed.
- Who sees it: the task's **client** (volunteer marker + route + ETA) and **staff**
  (ops map, live markers). Nobody else. Never outside an active task.
- Storage: keep only the latest position on the task/profile + a short trail for the
  active task; **delete the trail when the task ends** (or after 24 h max). Document
  this retention in the privacy section of the About page / terms.
- Feeds features 2 (navigation/re-route), 3 (activity), CRM.

### 8. Chat between client and volunteer (website)
- One chat per HelpRequest, between the client and the assigned volunteer; staff can
  read (for safety) and post as "Куратор". Opens when the task becomes active;
  read-only after completion; archived after N days.
- Realtime over WS, typing indicator, read receipts, unread badge, message history
  paginated, text + images (≤5 MB, WebP thumbs), quick replies for elderly-friendly use
  ("Я в пути", "Я на месте", "Позвоните мне"), large readable font option.
- Abuse: rate limits, max length, report message → staff; links not auto-clickable for
  clients. Phone numbers are not auto-shared — staff policy decides.
- Telegram relay (feature 4): if the user prefers, messages also arrive in the bot and
  replies go back to the chat.

### 9. Feedback / reviews
- After completion the **client** can leave one review per task: 1–5 stars + optional
  comment + quick tags (вежливый, пунктуальный, помог сверх ожидаемого...). Editable
  for 24 h. Prompt via notification, website and bot.
- Volunteer profile shows average rating + count; comments visible to the volunteer
  and staff; public cards show only the average. Staff can hide abusive reviews
  (logged).
- Low rating (≤2) → alert to staff. Ratings feed achievements and are available as a
  (small, documented) matching signal — ask the owner before changing matching weights.

### 10. More charts in the CRM
Self-hosted Chart.js (no CDN). Admin/curator dashboard + a new CRM "Аналитика" tab,
filters by period (7/30/90 days, custom) and region, all queries aggregated
(annotated counts, constant query count, cached 5 min in Redis):
- requests over time (created / completed / cancelled), by help type, by region,
  by priority;
- funnel pending → active → completed, average time to accept / to complete,
  overdue and stale trends;
- volunteer activity: active volunteers per day, top volunteers, tasks per volunteer
  distribution, availability split (live);
- emergencies over time and response time; donations by status/category;
  feedback average over time and rating distribution; new registrations by role;
- heatmap of requests on the map (Leaflet heat layer) by period.
Export of each chart's data to CSV. Charts update live via WS where cheap.

### 11. Notifications
- `Notification` model (recipient, type, title/body i18n key + params, link, read_at,
  created_at) + service `notify(user, type, ...)` that fans out to channels by user
  **preferences**: in-app (realtime WS), email (Celery), Telegram (Celery), optional
  **Web Push** (VAPID, service worker) for when the site is closed.
- Navbar **bell** with unread count, dropdown of latest, "mark all read", full page
  with filters. Toast for live events. Preferences page per type × channel.
- Types: task created in my region / assigned / accepted / stage changed / completed,
  chat message, SOS and its updates, review received, achievement unlocked,
  application approved/rejected, donation status, pet match, broadcasts/events,
  overdue/stale (staff).
- Replace ad-hoc direct sends in the code with this service where it fits, keeping
  existing guarantees (idempotency, zero-recipient ERROR log, one email per recipient,
  no address leaks). Old notifications auto-pruned after 90 days.

---

## 4. Milestones (stop and report after each)

0. **Plan.** Read the codebase; write `FEATURES_PLAN.md` (design per feature, models &
   migrations, WS protocol, permission matrix for new endpoints/events, memory budget
   per new container, risks), create agent files. Wait for OK.
1. **Realtime foundation + notifications** (12, 11) + automatic availability (3).
2. **Tracking + routes + navigation** (7, 2) incl. optional self-hosted OSRM.
3. **Chat + feedback** (8, 9).
4. **Achievements + CRM charts** (6, 10).
5. **Telegram main bot expansion** (4) and **community bot** (5).
6. **Speed** (1), full QA pass: all roles × desktop/390 px × light/dark × EN/RU/TJ,
   no horizontal scroll, accessibility, PageSpeed, WS reconnect tests, load sanity on
   1 vCPU. Update `CLAUDE.md`, `README.md`, `DEPLOY.md` (ASGI, nginx `/ws/`, new
   services, new env vars, owner steps).
7. **Deploy** — only after the owner writes "deploy": merge to `main`, push, deploy per
   `DEPLOY.md` (migrations, collectstatic, new containers), verify every feature on
   https://khayrkhoh.tj, report memory usage and any owner actions left.
