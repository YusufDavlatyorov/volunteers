"""The one WebSocket endpoint, ``/ws/`` — ``RealtimeConsumer``.

Protocol: ``docs/WS_PROTOCOL.md``. Rules:

* anonymous (or inactive) sockets are accepted then closed with 4401, so the
  browser sees the code and stops reconnecting with the same session;
* on connect: join ``user_<id>``; curators/admins also ``staff`` + ``map_staff``;
  send ``hello``; mark the user seen (presence);
* ``subscribe {topic: "task", id}`` joins ``task_<id>`` only after
  ``services.access.can_view_task`` — the same rule as the task detail page;
* every inbound message: ≤ 4 KB (else close 4408), rate-limited per user
  (``ratelimit.py``), unknown types → ``error {code}``;
* outbound shapes come only from ``events.py`` (no phone/email ever).
"""

import json
import logging

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from . import events, ratelimit
from .groups import MAP_STAFF_GROUP, STAFF_GROUP, task_group, user_group

logger = logging.getLogger(__name__)

MAX_TASK_SUBSCRIPTIONS = 20
# Inbound types that later milestones implement (tracking, chat). Until then
# they are rate-limited like the rest and answered with error "unsupported".
FUTURE_TYPES = {"location.update", "tracking.stop", "chat.send", "chat.typing", "chat.read"}


# ----------------------------------------------------------------- db helpers

@database_sync_to_async
def _task_visible(user, task_id):
    from ..models import HelpRequest
    from ..services import access

    task = HelpRequest.objects.filter(pk=task_id).only(
        "id", "client_id", "volunteer_id", "status", "region"
    ).first()
    return task is not None and access.can_view_task(user, task)


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


# ------------------------------------------------------------------ consumer

class RealtimeConsumer(AsyncJsonWebsocketConsumer):
    user = None
    is_staff = False

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
        await self._join(user_group(user.pk))
        if self.is_staff:
            await self._join(STAFF_GROUP)
            await self._join(MAP_STAFF_GROUP)
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

    # ------------------------------------------------------------- inbound

    async def receive(self, text_data=None, bytes_data=None, **kwargs):
        if self.user is None:
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

    @staticmethod
    def _task_id(payload):
        if payload.get("topic") != "task":
            return None
        raw = payload.get("id")
        if isinstance(raw, bool):
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    HANDLERS = {
        "ping": on_ping,
        "presence.heartbeat": on_heartbeat,
        "subscribe": on_subscribe,
        "unsubscribe": on_unsubscribe,
        "notification.read": on_notification_read,
    }

    # ------------------------------------------------------------ outbound

    async def realtime_event(self, message):
        """Channel-layer handler for ``{"type": "realtime.event", "event": ...}``
        (sent by ``publish.py`` and by this consumer)."""
        event = message.get("event")
        if isinstance(event, dict):
            await self.send_json(event)
