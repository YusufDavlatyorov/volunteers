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
ACK = "ack"
LOCATION_UPDATED = "location.updated"
TRACKING_STOPPED = "tracking.stopped"
MAP_MARKER = "map.marker"

# error codes
ERR_BAD_JSON = "bad_json"
ERR_BAD_REQUEST = "bad_request"
ERR_UNKNOWN_TYPE = "unknown_type"
ERR_FORBIDDEN = "forbidden"
ERR_RATE_LIMITED = "rate_limited"
ERR_TOO_MANY_SUBSCRIPTIONS = "too_many_subscriptions"
ERR_UNSUPPORTED = "unsupported"
ERR_SERVER = "server_error"
# location.update rejections — the tracking service's PointOutcome codes,
# relayed verbatim (``forbidden`` above is shared).
ERR_INVALID = "invalid"
ERR_INACCURATE = "inaccurate"
ERR_JUMP = "jump"
ERR_TOO_SOON = "too_soon"
TRACKING_ERROR_CODES = frozenset({ERR_FORBIDDEN, ERR_INVALID, ERR_INACCURATE, ERR_JUMP, ERR_TOO_SOON})

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


def ack(for_type, task_id, code="ok", ref=None):
    """Positive reply to an inbound command that has no other natural answer
    (``location.update``, ``tracking.stop``; chat later)."""
    return envelope(ACK, {"for": for_type, "task_id": task_id, "code": code}, ref)


def subscribed(topic, obj_id, ref=None):
    return envelope(SUBSCRIBED, {"topic": topic, "id": obj_id}, ref)


def unsubscribed(topic, obj_id, ref=None, reason=None):
    payload = {"topic": topic, "id": obj_id}
    if reason:
        payload["reason"] = reason  # "forbidden": the server dropped the subscription
    return envelope(UNSUBSCRIBED, payload, ref)


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


def _iso(ts):
    return ts.isoformat() if hasattr(ts, "isoformat") else ts


def _num(value):
    return None if value is None else float(value)


def location_updated_payload(task_id, lat, lng, ts, *, accuracy=None, eta_min=None):
    """``location.updated`` on ``task_<id>``: the volunteer's latest point.
    Delivered per socket only while ``access.can_see_location`` holds."""
    payload = {"task_id": task_id, "lat": _num(lat), "lng": _num(lng), "ts": _iso(ts),
               "accuracy": _num(accuracy)}
    if eta_min is not None:
        payload["eta_min"] = eta_min
    return payload


def tracking_stopped_payload(task_id, reason):
    """``tracking.stopped`` on ``task_<id>``; reason ``volunteer`` | ``task_ended``."""
    return {"task_id": task_id, "reason": reason}


def volunteer_live_marker_payload(task_id, lat, lng, ts):
    """``map.marker`` on ``map_staff`` for a live volunteer position."""
    return {"kind": "volunteer_live", "task_id": task_id, "lat": _num(lat), "lng": _num(lng), "ts": _iso(ts)}


def volunteer_live_marker_removed_payload(task_id):
    """``map.marker`` removal (tracking stopped / task ended) — no coordinates."""
    return {"kind": "volunteer_live", "task_id": task_id, "removed": True}
