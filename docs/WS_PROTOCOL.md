# KhayrKhoh WebSocket protocol (v1)

One endpoint: **`/ws/`** (served by the same gunicorn + uvicorn-worker app as the pages —
`khayrkhoh-web.service` running `server/asgi.py`; nginx passes `/ws/` with
`Upgrade`/`Connection` headers).
Code: `myapp/realtime/` (`consumers.py`, `events.py`, `groups.py`, `ratelimit.py`,
`publish.py`). Tests: `myapp/tests_realtime.py`, `myapp/tests_realtime_tracking.py`,
`myapp/tests_chat.py`.

## Connecting

* Auth = the normal Django session cookie (`AuthMiddlewareStack`). The `Origin`
  header must match `ALLOWED_HOSTS` (`AllowedHostsOriginValidator`), otherwise the
  handshake is rejected (HTTP 403).
* Anonymous / inactive user → the socket is accepted and immediately closed with
  **4401**. Clients must not reconnect on 4401 (log in first).
* On success the server sends `hello` and joins the socket to:
  * `user_<id>` — always;
  * `staff` and `map_staff` — curators and admins only;
  * `volunteers_<region>` — volunteers with a region (live SOS markers of their region);
    re-synced when the periodic user re-read sees a role/region change.
* The connection counts as presence (`presence.mark_seen(user, source="ws")`).

## Envelope

Every frame, both directions, is one JSON object:

```json
{"type": "<domain.event>", "payload": {...}, "ts": "<ISO 8601>", "id": "<optional client id>"}
```

* Client frames: `type` required, `payload` optional object, `id` optional string
  (≤ 64 chars) or integer — echoed back in the matching `pong` / `error` /
  `subscribed` as `id` (and in `error.payload.ref`).
* Server frames always carry `ts`.

## Client → server

| type | payload | who | reply |
|---|---|---|---|
| `ping` | `{}` | any | `pong` |
| `presence.heartbeat` | `{visible: bool}` | any — send every 25 s | none (updates `last_seen_at`) |
| `subscribe` | `{topic: "task", id: <int>}` | anyone allowed to open the task page (same rule: `services/access.py::can_view_task`) | `subscribed {topic, id}` or `error forbidden` |
| `unsubscribe` | `{topic: "task", id: <int>}` | any | `unsubscribed {topic, id}` |
| `notification.read` | `{ids: [<int>, …]}` or `{all: true}` | recipient (others' ids ignored) | `notification.read` to **all** of the user's sockets |
| `location.update` | `{task_id, lat, lng, accuracy?, speed?, heading?}` (numbers; `accuracy` m, `speed` m/s, `heading` °) | the task's assigned volunteer, task `active`, tracking consent given | `ack {for: "location.update", task_id, code: "ok"}` or `error {code}` (see below) |
| `tracking.stop` | `{task_id}` | the task's assigned volunteer, task `active` | `ack {for: "tracking.stop", task_id, code: "ok"}` or `error forbidden` |
| `chat.send` | `{task_id, text, client_msg_id}` (`text` ≤ 1000 chars after trimming; `client_msg_id` `[A-Za-z0-9_.:-]{1,64}`, also send it as the frame `id`) | the task's client / assigned volunteer while `active`; staff (shown as "Куратор") | `ack {for: "chat.send", task_id, code: "ok", client_msg_id, id}` + the message as `chat.message` on the task group, or `error {code}` |
| `chat.typing` | `{task_id}` | anyone who may write in the chat | none; `chat.typing` to the other sockets of the task group (`error forbidden` / `rate_limited`) |
| `chat.read` | `{task_id, last_id}` | anyone who may read the chat (parties + staff) | `ack {for: "chat.read", task_id, code: "ok", last_id}` (`last_id: null` = cursor unchanged); `chat.read` to the group on a change |

A socket can hold at most 20 task subscriptions (`error too_many_subscriptions`).
"Not found" and "not allowed" both answer `forbidden` (no existence oracle).
After a reconnect the client must re-send its `subscribe` frames.

### Chat (milestone 3)

One chat per task (`services/chat.py`, models in `myapp/models/chat.py`). Rules
(`services/access.py`): `can_read_chat` = staff always (archived included); the
task's client and assigned volunteer once a volunteer is assigned, while
`active` and read-only after completion/cancellation, until the chat is
archived (`CHAT_ARCHIVE_DAYS`, 30, after the end). `can_write_chat` = can read +
task `active`. A pending task has no chat (a volunteer who can *view* it can't
read it).

`chat.send` replies:

| reply | meaning |
|---|---|
| `ack {for: "chat.send", task_id, code: "ok", client_msg_id, id}` | stored (or a retry of an already stored `client_msg_id` — same `id`, nothing re-published) |
| `error empty` / `too_long` | nothing to send / > 1000 chars |
| `error rate_limited` | > 20 messages / min / task (socket bucket and service limit) |
| `error invalid` | malformed `client_msg_id` |
| `error forbidden` | can't write here (also: task missing, ended, archived) |
| `error bad_request` | `task_id` / `text` missing or wrong type |

The sender's own sockets get the `chat.message` too (`mine: true`) — match it
to the pending bubble by `ack.id`. Images go over HTTP (below), never the socket.

**Per-viewer payloads.** The server publishes one internal payload per chat
event; each socket personalises it right before sending
(`consumers.DELIVERY_TRANSFORMS` → `events.for_viewer`), the same function
the HTTP endpoints use (`chat.message_payload`):

* `mine` — computed from the author id, which itself never leaves the server;
* a staff post shows `sender_display: "Куратор"` + `label_key: "chat.staff_label"`
  to non-staff sockets; only staff sockets see the curator's real name;
* a hidden message is a stub for non-staff (`hidden: true`, `body: ""`, no
  image urls); staff get the full text with `hidden: true`;
* `chat.typing` is not echoed to the typist's own sockets;
* a curator's `chat.read` goes to staff sockets only.

Display names are `Profile.full_name` or the username — never a phone number or
an email. Message text is plain text: clients render it with `textContent`
and never turn links into anchors.

### Live location (milestone 2)

`location.update` → `services/tracking.py::accept_point(user, task_id, lat, lng,
accuracy=, speed=, heading=, source="web")`. The service owns every rule (assigned
volunteer, active task, consent, coordinate range, accuracy ≤ 200 m, implied speed
≤ 150 km/h, ≥ 5 s since the last accepted point); the socket only checks the shape.

| reply | meaning | client action |
|---|---|---|
| `ack {for, task_id, code: "ok"}` | point stored and published | keep sending (every 10–15 s or > 25 m moved) |
| `error bad_request` | `task_id` not a positive int, `lat`/`lng` missing or not numbers, an optional field not a number | fix the frame |
| `error rate_limited` | more than 1 point per 5 s (socket stays open) | drop it, keep the cadence |
| `error too_soon` | < 5 s since the last accepted point (also covers the Telegram path) | drop it |
| `error inaccurate` | `accuracy` > 200 m | drop it, wait for a better fix |
| `error jump` | implied speed > 150 km/h | drop it (the next consistent point is accepted) |
| `error invalid` | coordinates out of range | drop it |
| `error forbidden` | not the assigned volunteer / task not active / no consent | **stop `watchPosition`** for this task |
| `error unsupported` / `server_error` | tracking service missing / crashed | back off, retry later |

`tracking.stop {task_id}` ("stop sharing" button) → `tracking.stop(user, task,
reason="volunteer")` — only the assigned volunteer of an active task (`error
forbidden` otherwise, also for a missing task). It clears the latest position,
deletes the trail, publishes `tracking.stopped` and gives staff an in-app
`tracking_stopped` notification. Sending a new `location.update` resumes sharing.

## Server → client

| type | payload | group |
|---|---|---|
| `hello` | `{user_id, role}` | this socket |
| `pong` | `{}` | this socket |
| `error` | `{code, ref}` | this socket |
| `ack` | `{for, task_id, code}` | this socket |
| `subscribed` / `unsubscribed` | `{topic, id}` (+ `reason: "forbidden"` when the server dropped it) | this socket |
| `notification.new` | `{id, type, title_key, body_key, params, link, read, created_at}` | `user_<id>` |
| `notification.read` | `{ids: [...], unread}` or `{all: true, unread: 0}` | `user_<id>` |
| `task.updated` | `{task_id, status, work_stage}` | `task_<id>` |
| `availability.changed` | `{user_id, status}` | `staff` |
| `location.updated` | `{task_id, lat, lng, ts, accuracy}` (+ `eta_min?`) | `task_<id>` — **guarded**: only sockets whose user passes `access.can_see_location` now (staff, the client, the assigned volunteer; task `active`) |
| `tracking.stopped` | `{task_id, reason: "volunteer" \| "task_ended"}` | `task_<id>` — **guarded**: staff, the client, the assigned volunteer (no "active" condition, so "task ended" reaches the client) |
| `map.marker` | live volunteer: `{kind: "volunteer_live", task_id, lat, lng, ts}`; removal: `{kind: "volunteer_live", task_id, removed: true}`; other kinds `{kind, id, …}` | `map_staff` — **guarded**: user still active staff; a positioned marker naming a `task_id` also needs `can_see_location` |
| `chat.message` | `{task_id, id, kind: "text"\|"image"\|"system", body, sender_display, sender_role: "client"\|"volunteer"\|"staff"\|"system"\|"member", label_key, is_staff_post, hidden, thumb_url, image_url, created_at, mine}` — system messages carry an i18n key as `body` (`chat.system.closed` / `chat.system.cancelled`) | `task_<id>` — **guarded** by `access.can_read_chat`, personalised |
| `chat.message_updated` | same shape as `chat.message` (WebP thumb ready, hidden / unhidden) — replace the bubble with this `id` | `task_<id>` — guarded, personalised |
| `chat.typing` | `{task_id, sender_display}` — show "… is typing" for ~4 s | `task_<id>` — guarded, not to the typist |
| `chat.read` | `{task_id, user_display, last_id, mine}` — read ticks on messages with `id ≤ last_id` | `task_<id>` — guarded; a curator's receipt only to staff |
| `chat.hidden` | `{task_id, id, hidden}` (moderation; a `chat.message_updated` follows) | `task_<id>` — guarded |
| `sos.marker` | active SOS: `{id, region, status, lat, lng, created_at}`; ended (resolved/cancelled): `{id, region, removed: true}` — ids + point only, never a name, phone or reason | `staff` + `volunteers_<region>` — **guarded** (`SosGuard`): staff, or an active volunteer whose *current* region equals `payload.region`. Sent by `services/emergency.py` on report / acknowledge / resolve / cancel (`publish.sos_marker`). The ops map draws a pulsing marker and drops it on removal |
| `crm.counters` | later milestone | `staff` |

### Delivery guards (re-check before every sensitive delivery)

Group membership alone is not trusted: a task's parties change (accept, assign,
complete, cancel) while sockets stay subscribed. The consumer keeps a table
`DELIVERY_GUARDS = {event type: guard}` (`myapp/realtime/consumers.py`); a guarded
event is sent to a socket only if its guard says yes for that socket's user.

* `TaskGuard(name, predicate)` reads `payload.task_id`, evaluates
  `predicate(user, task)` on a fresh DB row on first use and caches the answer per
  socket under `(name, task_id)` (≤ 256 entries, each re-evaluated after
  `GUARD_TTL_SECONDS` = 120 s even without a `task.updated` — e.g. a chat that
  became archived). Missing task → no.
* Every `task.updated` for a task drops that task's cached answers, then re-checks
  `access.can_view_task`; if the user lost it, the socket leaves `task_<id>` and gets
  `unsubscribed {topic: "task", id, reason: "forbidden"}` (after the `task.updated`
  itself, so the UI can say "this request was taken"). Clients must not
  re-subscribe on `reason: "forbidden"`.
* The socket's user is re-read at most every 60 s (`USER_RECHECK_SECONDS`), on any
  inbound frame (the 25 s heartbeat included) and any outbound event: deactivated →
  close **4401**; lost staff role → leaves `staff` / `map_staff`; any role/region
  change clears the guard cache.
* Chat (milestone 3): every `chat.*` event type is guarded by
  `TaskGuard("chat", access.can_read_chat)` and then personalised by
  `DELIVERY_TRANSFORMS` (see **Chat**). A volunteer who subscribed to a pending
  task never receives its chat; a party whose chat got archived, or a
  volunteer the task was taken from, stops receiving it at the next
  `task.updated` (or within the TTL).

Clients receive `location.updated` / `tracking.stopped` only while subscribed to
`task_<id>` (client + assigned volunteer + staff on the task page); the staff map
gets positions via `map.marker` on `map_staff` without subscribing.

`notification.new`: render `title_key` / `body_key` through `static/js/i18n.js`
with `params` as the `{placeholder}` arguments (same as `data-i18n-args`). `link`
is an internal relative URL or `""`.

**No payload ever contains a phone number or an email address.** Builders live
only in `myapp/realtime/events.py`; notification `params` are sanitised by
`services/notify.py::safe_params`. Contact details exist only in the
server-rendered email/Telegram text sent to the addressed party.

## Error codes

`bad_json`, `bad_request` (shape / binary frame), `unknown_type`, `unsupported`
(known type, later milestone / service missing), `forbidden`, `rate_limited`,
`too_many_subscriptions`, `server_error` (a handler crashed; the socket stays open),
the `location.update` rejections `invalid`, `inaccurate`, `jump`, `too_soon`, and
the `chat.send` rejections `empty`, `too_long` (+ `invalid`, `rate_limited`,
`forbidden`).

## Close codes

| code | meaning | client action |
|---|---|---|
| 4401 | not authenticated, or the account was deactivated while connected | stop; reconnect only after login |
| 4408 | message > 4 KB, or sustained flooding | back off (≥ 30 s) before reconnecting |
| 1000/1001/1006 | normal / going away / network | reconnect with backoff 1 s → 30 s + jitter |

## Rate limits (per user, shared cache — Redis in production)

| bucket | limit | on excess |
|---|---|---|
| all inbound (default) | 60 / min | `error rate_limited`; > 2× in the window → close 4408 |
| `location.update` | 1 / 5 s | `error rate_limited` (dropped; never a close) |
| `chat.send` | 20 / min per task | `error rate_limited` |
| `chat.typing` | 1 / 2 s | `error rate_limited` (dropped) |

Frames larger than 4 KB close the socket with 4408 (uvicorn also caps frames at
16 KB — `ws_max_size` in `server/asgi_worker.py`).

## Publishing from server code

Sync code (views, services, Celery) uses `myapp/realtime/publish.py`:
`send_to_user(user|id, type, payload)`, `send_to_task(task|id, …)`,
`send_to_staff(…)`, `send_to_map_staff(…)`, `send_to_group(group, …)`. They never
raise: a broken/missing channel layer logs one WARNING and returns `False`.
Inside the consumer's event loop use `await channel_layer.group_send(...)` with
`{"type": "realtime.event", "event": <envelope>}` instead.

## HTTP fallback (WebSocket blocked)

`GET /myapp/realtime/poll/?since=<notification id>` (login required) returns

```json
{"events": [<notification.new envelopes, oldest first, ≤ 50>], "cursor": <id>, "unread_count": <int>}
```

Unread notifications with `id > since` only. Start with `since=0`, then pass the
returned `cursor`. Suggested interval: 30 s while the socket is down.

### Chat over HTTP (`myapp/views_chat.py`)

All `@login_required`; a task the user can't open at all → **404** (no
existence oracle), a task they can open but whose chat they can't read/write →
**403**. JSON bodies or form fields; send `X-Requested-With: XMLHttpRequest`
(or `Accept: application/json`) to get JSON instead of a redirect.

| endpoint | body / query | answer |
|---|---|---|
| `GET /myapp/tasks/<pk>/chat/` | — | the chat page (`templates/myapp/chat.html`, `static/js/chat.js`) |
| `GET /myapp/tasks/<pk>/chat/messages/` | `?before=<id>` older page, `?after=<id>` newer (polling fallback while WS is down — every ~10 s), `?limit=` ≤ 50 (default 30) | `{ok, task_id, messages: [chat.message payloads, oldest first], has_more, cursor, latest_id, my_last_read_id, receipts: [{role, user_display, last_id}], can_write, archived}` |
| `POST /myapp/tasks/<pk>/chat/send/` | `text`, `client_msg_id` | `{ok, code, message}` — 400 `empty`/`too_long`/`invalid`, 403 `forbidden`, 429 `rate_limited` |
| `POST /myapp/tasks/<pk>/chat/upload/` | multipart `image` (JPEG/PNG/WebP, ≤ 5 MB; HEIC only if a Pillow HEIF plugin is installed), `text` caption, `client_msg_id` | `{ok, code, message}` — 413 `too_large`, 400 `invalid_image`/`empty`, 415 `unsupported_format`; WebP `thumb` (320 px) + `medium` (960 px) follow via `chat.message_updated` |
| `POST /myapp/tasks/<pk>/chat/read/` | `last_id` | `{ok, last_id}` (null = unchanged) |
| `POST /myapp/chat/messages/<id>/report/` | `reason` | `{ok, code}` — `ok` / `already_reported`; 400 `own_message` / `invalid` (system message) |
| `POST /myapp/chat/messages/<id>/hide/` | `action=hide\|unhide` (staff) | `{ok, code, message}`; 403 for parties |

Related HTTP endpoints: `GET /myapp/notifications/bell/` (unread count + latest 10,
same item shape), `POST /myapp/notifications/<id>/read/`,
`POST /myapp/notifications/read-all/` (both publish `notification.read`),
`POST /profile/language/` (`language=ru|tj|en`, stores `Profile.language`).
