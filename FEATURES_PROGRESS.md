# Feature Pack v2 — Progress

Branch `feature-pack-v2` (local only — no push, no server until the owner writes "deploy").
Design: `FEATURES_PLAN.md`. Brief: `FEATURES_SPEC.md`.

| Milestone | Scope | Status |
|---|---|---|
| 0 | Plan, agent files | ✅ done 2026-10-02 (`2cfaf54`); owner answers in FEATURES_PLAN §9 |
| 1 | Realtime (12) + notifications (11) + availability (3) + DND + cancellation | ✅ done 2026-10-02 (`a4d5fe0`, `477c53c`) |
| 2 | Tracking (7) + routes/navigation (2) + self-hosted OSRM + DND manual assign | ✅ done 2026-10-02 (`a3b65b7`) |
| 3 | Chat (8) + feedback (9) + profile location button | ✅ done 2026-10-02 — waiting for owner OK |
| 4 | Achievements (6) + CRM charts (10) | ⏸ |
| 5 | Telegram main bot (4) + community bot (5) | ⏸ |
| 6 | Speed (1) + full QA + docs | ⏸ |
| 7 | Deploy (after "deploy") | ⏸ |

Baseline: 657 tests green at `2467bc5` (main).

## Log
- 2026-10-02 — milestone 0: branch created; `FEATURES_PLAN.md` written; agents created in
  `.claude/agents/` (realtime-engineer, geo-engineer, telegram-engineer,
  engagement-engineer, frontend-qa). Open questions: FEATURES_PLAN §9.
- 2026-10-02 — milestone 1 started after owner OK. Orchestrator: channels/uvicorn smoke-tested
  on Py 3.14 and pinned; settings (ASGI, channel layer DB 2, defaults); compose `ws`; migrations
  accounts 0008 + myapp 0016; cancellation service/view/tests. realtime-engineer: `/ws/`
  consumer, publish helpers, `access.py`, Notification models (0017), `notify()` + typed
  migration of all `notify_users` call sites, bell/page/preferences endpoints, poll fallback,
  `prune_notifications`. geo-engineer: presence service, middleware, `sweep_availability`,
  DND fields in ProfileForm, matching excludes DND. Backend suite: 792 OK.
  frontend-qa: realtime.js (WS + backoff + poll fallback), bell/toasts/language sync,
  notifications + preferences pages, DND/availability UI, cancel UI, +165 i18n keys/lang,
  Playwright matrix (5 roles × 390/1280 × light/dark: no h-scroll, no console errors),
  WS live + polling verified in a browser. Perf budget for one page +1 query (bell count).
  Result: 805 tests OK. `ws` process RSS ≈ 69 MB idle / 73 MB with 50 sockets (local).
  Cron to add at deploy: `sweep_availability` every 5 min, `prune_notifications` daily.
  Follow-up for milestone 2: re-check `task_<id>` subscriptions on `task.updated` before
  publishing live location.
- 2026-10-02 — milestone 2. Orchestrator: migrations accounts 0009 + myapp 0018,
  `access.can_see_location/can_view_route`, DND manual assign (warning + confirm_away + ID log
  + urgent notice), OSRM data built on the owner's Mac (`scripts/osrm_build.sh`, amd64 image
  v6.0.0, 93 s, peak 1.06 GB; car 191 MB / foot 250 MB on disk), compose profile `osrm`
  (measured RSS after 300 routes: car 223 MiB, foot 261 MiB; p50 8–10 ms), consent revoke in
  edit profile. geo-engineer: tracking + routing services, HTTP fallbacks, purge_tracking,
  map.js route/nav/tracker. realtime-engineer: WS location/tracking handlers, delivery
  guards (re-check before every location event; reusable for chat), urgent variant.
  frontend-qa: consent dialog, sharing banner, route/nav panel + deep links, live client/staff
  views, ops map live list, About privacy section; e2e 37/37 with real OSRM, matrix 46 pages OK.
  Result: 912 tests OK. Crons to add at deploy: `purge_tracking` hourly.
- 2026-10-02 — milestone 3. Orchestrator: `.env` `$` fix (local DJANGO_SECRET_KEY single-quoted;
  server .env checked read-only — no `$`), models chat/feedback (myapp 0019, accounts 0010),
  `access.can_read_chat/can_write_chat`, chat.close + review_request hooks, protected chat
  photos (permission-checked view + X-Accel-Redirect), "Обновить моё местоположение" +
  `remind_stale_locations`, role-based quick replies, prerender single-quoted args, CRM
  reviews volunteer filter. realtime-engineer: chat service/views/WS + guards, images.py,
  new notification types. engagement-engineer: reviews service/views/tags/admin.
  frontend-qa: chat UI, unread badges, review UI, e2e 45/45, matrix 125/125.
  Result: 1081 tests OK.

## Deploy checklist (accumulated; applied only after the owner writes "deploy")
- `.env` (server): add `OSRM_BASE_URL=http://osrm_car:5000`, `OSRM_FOOT_BASE_URL=http://osrm_foot:5000`
  (only with the `osrm` profile). Server `.env` has no `$` values (checked read-only 2026-10-02).
- Copy `osrm-data/{car,foot}` to `/opt/khayrkhoh/osrm-data/`; `docker compose --profile osrm up -d`;
  measure `docker stats`.
- nginx: `location /ws/ { proxy_pass http://127.0.0.1:8001; proxy_http_version 1.1;
  proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade";
  proxy_set_header Host $host; proxy_set_header X-Forwarded-Proto $scheme;
  proxy_read_timeout 3600s; }`
- nginx (private chat photos): `location ^~ /media/chat/ { return 404; }` and
  `location ^~ /protected-media/ { internal; alias /opt/khayrkhoh/media/; }`
  (compose sets `DJANGO_MEDIA_X_ACCEL=True` for `web`).
- crons (deploy user, docker compose exec -T web …): `sweep_availability` */5,
  `prune_notifications` daily, `purge_tracking` hourly, `remind_stale_locations` daily.
- Order: build → migrate → collectstatic → up -d (incl. new `ws`) → nginx reload → verify.
