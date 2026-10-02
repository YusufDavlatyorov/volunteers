# KhayrKhoh WebSocket protocol (v1)

One endpoint: **`/ws/`** (served by the `ws` compose service — uvicorn running
`server/asgi.py`; nginx routes `/ws/` there with `Upgrade`/`Connection` headers).
Code: `myapp/realtime/` (`consumers.py`, `events.py`, `groups.py`, `ratelimit.py`,
`publish.py`). Tests: `myapp/tests_realtime.py`.

## Connecting

* Auth = the normal Django session cookie (`AuthMiddlewareStack`). The `Origin`
  header must match `ALLOWED_HOSTS` (`AllowedHostsOriginValidator`), otherwise the
  handshake is rejected (HTTP 403).
* Anonymous / inactive user → the socket is accepted and immediately closed with
  **4401**. Clients must not reconnect on 4401 (log in first).
* On success the server sends `hello` and joins the socket to:
  * `user_<id>` — always;
  * `staff` and `map_staff` — curators and admins only.
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
| `location.update`, `tracking.stop` | — | milestone 2 | `error unsupported` for now |
| `chat.send`, `chat.typing`, `chat.read` | — | milestone 3 | `error unsupported` for now |

A socket can hold at most 20 task subscriptions (`error too_many_subscriptions`).
"Not found" and "not allowed" both answer `forbidden` (no existence oracle).
After a reconnect the client must re-send its `subscribe` frames.

## Server → client

| type | payload | group |
|---|---|---|
| `hello` | `{user_id, role}` | this socket |
| `pong` | `{}` | this socket |
| `error` | `{code, ref}` | this socket |
| `subscribed` / `unsubscribed` | `{topic, id}` | this socket |
| `notification.new` | `{id, type, title_key, body_key, params, link, read, created_at}` | `user_<id>` |
| `notification.read` | `{ids: [...], unread}` or `{all: true, unread: 0}` | `user_<id>` |
| `task.updated` | `{task_id, status, work_stage}` | `task_<id>` |
| `availability.changed` | `{user_id, status}` | `staff` |
| `crm.counters`, `map.marker`, `location.updated`, `tracking.stopped`, `chat.*` | later milestones | `staff` / `map_staff` / `task_<id>` |

`notification.new`: render `title_key` / `body_key` through `static/js/i18n.js`
with `params` as the `{placeholder}` arguments (same as `data-i18n-args`). `link`
is an internal relative URL or `""`.

**No payload ever contains a phone number or an email address.** Builders live
only in `myapp/realtime/events.py`; notification `params` are sanitised by
`services/notify.py::safe_params`. Contact details exist only in the
server-rendered email/Telegram text sent to the addressed party.

## Error codes

`bad_json`, `bad_request` (shape / binary frame), `unknown_type`, `unsupported`
(known type, later milestone), `forbidden`, `rate_limited`, `too_many_subscriptions`.

## Close codes

| code | meaning | client action |
|---|---|---|
| 4401 | not authenticated | stop; reconnect only after login |
| 4408 | message > 4 KB, or sustained flooding | back off (≥ 30 s) before reconnecting |
| 1000/1001/1006 | normal / going away / network | reconnect with backoff 1 s → 30 s + jitter |

## Rate limits (per user, shared cache — Redis in production)

| bucket | limit | on excess |
|---|---|---|
| all inbound (default) | 60 / min | `error rate_limited`; > 2× in the window → close 4408 |
| `location.update` | 1 / 5 s | `error rate_limited` (dropped) |
| `chat.send` | 20 / min per task | `error rate_limited` |
| `chat.typing` | 1 / 2 s | `error rate_limited` (dropped) |

Frames larger than 4 KB close the socket with 4408 (uvicorn also caps frames at
16 KB via `--ws-max-size`).

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

Related HTTP endpoints: `GET /myapp/notifications/bell/` (unread count + latest 10,
same item shape), `POST /myapp/notifications/<id>/read/`,
`POST /myapp/notifications/read-all/` (both publish `notification.read`),
`POST /profile/language/` (`language=ru|tj|en`, stores `Profile.language`).
