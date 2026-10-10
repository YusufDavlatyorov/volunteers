"""The one WebSocket endpoint, ``/ws/`` — ``RealtimeConsumer``.

Protocol: ``docs/WS_PROTOCOL.md``. Rules:

* anonymous (or inactive) sockets are accepted then closed with 4401, so the
  browser sees the code and stops reconnecting with the same session;
* on connect: join ``user_<id>``; curators/admins also ``staff`` + ``map_staff``;
  volunteers with a region also ``volunteers_<region>`` (SOS markers nearby);
  send ``hello``; mark the user seen (presence);
* ``subscribe {topic: "task", id}`` joins ``task_<id>`` only after
  ``services.access.can_view_task`` — the same rule as the task detail page;
* every inbound message: ≤ 4 KB (else close 4408), rate-limited per user
  (``ratelimit.py``), unknown types → ``error {code}``;
* outbound shapes come only from ``events.py`` (no phone/email ever);
* **delivery guards** (milestone 2): sensitive group events are re-checked per
  socket right before they are sent (``DELIVERY_GUARDS``) — membership of a
  group is not enough, because a task's parties change (accept / complete /
  cancel) while sockets stay subscribed. Decisions are cached per socket per
  task (at most ``GUARD_TTL_SECONDS``) and dropped on every ``task.updated``
  for that task;
* **chat** (milestone 3): ``chat.send`` / ``chat.typing`` / ``chat.read`` call
  ``services/chat.py`` (which re-checks ``access.can_write_chat`` /
  ``can_read_chat`` on a fresh task row); every outbound ``chat.*`` event is
  guarded by ``can_read_chat`` and then personalised per socket
  (``DELIVERY_TRANSFORMS`` → ``events.for_viewer``: ``mine``, a curator's
  real name only for staff sockets, hidden bodies only for staff);
* the socket's user is re-read from the DB at most every
  ``USER_RECHECK_SECONDS`` (checked on every inbound frame — the 25 s
  heartbeat included — and every outbound event): deactivated → close 4401; lost staff role → leave staff groups.
"""

import json
import logging
import time

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from . import events, ratelimit
from .groups import MAP_STAFF_GROUP, STAFF_GROUP, region_volunteers_group, task_group, user_group

logger = logging.getLogger(__name__)

MAX_TASK_SUBSCRIPTIONS = 20
# Inbound types a later milestone implements: rate-limited like the rest and
# answered with error "unsupported" until then.
FUTURE_TYPES = set()
# How stale the socket's copy of the user may get (deactivation, role change).
USER_RECHECK_SECONDS = 60
# Per-socket guard decisions kept at most (staff see markers of many tasks).
MAX_GUARD_CACHE = 256
# A cached guard decision is re-evaluated after this long even without a
# task.updated (e.g. a chat that became archived, a socket not subscribed).
GUARD_TTL_SECONDS = 120
_TASK_FIELDS = ("id", "client_id", "volunteer_id", "status", "region",
                "completed_at", "cancelled_at", "updated_at")


def _positive_int(raw):
    if isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _number(raw):
    """A JSON number (not a bool / string) as float, else None."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    return value if value == value and value not in (float("inf"), float("-inf")) else None


def _identity(user):
    """What a guard decision depends on, on the user's side."""
    return (user.is_active, user.is_superuser, user.is_curator, user.is_volunteer,
            user.is_client, user.region)


# ----------------------------------------------------------------- db helpers

def _load_task(task_id):
    from ..models import HelpRequest

    return HelpRequest.objects.filter(pk=task_id).only(*_TASK_FIELDS).first()


@database_sync_to_async
def _task_visible(user, task_id):
    from ..services import access

    task = _load_task(task_id)
    return task is not None and access.can_view_task(user, task)


@database_sync_to_async
def _task_check(user, task_id, predicate):
    """``predicate(user, task)`` on a fresh row; a missing task is a "no"."""
    task = _load_task(task_id)
    return task is not None and bool(predicate(user, task))


@database_sync_to_async
def _reload_user(user_id):
    from accounts.models import Users

    return Users.objects.filter(pk=user_id).first()


@database_sync_to_async
def _mark_seen(user):
    from ..services import presence

    try:
        presence.mark_seen(user, source="ws")
    except Exception:  # noqa: BLE001 — presence must never break the socket
        logger.warning("presence.mark_seen failed for user #%s", user.pk)


@database_sync_to_async
def _recompute_presence(user):
    from ..services import presence

    try:
        presence.recompute(user)
    except Exception:  # noqa: BLE001
        logger.warning("presence.recompute failed for user #%s", user.pk)


@database_sync_to_async
def _mark_read(user, ids, all_):
    from ..services import notify

    return notify.mark_read(user, ids=ids, all=all_, publish_event=False)


def _tracking():
    """The tracking service (geo-engineer), imported lazily: the consumer must
    load even where it is missing (``error unsupported`` then)."""
    from ..services import tracking

    return tracking


@database_sync_to_async
def _accept_point(user, task_id, lat, lng, accuracy, speed, heading):
    return _tracking().accept_point(
        user, task_id, lat, lng, accuracy=accuracy, speed=speed, heading=heading, source="ws",
    )


def _chat():
    from ..services import chat

    return chat


def _load_chat_task(task_id):
    from ..models import HelpRequest

    return HelpRequest.objects.filter(pk=task_id).first()


@database_sync_to_async
def _chat_send(user, task_id, text, client_msg_id):
    """→ (code, message_id). A missing task is ``forbidden`` (no oracle)."""
    task = _load_chat_task(task_id)
    if task is None:
        return events.ERR_FORBIDDEN, None
    outcome = _chat().send_message(user, task, text=text, client_msg_id=client_msg_id)
    if not outcome.ok:
        return outcome.code, None
    return "ok", outcome.message.pk


@database_sync_to_async
def _chat_typing(user, task_id):
    """→ the internal ``chat.typing`` payload, or None when the user may not
    write in this chat (typing is only meaningful where one can write)."""
    from ..services import access

    task = _load_chat_task(task_id)
    if task is None or not access.can_write_chat(user, task):
        return None
    chat = _chat()
    staff = access.is_staff_user(user)
    real = chat.display_name(user)
    public = events.chat_typing_payload(task.pk, chat.STAFF_LABEL if staff else real)
    return events.chat_internal(public, sender_id=user.pk, staff={"sender_display": real}, skip_self=True)


@database_sync_to_async
def _chat_read(user, task_id, last_id):
    """→ (ok, cursor). ``mark_read`` publishes ``chat.read`` itself."""
    from ..services import access

    task = _load_chat_task(task_id)
    if task is None or not access.can_read_chat(user, task):
        return False, None
    return True, _chat().mark_read(user, task, last_id)


@database_sync_to_async
def _stop_tracking(user, task_id):
    """Only the task's assigned volunteer may stop sharing. Returns an error
    code, or None on success."""
    task = _load_task(task_id)
    if task is None or task.volunteer_id != user.pk:
        return events.ERR_FORBIDDEN
    result = _tracking().stop(user, task, reason="volunteer")
    # tracking.stop returns a bool (False: not the assigned volunteer of an
    # active task); tolerate an outcome object too.
    if result is False or getattr(result, "ok", True) is False:
        return getattr(result, "code", None) or events.ERR_FORBIDDEN
    return None


# -------------------------------------------------------------- permissions

def can_see_location(user, task):
    from ..services import access

    return access.can_see_location(user, task)


def was_location_party(user, task):
    """Audience of ``tracking.stopped``: the people who could see the live
    location — staff, the client, the assigned volunteer — WITHOUT the
    "task is active" condition, so "tracking ended because the task ended"
    still reaches the client. The event carries no coordinates."""
    from ..services import access

    if not user.is_active or not task.volunteer_id:
        return False
    return access.is_staff_user(user) or user.id in (task.client_id, task.volunteer_id)


class TaskGuard:
    """Deliver an event only if ``predicate(user, task)`` holds for the task
    named by ``payload.task_id``. Decisions are cached per socket under
    ``(name, task_id)`` and dropped on ``task.updated`` for that task.

    Milestone 3 plugs chat in with one line:
    ``DELIVERY_GUARDS["chat.message"] = TaskGuard("chat", can_read_chat)``.
    """

    def __init__(self, name, predicate, staff_only=False):
        self.name = name
        self.predicate = predicate
        self.staff_only = staff_only

    async def __call__(self, consumer, payload):
        if self.staff_only and not consumer.is_staff:
            return False
        task_id = _positive_int(payload.get("task_id"))
        if task_id is None:
            return False
        return await consumer.task_permission(self.name, task_id, self.predicate)


class StaffGuard:
    """``map_staff`` / ``staff`` events: the socket's user must still be
    active staff (re-read every USER_RECHECK_SECONDS). A marker that names a
    task (live volunteer position) additionally needs ``can_see_location`` —
    except a removal (``removed: true``, no coordinates), which must reach
    staff precisely when the task stopped being live."""

    def __init__(self, task_guard=None):
        self.task_guard = task_guard

    async def __call__(self, consumer, payload):
        if not consumer.is_staff:
            return False
        if payload.get("removed") is True:
            return True
        if self.task_guard is not None and payload.get("task_id") is not None:
            return await self.task_guard(consumer, payload)
        return True


def can_read_chat(user, task):
    from ..services import access

    return access.can_read_chat(user, task)


class SosGuard:
    """``sos.marker``: staff see every SOS; a volunteer only those of their
    own (current) region — checked on the socket's re-read user, so a role
    or region change stops delivery even before the groups are updated."""

    async def __call__(self, consumer, payload):
        if consumer.is_staff:
            return True
        user = consumer.user
        region = payload.get("region")
        return bool(user.is_active and user.is_volunteer and region and user.region == region)


LOCATION_GUARD = TaskGuard("location", can_see_location)
CHAT_GUARD = TaskGuard("chat", can_read_chat)

# event type -> async guard(consumer, payload) -> bool. Unlisted types pass.
DELIVERY_GUARDS = {
    events.LOCATION_UPDATED: LOCATION_GUARD,
    events.TRACKING_STOPPED: TaskGuard("location_party", was_location_party),
    events.MAP_MARKER: StaffGuard(task_guard=LOCATION_GUARD),
    events.SOS_MARKER: SosGuard(),
    **{event_type: CHAT_GUARD for event_type in events.CHAT_EVENTS},
}


def _personalise_chat(consumer, payload):
    return events.for_viewer(payload, consumer.user.pk, consumer.is_staff)


# event type -> transform(consumer, payload) -> payload | None (None = drop).
# Runs after the guard; per-viewer shapes (chat) are made here.
DELIVERY_TRANSFORMS = {event_type: _personalise_chat for event_type in events.CHAT_EVENTS}


# ------------------------------------------------------------------ consumer

class RealtimeConsumer(AsyncJsonWebsocketConsumer):
    user = None
    is_staff = False
    closing = False

    async def connect(self):
        user = self.scope.get("user")
        await self.accept()
        if user is None or not user.is_authenticated or not user.is_active:
            await self.close(code=events.CLOSE_UNAUTHENTICATED)
            return
        self.user = user
        self.is_staff = bool(user.is_superuser or user.is_curator)
        self.groups_joined = set()
        self.task_ids = set()
        self.guard_cache = {}
        self.user_checked_at = time.monotonic()
        await self._join(user_group(user.pk))
        if self.is_staff:
            await self._join(STAFF_GROUP)
            await self._join(MAP_STAFF_GROUP)
        self.region_group = None
        await self._sync_region_group(user)
        await self.send_json(events.hello(user))
        await _mark_seen(user)

    async def disconnect(self, code):
        for group in getattr(self, "groups_joined", ()):
            try:
                await self.channel_layer.group_discard(group, self.channel_name)
            except Exception:  # noqa: BLE001
                pass
        if self.user is not None:
            await _recompute_presence(self.user)

    async def _join(self, group):
        await self.channel_layer.group_add(group, self.channel_name)
        self.groups_joined.add(group)

    async def _leave(self, group):
        await self.channel_layer.group_discard(group, self.channel_name)
        self.groups_joined.discard(group)

    async def _sync_region_group(self, user):
        """Volunteers sit in ``volunteers_<region>`` of their current region
        (SOS markers); anyone else in none."""
        wanted = region_volunteers_group(user.region) if user.is_volunteer else None
        current = getattr(self, "region_group", None)
        if wanted == current:
            return
        if current:
            await self._leave(current)
        if wanted:
            await self._join(wanted)
        self.region_group = wanted

    # ------------------------------------------------------- user re-check

    async def ensure_user_fresh(self, force=False):
        """Re-read the user at most every USER_RECHECK_SECONDS. Returns False
        (and closes 4401) when the account is gone or deactivated."""
        if self.closing or self.user is None:
            return False
        now = time.monotonic()
        if not force and now - self.user_checked_at < USER_RECHECK_SECONDS:
            return True
        self.user_checked_at = now
        fresh = await _reload_user(self.user.pk)
        if fresh is None or not fresh.is_active:
            logger.info("ws: user #%s deactivated, closing", self.user.pk)
            self.closing = True
            self.guard_cache = {}
            await self.close(code=events.CLOSE_UNAUTHENTICATED)
            return False
        if _identity(fresh) != _identity(self.user):
            self.guard_cache = {}  # role / region changed: every decision is stale
        self.user = fresh
        is_staff = bool(fresh.is_superuser or fresh.is_curator)
        if self.is_staff and not is_staff:
            await self._leave(STAFF_GROUP)
            await self._leave(MAP_STAFF_GROUP)
        elif is_staff and not self.is_staff:
            await self._join(STAFF_GROUP)
            await self._join(MAP_STAFF_GROUP)
        self.is_staff = is_staff
        await self._sync_region_group(fresh)
        return True

    # ----------------------------------------------------------- guards

    async def task_permission(self, name, task_id, predicate):
        key = (name, task_id)
        now = time.monotonic()
        cached = self.guard_cache.get(key)
        if cached is None or now - cached[1] > GUARD_TTL_SECONDS:
            if len(self.guard_cache) >= MAX_GUARD_CACHE:
                self.guard_cache = {}
            cached = (await _task_check(self.user, task_id, predicate), now)
            self.guard_cache[key] = cached
        return cached[0]

    def invalidate_task(self, task_id):
        self.guard_cache = {k: v for k, v in self.guard_cache.items() if k[1] != task_id}

    async def _recheck_subscription(self, task_id):
        """After a task changed: drop the subscription if the user can no
        longer open the task (e.g. a pending task another volunteer took)."""
        if task_id not in self.task_ids:
            return
        if await _task_visible(self.user, task_id):
            return
        await self._leave(task_group(task_id))
        self.task_ids.discard(task_id)
        await self.send_json(events.unsubscribed("task", task_id, reason=events.ERR_FORBIDDEN))

    # ------------------------------------------------------------- inbound

    async def receive(self, text_data=None, bytes_data=None, **kwargs):
        if self.user is None or self.closing:
            return
        if bytes_data is not None:
            if len(bytes_data) > ratelimit.MAX_MESSAGE_BYTES:
                await self.close(code=events.CLOSE_POLICY)
                return
            await self.send_json(events.error(events.ERR_BAD_REQUEST))
            return
        if text_data is None:
            return
        if len(text_data.encode("utf-8")) > ratelimit.MAX_MESSAGE_BYTES:
            logger.info("ws: oversized message from user #%s, closing", self.user.pk)
            await self.close(code=events.CLOSE_POLICY)
            return
        try:
            content = json.loads(text_data)
        except ValueError:
            await self.send_json(events.error(events.ERR_BAD_JSON))
            return
        if not isinstance(content, dict) or not isinstance(content.get("type"), str):
            await self.send_json(events.error(events.ERR_BAD_REQUEST))
            return
        await self.receive_json(content)

    async def receive_json(self, content, **kwargs):
        msg_type = content["type"]
        ref = content.get("id")
        if not isinstance(ref, (str, int)) or isinstance(ref, bool):
            ref = None
        elif isinstance(ref, str):
            ref = ref[:64]
        payload = content.get("payload") or {}
        if not isinstance(payload, dict):
            await self.send_json(events.error(events.ERR_BAD_REQUEST, ref))
            return

        bucket = ratelimit.bucket_for(msg_type)
        scope = str(payload.get("task_id", "")) if msg_type == "chat.send" else ""
        allowed, count, limit = await ratelimit.hit(self.user.pk, msg_type, scope)
        if not allowed:
            if bucket == "default" and ratelimit.is_abuse(count, limit):
                logger.info("ws: rate abuse by user #%s, closing", self.user.pk)
                await self.close(code=events.CLOSE_POLICY)
                return
            await self.send_json(events.error(events.ERR_RATE_LIMITED, ref))
            return

        if not await self.ensure_user_fresh():
            return

        handler = self.HANDLERS.get(msg_type)
        if handler is None:
            code = events.ERR_UNSUPPORTED if msg_type in FUTURE_TYPES else events.ERR_UNKNOWN_TYPE
            await self.send_json(events.error(code, ref))
            return
        await handler(self, payload, ref)

    async def on_ping(self, payload, ref):
        await self.send_json(events.pong(ref))

    async def on_heartbeat(self, payload, ref):
        await _mark_seen(self.user)

    async def on_subscribe(self, payload, ref):
        task_id = self._task_id(payload)
        if task_id is None:
            await self.send_json(events.error(events.ERR_BAD_REQUEST, ref))
            return
        if task_id in self.task_ids:
            await self.send_json(events.subscribed("task", task_id, ref))
            return
        if len(self.task_ids) >= MAX_TASK_SUBSCRIPTIONS:
            await self.send_json(events.error(events.ERR_TOO_MANY_SUBSCRIPTIONS, ref))
            return
        # Not-found and not-allowed look the same: no existence oracle.
        if not await _task_visible(self.user, task_id):
            await self.send_json(events.error(events.ERR_FORBIDDEN, ref))
            return
        await self._join(task_group(task_id))
        self.task_ids.add(task_id)
        await self.send_json(events.subscribed("task", task_id, ref))

    async def on_unsubscribe(self, payload, ref):
        task_id = self._task_id(payload)
        if task_id is None:
            await self.send_json(events.error(events.ERR_BAD_REQUEST, ref))
            return
        if task_id in self.task_ids:
            await self._leave(task_group(task_id))
            self.task_ids.discard(task_id)
            self.invalidate_task(task_id)
        await self.send_json(events.unsubscribed("task", task_id, ref))

    async def on_notification_read(self, payload, ref):
        all_ = payload.get("all") is True
        ids = payload.get("ids")
        if not all_ and not isinstance(ids, list):
            await self.send_json(events.error(events.ERR_BAD_REQUEST, ref))
            return
        result = await _mark_read(self.user, ids or [], all_)
        # Fan out to every tab of this user (this one included).
        await self.channel_layer.group_send(
            user_group(self.user.pk),
            {"type": "realtime.event", "event": events.envelope(events.NOTIFICATION_READ, result)},
        )

    async def on_location_update(self, payload, ref):
        """``{task_id, lat, lng, accuracy, speed?, heading?}`` → tracking.accept_point.
        ok → ``ack``; a rejected point → ``error {code}`` with the service's code."""
        task_id = _positive_int(payload.get("task_id"))
        lat, lng = _number(payload.get("lat")), _number(payload.get("lng"))
        if task_id is None or lat is None or lng is None:
            await self.send_json(events.error(events.ERR_BAD_REQUEST, ref))
            return
        optional = {}
        for key in ("accuracy", "speed", "heading"):
            raw = payload.get(key)
            optional[key] = _number(raw)
            if raw is not None and optional[key] is None:
                await self.send_json(events.error(events.ERR_BAD_REQUEST, ref))
                return
        try:
            outcome = await _accept_point(self.user, task_id, lat, lng, optional["accuracy"],
                                          optional["speed"], optional["heading"])
        except ImportError:
            await self.send_json(events.error(events.ERR_UNSUPPORTED, ref))
            return
        except Exception:  # noqa: BLE001 — one bad point must not kill the socket
            logger.exception("ws: location.update failed user #%s task #%s", self.user.pk, task_id)
            await self.send_json(events.error(events.ERR_SERVER, ref))
            return
        code = getattr(outcome, "code", None)
        if getattr(outcome, "ok", False):
            await self.send_json(events.ack("location.update", task_id, "ok", ref))
            return
        if code not in events.TRACKING_ERROR_CODES:
            code = events.ERR_FORBIDDEN
        await self.send_json(events.error(code, ref))

    async def on_tracking_stop(self, payload, ref):
        """``{task_id}`` → tracking.stop, assigned volunteer only."""
        task_id = _positive_int(payload.get("task_id"))
        if task_id is None:
            await self.send_json(events.error(events.ERR_BAD_REQUEST, ref))
            return
        try:
            code = await _stop_tracking(self.user, task_id)
        except ImportError:
            await self.send_json(events.error(events.ERR_UNSUPPORTED, ref))
            return
        except Exception:  # noqa: BLE001
            logger.exception("ws: tracking.stop failed user #%s task #%s", self.user.pk, task_id)
            await self.send_json(events.error(events.ERR_SERVER, ref))
            return
        if code:
            await self.send_json(events.error(code, ref))
            return
        await self.send_json(events.ack("tracking.stop", task_id, "ok", ref))

    # ---------------------------------------------------------------- chat

    async def on_chat_send(self, payload, ref):
        """``{task_id, text, client_msg_id}`` → chat.send_message. Reply
        ``ack {for: "chat.send", task_id, code, client_msg_id, id}`` or
        ``error {code}``; the message itself arrives as ``chat.message`` on
        the task group (this socket included, ``mine: true``)."""
        task_id = _positive_int(payload.get("task_id"))
        text = payload.get("text")
        client_msg_id = payload.get("client_msg_id") or ""
        if task_id is None or not isinstance(text, str) or not isinstance(client_msg_id, str):
            await self.send_json(events.error(events.ERR_BAD_REQUEST, ref))
            return
        try:
            code, message_id = await _chat_send(self.user, task_id, text, client_msg_id)
        except Exception:  # noqa: BLE001 — one bad message must not kill the socket
            logger.exception("ws: chat.send failed user #%s task #%s", self.user.pk, task_id)
            await self.send_json(events.error(events.ERR_SERVER, ref))
            return
        if code != "ok":
            await self.send_json(events.error(code if code in events.CHAT_ERROR_CODES else events.ERR_FORBIDDEN, ref))
            return
        await self.send_json(events.ack("chat.send", task_id, "ok", ref,
                                        client_msg_id=client_msg_id, id=message_id))

    async def on_chat_typing(self, payload, ref):
        """``{task_id}`` → ``chat.typing {task_id, sender_display}`` to the
        task group, except this user's own sockets. No reply on success."""
        task_id = _positive_int(payload.get("task_id"))
        if task_id is None:
            await self.send_json(events.error(events.ERR_BAD_REQUEST, ref))
            return
        internal = await _chat_typing(self.user, task_id)
        if internal is None:
            await self.send_json(events.error(events.ERR_FORBIDDEN, ref))
            return
        await self.channel_layer.group_send(
            task_group(task_id),
            {"type": "realtime.event", "event": events.envelope(events.CHAT_TYPING, internal)},
        )

    async def on_chat_read(self, payload, ref):
        """``{task_id, last_id}`` → chat.mark_read (monotonic; publishes
        ``chat.read`` to the group on a change). Reply ``ack {last_id}``."""
        task_id = _positive_int(payload.get("task_id"))
        last_id = _positive_int(payload.get("last_id"))
        if task_id is None or last_id is None:
            await self.send_json(events.error(events.ERR_BAD_REQUEST, ref))
            return
        ok, cursor = await _chat_read(self.user, task_id, last_id)
        if not ok:
            await self.send_json(events.error(events.ERR_FORBIDDEN, ref))
            return
        await self.send_json(events.ack("chat.read", task_id, "ok", ref, last_id=cursor))

    @staticmethod
    def _task_id(payload):
        if payload.get("topic") != "task":
            return None
        return _positive_int(payload.get("id"))

    HANDLERS = {
        "ping": on_ping,
        "presence.heartbeat": on_heartbeat,
        "subscribe": on_subscribe,
        "unsubscribe": on_unsubscribe,
        "notification.read": on_notification_read,
        "location.update": on_location_update,
        "tracking.stop": on_tracking_stop,
        "chat.send": on_chat_send,
        "chat.typing": on_chat_typing,
        "chat.read": on_chat_read,
    }

    # ------------------------------------------------------------ outbound

    async def realtime_event(self, message):
        """Channel-layer handler for ``{"type": "realtime.event", "event": ...}``
        (sent by ``publish.py`` and by this consumer). Guarded event types are
        re-checked for THIS socket's user before they are sent."""
        event = message.get("event")
        if not isinstance(event, dict) or self.user is None or self.closing:
            return
        event_type = event.get("type")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if not await self.ensure_user_fresh():
            return
        guard = DELIVERY_GUARDS.get(event_type)
        if guard is not None and not await guard(self, payload):
            return
        transform = DELIVERY_TRANSFORMS.get(event_type)
        if transform is not None:
            shaped = transform(self, payload)
            if shaped is None:
                return
            event = {**event, "payload": shaped}
        await self.send_json(event)
        if event_type == events.TASK_UPDATED:
            task_id = _positive_int(payload.get("task_id"))
            if task_id is not None:
                self.invalidate_task(task_id)
                await self._recheck_subscription(task_id)
