# Feature Pack v2 — Progress

Branch `feature-pack-v2` (pushed to GitHub as an off-site backup since 2026-10-04, owner's request — **not** merged into `main`; no server until the owner writes "deploy").
Design: `FEATURES_PLAN.md`. Brief: `FEATURES_SPEC.md`.

| Milestone | Scope | Status |
|---|---|---|
| 0 | Plan, agent files | ✅ done 2026-10-02 (`2cfaf54`); owner answers in FEATURES_PLAN §9 |
| 1 | Realtime (12) + notifications (11) + availability (3) + DND + cancellation | ✅ done 2026-10-02 (`a4d5fe0`, `477c53c`) |
| 2 | Tracking (7) + routes/navigation (2) + self-hosted OSRM + DND manual assign | ✅ done 2026-10-02 (`a3b65b7`) |
| 3 | Chat (8) + feedback (9) + profile location button | ✅ done 2026-10-02 (`a1e8244`); owner follow-ups done in milestone 4 |
| 4 | Achievements (6) + CRM charts (10) + milestone-3 follow-ups | ✅ done 2026-10-02/03 (`56f2d6d`, `0186dae`, `95cf18b`) |
| 5 | Telegram main bot (4) + community bot (5) | ✅ done 2026-10-03 (`bb810a7`); follow-ups in milestone 6 |
| 6 | Speed (1) + full QA + docs | ✅ done 2026-10-03/04; owner decisions in FEATURES_PLAN §16 |
| 7 | Deploy (after "deploy") | 🟡 prep done 2026-10-04 (owner OK) — deploy itself waits for "deploy" |

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
- 2026-10-03 — milestone 5 (owner decisions: FEATURES_PLAN §15; deploy postponed until all 8
  milestones; per-milestone routine: deploy docs as we go, PG16 rollback rehearsal, repo bundle —
  `scripts/backup_repo.sh`). PTB 22.8 smoke-tested on Py 3.14 (offline dispatch with a fake HTTP
  layer). Extracted from views into `services/task_actions.py` (accept / complete / assign /
  create request / applications / broadcast) — the site and the bot share one copy of each rule.
  Main bot: `myapp/telegram/main_bot/{core,app}.py` (transport-agnostic core + thin PTB adapter):
  role menus (volunteer / client / staff / admin), client request wizard (location + contact
  buttons, `normalize_tj_phone`, `HelpRequestForm`), chat relay incl. photos (`/chat_<id>` hint in
  Telegram chat notifications), reviews, SOS, stages, live location → tracking, matching top 3 +
  assign with DND confirmation, SOS ack/resolve, applications (admin), donations, broadcast;
  one-time nonces on mutating buttons, per-chat rate limit, `/start <code>` deep link,
  RU/TJ/EN texts (`myapp/telegram/i18n.py`). Community bot: models (myapp 0023, accounts 0011
  `community_shoutouts`), `services/community.py` (single-use 24 h invites — hash stored, join
  binding, unknown joiners kicked, revoke on rejection / unlink + hourly `community_sweep`,
  moderation: links, flood, /report, /warn /mute /ban, `/mentor` manual badge, mirrored
  broadcasts/events pinned, weekly "Спасибо недели" without rankings, opt-out), `run_community_bot`
  (compose profile `community`), `docs/COMMUNITY_BOT.md`. Site: profile community card + invite
  page, "Открыть в Telegram" deep link, opt-out checkbox, CRM "Выдать «Наставник»".
  Found & fixed: bot token leaked into logs on a Telegram send error (requests error text has the
  URL); user text could break Telegram HTML parse mode (now escaped); `yandexnavi://` button would
  make Telegram reject the route message; `normalize_tj_phone` raises (wizard would crash on a
  typo); `@` doubled in the bot username / broken deep link; `manage.py test` can no longer use a
  real bot token from `.env`. RAM (offline, macOS, 50 updates): main ≈ 91 MiB, community ≈ 88 MiB.
  PG16 rehearsal incl. accounts 0011 + myapp 0023: forward → rollback (12 migrations) → old code
  OK → forward. Result: 1281 tests OK.

- 2026-10-03 — milestone 5 follow-ups (owner): client phone hidden on the site from a volunteer
  until they accept (`access.can_see_client_phone`, same rule as the bot); runbook §10a — new main-bot
  token via @BotFather `/revoke` after the deploy (old logs may hold the old one); local `.env` cleared
  of SMTP + both bot tokens (backup `~/khayrkhoh-backups/env-backup-2026-10-03`, never printed),
  `server/dev_safety.py` banner if a DEBUG run sees delivery keys, `export` step removed from
  docs/LOCAL_RUN.md; `docs/TEST_BOT.md` (separate test bot, scenario per role). Suite run in the
  production image: base pinned to `python:3.14.8-slim-trixie` (the server builds the image itself — a
  floating tag would give it whatever Python was newest that day), built `linux/amd64`.
  Found: Python 3.14 on Linux defaults to `forkserver`, Django 5.2 can't clone SQLite test DBs for it
  (`--parallel` errors) and on PG the forkserver workers hung under qemu — run serial or force `fork`.
  Found & fixed: a log filter (`server/log_filters.py`) now cuts bot tokens out of every log line and
  traceback; `httpx` pinned to WARNING (PTB logs request URLs with the token at INFO).
- 2026-10-03 — milestone 6. Speed: WebP thumb 320 / medium 960 for avatars, photo reports, pet
  photos (accounts 0012 + myapp 0024 `image_variants`), Celery `images.make_variants`,
  `{% responsive_image %}` (`<picture>` + WebP srcset, original as fallback — old iOS),
  `backfill_image_variants [--strip-metadata]`. Measured (QA data, 12 MP phone-like photos):
  landing 9.1 MB → 0.14 MB of images, rating 12.3 MB → 0.06 MB (44 px avatars were full originals),
  photo reports 13.8 MB → 0.2 MB, profile 1.5 MB → 8 KB; image geometry identical with/without
  variants (54 images, 12 pages). nginx (verified on nginx:1.27): `open_file_cache`, media 30 d +
  `nosniff`; static already 1 y immutable, brotli/gzip, HTTP/2. CDN-ready: `server/client_ip.py`
  (X-Real-IP from our nginx, CF-Connecting-IP only from Cloudflare ranges, `DJANGO_BEHIND_CLOUDFLARE`),
  `docs/CLOUDFLARE.md` (owner steps, NS at navjucloud, Full (strict), rollback).
  Found & fixed: (1) **production bug** — rate limits used `REMOTE_ADDR`, which behind nginx+Docker
  is the bridge gateway for everyone: 20 failed logins (5 password resets) from anyone locked
  everyone out for 15 min; (2) **privacy** — avatars / photo reports / pet photos kept EXIF GPS and
  the user's file name (photo reports are public on the landing page): now re-encoded without
  metadata, random names, also via the Django admin; `--strip-metadata` for old files; (3) four
  concurrent 12 MP uploads peaked at ~690 MB in the 300 MB celery_worker (OOM loop with acks_late):
  JPEG draft + one decode at a time → 119 MB; (4) `<source>` inside `display: contents` `<picture>`
  became a flex item and shifted avatars by the gap; (5) WCAG contrast: 135 axe-core serious
  findings → 0 (`--on-primary` token: dark ink on the dark theme's light fills; `--muted` #5b6473);
  chat log list semantics, heat-map role; (6) `update_profile` accepted non-image files as avatars.
  QA: browser matrix 288/288 (anon, client, volunteer, curator, admin × 390/1280 × light/dark + TJ/EN
  at 390; on uvicorn/ASGI; no h-scroll, no JS errors, no broken images); Lighthouse (local, mobile):
  accessibility 100, best practices 100, SEO 100, performance 72 (dev server without compression);
  WS reconnect in a browser: server killed → connecting → poll, restarted → back on WS in 9 s,
  re-subscribed, events from another process through Redis arrive. PG16 rehearsal: 14 migrations
  forward → back (1 s) → old code OK → forward.
  Load sanity (2026-10-04, production settings DEBUG=False, gunicorn 3 workers + uvicorn ws +
  postgres:16 + redis, **all pinned to one CPU core**, compose memory limits, native arm64 image;
  virtual users with 0.2–0.8 s think time, half signed in): 10 users → 19.5 rps, p50 11 / p95 40 /
  p99 67 ms; + 150 WebSockets → same latency, all 150 open; 25 users + 300 WS → 47 rps, p95 36 /
  p99 86 ms; saturation 80 users + 300 WS → 142 rps, p95 53 / p99 340 ms, max 4.4 s, the core ≈ 70 %
  busy; 0 errors, every response 200 in all phases. RSS: web 234 → 245 MiB (limit 700), ws 89 →
  137 MiB with 300 sockets (limit 256), redis 18 → 22 MiB, postgres 76 MiB. Caveat: an Apple M-series
  core is faster than the server's vCPU — expect lower throughput there, same headroom shape.
  Suite in the production image (Python 3.14.8, linux/amd64 under qemu, 8 workers, `fork`): first
  run found a **test-only race** — six modules made one temp MEDIA_ROOT at import and deleted it in
  `tearDownModule`; forked workers share that path, so one worker deleted it under another
  (never on macOS, where `spawn` re-imports per worker). Fixed with `myapp/testing.py::TempMediaRoot`
  (only the creating process removes it). Note: without `tblib` one failure in a Linux parallel run
  aborts the whole run ("cannot pickle 'traceback'") — run serially to see a traceback.
- **Milestone 7 prep (2026-10-04, owner OK; the deploy itself only after "deploy").** Owner decisions on
  the milestone-6 questions (FEATURES_PLAN §16): strip GPS from old photos at deploy (runbook §9 runs
  `backfill_image_variants --strip-metadata`); **new uploads are downscaled to 2560 px** on the long
  side (`images.MAX_ORIGINAL_SIDE`, in `strip_metadata`, so avatars / photo reports / pets / admin /
  chat — JPEG decoded via `draft` at 1/2–1/8 scale when that still covers 2560; palette PNGs converted
  before resampling; the backfill's in-place strip keeps old sizes); i18n.js stays one file until after
  Cloudflare. Secrets: `.env` / `.env.production` are gitignored and in no commit of any ref; no real
  secret value from them appears in any commit — except that the *local dev* `DJANGO_SECRET_KEY` was the
  old built-in `django-insecure-…` fallback that lived in `server/settings.py` in two early (public)
  commits → rotated locally (production's key is different and never was in git). `docs/LOCAL_RUN.md`
  / `docs/TEST_BOT.md`: `/Users/<name>/…` → `~/…`, folded into the unpushed milestone-6 commit (now
  `f680374`) so the public history never had it. Demo password: new read-only
  `check_demo_password` (every account vs seed_demo's public password + demo usernames, never writes —
  `hashers.check_password` without a setter, exit 1 if anything is found) and
  `create_initial_production_accounts` refuses the demo password before creating anything; local
  `.env.production`'s four `INITIAL_*_PASSWORD` are not the demo password; the production DB itself is
  checked read-only in runbook §1 (one-liner, old code) and §9 (`check_demo_password`).

## Deploy checklist (milestones 1–6 together = milestone 7; only after the owner writes "deploy")

Full runbook with commands, expected output and the rollback plan:
**`docs/DEPLOY_FEATURE_PACK_V2.md`** (rollback rehearsed on postgres:16, 2026-10-03). Order:

0. Local: full suite green → `git merge --ff-only feature-pack-v2` into `main` →
   `git push origin main` (the server pulls from the **public** GitHub repo; `git bundle` + scp is
   the no-publish alternative). **OSRM is not part of this deploy** (owner, 2026-10-03).
1. Server pre-flight (read-only): clean tree at `2467bc5`, `docker compose ps`, `nginx -t`,
   `/health/ready/`, no `$` in `.env`; `DJANGO_BEHIND_TLS_PROXY=True` in `.env` and
   `proxy_set_header X-Real-IP $remote_addr` in nginx `location /` (milestone 6: rate limits read
   the visitor IP from it); demo password accepts no account (read-only `shell -c` one-liner); `free -h`, `df -h`, `docker system df`, DB size, `du -sh media`
   (need ≥ 2 GB free: ≈ 0.6 GB image/cache + dump + media; RAM "available" noted for the later OSRM deploy).
2. Backups: `pg_dump -Fc`, `media.tgz`, `docker tag khayrkhoh:latest khayrkhoh:rollback-2467bc5`,
   `staticfiles.json`, nginx site file, `crontab -l`, `SHA256SUMS`; then on the Mac `rsync` the backup
   to `~/khayrkhoh-backups/` and run `scripts/verify_backup.sh` (checksums + test restore into postgres:16)
   — no verified copy, no deploy.
3. `git pull --ff-only` → `docker compose build` (`.env` unchanged).
4. `migrate` (accounts 0008–0012, myapp 0016–0024 — 14 migrations) — then steps 5–7 without a pause
   (old code can't run on the new schema).
5. `collectstatic`.
6. nginx: `map $http_upgrade $connection_upgrade` + `/ws/` + `/media/chat/` 404 +
   `internal /protected-media/` (DEPLOY.md §10a); milestone 6: `open_file_cache` (4 lines) +
   new `location /media/` (30 d, `nosniff`, `sendfile`) from DEPLOY.md §10 → `nginx -t` → reload —
   **before** `up -d` (`web` now serves chat photos via X-Accel).
7. `docker compose up -d` (new `ws`; `telegram_bot` now runs the PTB bot — same command; no `osrm`
   and no `community` profile this time — the community bot is enabled separately, docs/COMMUNITY_BOT.md).
8. Verify: health/ready, `check_realtime` in `web` and `ws`, `/ws/` handshake 101, logs, `docker stats`.
9. `backfill_achievements --dry-run`, then without (silent); `purge_chats --dry-run`;
   `check_demo_password` → OK;
   `backfill_image_variants --dry-run --strip-metadata`, then with `--strip-metadata` (owner
   decision; media backup from step 2 exists), then once more → `variants 0, stripped 0`.
10. crons (DEPLOY.md §13): `sweep_availability` */5, `purge_tracking` hourly, `prune_notifications`
    daily, `purge_chats` daily, `remind_stale_locations` daily 05:00 UTC, `community_sweep` hourly,
    `community_weekly` Mondays 05:00 UTC (both no-op until the community is configured).
10a. **New main-bot token** (runbook §10a): before milestone 5 a Telegram send error logged the URL
    with the token, so old server logs may hold it → @BotFather `/revoke` → new token into `.env`
    (`nano`, never printed) → `up -d --force-recreate telegram_bot celery_worker web ws` (restart
    doesn't re-read `.env`) → `/start` + a real notification arrives, celery queue 0.
11. Browser + Telegram check by role (runbook §11); 12. 24 h monitoring.
Later, separately: community bot (`.env` TELEGRAM_COMMUNITY_*, `--profile community`), OSRM,
Cloudflare (owner switches NS at navjucloud — `docs/CLOUDFLARE.md`; then `DJANGO_BEHIND_CLOUDFLARE=True`
+ `up -d --force-recreate web ws`).

Rollback: level 0 (switch one feature off), level 1 (migrate `myapp 0015` → `accounts 0007` with the
NEW image — rehearsed again on postgres:16.15 with milestone 6's 0012/0024, 14 migrations back in 1 s, then old commit + `rollback-2467bc5` image + collectstatic + nginx/crontab backups),
level 2 (pg_restore of the pre-deploy dump — loses everything written after step 2).
