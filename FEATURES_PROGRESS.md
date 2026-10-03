# Feature Pack v2 — Progress

Branch `feature-pack-v2` (local only — no push, no server until the owner writes "deploy").
Design: `FEATURES_PLAN.md`. Brief: `FEATURES_SPEC.md`.

| Milestone | Scope | Status |
|---|---|---|
| 0 | Plan, agent files | ✅ done 2026-10-02 (`2cfaf54`); owner answers in FEATURES_PLAN §9 |
| 1 | Realtime (12) + notifications (11) + availability (3) + DND + cancellation | ✅ done 2026-10-02 (`a4d5fe0`, `477c53c`) |
| 2 | Tracking (7) + routes/navigation (2) + self-hosted OSRM + DND manual assign | ✅ done 2026-10-02 (`a3b65b7`) |
| 3 | Chat (8) + feedback (9) + profile location button | ✅ done 2026-10-02 (`a1e8244`); owner follow-ups done in milestone 4 |
| 4 | Achievements (6) + CRM charts (10) + milestone-3 follow-ups | ✅ done 2026-10-02 — waiting for owner OK |
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

- 2026-10-02 — milestone 4 (owner decisions: FEATURES_PLAN §14). Milestone-3 follow-ups:
  quiet chat safety flags (`services/chat_flags.py`, `ChatMessage.flags`, myapp 0020,
  staff-only in-app `chat_flagged`, 1 per task × category × day); chat photo originals
  re-encoded without EXIF/GPS/XMP (`images.strip_metadata`); 1–2★ review comments for
  coordinators only (`feedback.can_see_comment`); `check_realtime` command + DEPLOY.md §10a
  nginx `/ws/` + protected media, verified against nginx:1.27 in Docker. Achievements:
  myapp 0021 (models + starter set "без гонки"), `services/achievements.py` (4 aggregate
  queries per evaluation, hooks: task completed / review saved·unhidden / donation delivered /
  pet resolved), 12 original SVG badges, profile grid + `<dialog>`, top-3 on rating and CRM
  volunteer page, `backfill_achievements`, admin (threshold/tier/on-off; award removal logged).
  Analytics: `services/analytics_v2.py` (16 charts, 20 queries cold / 0 cached, 5-min cache,
  Asia/Dushanbe days, weekly > 92 days), `/myapp/crm/analytics/` + CSV + heat.json,
  self-hosted Chart.js 4.4.7 (≈70 KB gzip, analytics page only) + leaflet.heat 0.2.0,
  palette validated (CVD ΔE ≥ 8.4), table view per chart, 14-day SVG sparkline on the staff
  dashboard. Found & fixed: `.gc-dialog` never centred (global margin reset — also the
  milestone-2 consent dialog), `.kpi-row` phone rule overridden (tiles half-width on phones),
  `seed_demo` created tasks after their accept time. Browser matrix: 5 roles × 390/1280 ×
  light/dark — no horizontal scroll, no JS errors (on ASGI). Result: 1180 tests OK.
- 2026-10-03 — milestone 4 follow-ups (owner OK + decisions): chat retention option A
  (`services/chat_retention.py`, `purge_chats` daily; photos 90 d / messages 180 d after the task
  ended; flagged / reported kept until reviewed + 180 d; settings `CHAT_*_DAYS`; About privacy line);
  curator "Проверено" on flagged/reported messages (`ChatMessage.reviewed_at/by`, myapp 0022,
  `chat.review`, also closes open reports); public rating without places/points (region → name,
  avg + badges, paginated, cache key v2); chat-flag stats in CRM analytics (by topic with share +
  unreviewed, by help type); compression verified from outside (br + gzip on for static and HTML);
  deploy runbook + rollback plan `docs/DEPLOY_FEATURE_PACK_V2.md`, rollback rehearsed on
  postgres:16 — found and fixed: 0021 reverse failed on PostgreSQL (pending trigger events).
- 2026-10-03 — deploy prep (owner: OSRM later, separately): runbook answers how code reaches the
  server (push `main` to the public GitHub origin), pre-flight memory/disk + size estimates (image
  ≈ 265 → 300 MB unpacked, amd64 build), backup copy to the Mac + `scripts/verify_backup.sh`
  (tested end to end). Found: `osrm-data/` was not in `.dockerignore` — a later OSRM deploy would
  have baked ~0.5 GB into every image; fixed.

## Deploy checklist (milestones 1–4 together; only after the owner writes "deploy")

Full runbook with commands, expected output and the rollback plan:
**`docs/DEPLOY_FEATURE_PACK_V2.md`** (rollback rehearsed on postgres:16, 2026-10-03). Order:

0. Local: full suite green → `git merge --ff-only feature-pack-v2` into `main` →
   `git push origin main` (the server pulls from the **public** GitHub repo; `git bundle` + scp is
   the no-publish alternative). **OSRM is not part of this deploy** (owner, 2026-10-03).
1. Server pre-flight (read-only): clean tree at `2467bc5`, `docker compose ps`, `nginx -t`,
   `/health/ready/`, no `$` in `.env`; `free -h`, `df -h`, `docker system df`, DB size, `du -sh media`
   (need ≥ 2 GB free: ≈ 0.6 GB image/cache + dump + media; RAM "available" noted for the later OSRM deploy).
2. Backups: `pg_dump -Fc`, `media.tgz`, `docker tag khayrkhoh:latest khayrkhoh:rollback-2467bc5`,
   `staticfiles.json`, nginx site file, `crontab -l`, `SHA256SUMS`; then on the Mac `rsync` the backup
   to `~/khayrkhoh-backups/` and run `scripts/verify_backup.sh` (checksums + test restore into postgres:16)
   — no verified copy, no deploy.
3. `git pull --ff-only` → `docker compose build` (`.env` unchanged).
4. `migrate` (accounts 0008–0010, myapp 0016–0022) — then steps 5–7 without a pause
   (old code can't run on the new schema).
5. `collectstatic`.
6. nginx: `map $http_upgrade $connection_upgrade` + `/ws/` + `/media/chat/` 404 +
   `internal /protected-media/` (DEPLOY.md §10a) → `nginx -t` → reload — **before** `up -d`
   (`web` now serves chat photos via X-Accel).
7. `docker compose up -d` (new `ws`; no `osrm` profile this time).
8. Verify: health/ready, `check_realtime` in `web` and `ws`, `/ws/` handshake 101, logs, `docker stats`.
9. `backfill_achievements --dry-run`, then without (silent); `purge_chats --dry-run`.
10. crons (DEPLOY.md §13): `sweep_availability` */5, `purge_tracking` hourly, `prune_notifications`
    daily, `purge_chats` daily, `remind_stale_locations` daily 05:00 UTC.
11. Browser check by role (runbook §11); 12. 24 h monitoring.

Rollback: level 0 (switch one feature off), level 1 (migrate `myapp 0015` → `accounts 0007` with the
NEW image, then old commit + `rollback-2467bc5` image + collectstatic + nginx/crontab backups),
level 2 (pg_restore of the pre-deploy dump — loses everything written after step 2).
