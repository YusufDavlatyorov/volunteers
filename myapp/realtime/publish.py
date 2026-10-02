"""Publish realtime events from sync code (views, services, Celery tasks).

Every helper is fire-and-forget: a missing or broken channel layer (Redis down,
no CHANNEL_LAYERS) logs one WARNING and returns False — it never raises into the
request, the same philosophy as myapp.notifications._dispatch. Payloads must
already be safe to show to every member of the target group (no phone/email).
"""

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from .events import envelope
from .groups import MAP_STAFF_GROUP, STAFF_GROUP, task_group, user_group

logger = logging.getLogger(__name__)

__all__ = [
    "STAFF_GROUP", "MAP_STAFF_GROUP", "user_group", "task_group", "envelope",
    "send_to_group", "send_to_user", "send_to_task", "send_to_staff", "send_to_map_staff",
    "task_changed",
]


def send_to_group(group, event_type, payload):
    try:
        layer = get_channel_layer()
        if layer is None:
            return False
        # "realtime.event" → RealtimeConsumer.realtime_event forwards the envelope.
        async_to_sync(layer.group_send)(
            group, {"type": "realtime.event", "event": envelope(event_type, payload)}
        )
        return True
    except Exception:  # noqa: BLE001 — a realtime hiccup must never break the caller
        logger.warning("realtime publish failed type=%s group=%s", event_type, group.split("_")[0])
        return False


def send_to_user(user_or_id, event_type, payload):
    user_id = getattr(user_or_id, "pk", user_or_id)
    return send_to_group(user_group(user_id), event_type, payload)


def send_to_task(task_or_id, event_type, payload):
    task_id = getattr(task_or_id, "pk", task_or_id)
    return send_to_group(task_group(task_id), event_type, payload)


def send_to_staff(event_type, payload):
    return send_to_group(STAFF_GROUP, event_type, payload)


def send_to_map_staff(event_type, payload):
    return send_to_group(MAP_STAFF_GROUP, event_type, payload)


def task_changed(task):
    """A task's status/work_stage moved: tell its subscribers (parties + staff
    viewing it) and the staff CRM. Payload: ids and codes only."""
    from .events import TASK_UPDATED, task_updated_payload

    payload = task_updated_payload(task)
    send_to_task(task, TASK_UPDATED, payload)
    send_to_staff(TASK_UPDATED, payload)
