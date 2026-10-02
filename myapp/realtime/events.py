"""Outbound WebSocket event builders — the ONLY place payload shapes are made.

Every builder whitelists its fields explicitly; none ever includes a phone
number or an email address (``myapp/tests_realtime.py`` inspects each shape).
Protocol reference: ``docs/WS_PROTOCOL.md``.
"""

from django.utils import timezone

# server -> client event types
HELLO = "hello"
PONG = "pong"
ERROR = "error"
SUBSCRIBED = "subscribed"
UNSUBSCRIBED = "unsubscribed"
NOTIFICATION_NEW = "notification.new"
NOTIFICATION_READ = "notification.read"
TASK_UPDATED = "task.updated"
AVAILABILITY_CHANGED = "availability.changed"

# error codes
ERR_BAD_JSON = "bad_json"
ERR_BAD_REQUEST = "bad_request"
ERR_UNKNOWN_TYPE = "unknown_type"
ERR_FORBIDDEN = "forbidden"
ERR_RATE_LIMITED = "rate_limited"
ERR_TOO_MANY_SUBSCRIPTIONS = "too_many_subscriptions"
ERR_UNSUPPORTED = "unsupported"

# close codes
CLOSE_UNAUTHENTICATED = 4401
CLOSE_POLICY = 4408  # oversized message / sustained abuse


def envelope(event_type, payload, ref=None):
    env = {"type": event_type, "payload": payload, "ts": timezone.now().isoformat()}
    if ref is not None:
        env["id"] = ref
    return env


def hello(user):
    return envelope(HELLO, {"user_id": user.pk, "role": user.role})


def pong(ref=None):
    return envelope(PONG, {}, ref)


def error(code, ref=None):
    return envelope(ERROR, {"code": code, "ref": ref}, ref)


def subscribed(topic, obj_id, ref=None):
    return envelope(SUBSCRIBED, {"topic": topic, "id": obj_id}, ref)


def unsubscribed(topic, obj_id, ref=None):
    return envelope(UNSUBSCRIBED, {"topic": topic, "id": obj_id}, ref)


def notification_payload(n):
    """Shape shared by ``notification.new``, the bell JSON and the poll
    endpoint. ``params`` were sanitised by ``services.notify.safe_params``."""
    return {
        "id": n.pk,
        "type": n.type,
        "title_key": n.title_key,
        "body_key": n.body_key,
        "params": n.params or {},
        "link": n.link or "",
        "read": n.read_at is not None,
        "created_at": n.created_at.isoformat() if n.created_at else None,
    }


def notification_read_payload(*, ids=None, all=False, unread=None):
    payload = {"all": True} if all else {"ids": list(ids or [])}
    if unread is not None:
        payload["unread"] = unread
    return payload


def task_updated_payload(task):
    return {"task_id": task.pk, "status": task.status, "work_stage": task.work_stage}


def availability_changed_payload(user_id, status):
    return {"user_id": user_id, "status": status}
