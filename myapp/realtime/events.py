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
SOS_MARKER = "sos.marker"
CHAT_MESSAGE = "chat.message"
CHAT_MESSAGE_UPDATED = "chat.message_updated"
CHAT_TYPING = "chat.typing"
CHAT_READ = "chat.read"
CHAT_HIDDEN = "chat.hidden"
CHAT_EVENTS = frozenset({CHAT_MESSAGE, CHAT_MESSAGE_UPDATED, CHAT_TYPING, CHAT_READ, CHAT_HIDDEN})

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
# chat.send rejections — services/chat.py Outcome codes, relayed verbatim.
ERR_EMPTY = "empty"
ERR_TOO_LONG = "too_long"
CHAT_ERROR_CODES = frozenset({ERR_FORBIDDEN, ERR_EMPTY, ERR_TOO_LONG, ERR_RATE_LIMITED, ERR_INVALID})

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


def ack(for_type, task_id, code="ok", ref=None, **extra):
    """Positive reply to an inbound command that has no other natural answer
    (``location.update``, ``tracking.stop``, ``chat.send`` / ``chat.read`` —
    the chat ones add ``client_msg_id`` + ``id`` / ``last_id`` via ``extra``)."""
    return envelope(ACK, {"for": for_type, "task_id": task_id, "code": code, **extra}, ref)


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


def sos_marker_payload(report):
    """``sos.marker`` on ``staff`` + ``volunteers_<region>``: an active SOS
    and where it is. Ids, region, status and the point only — no names, no
    contacts, no reason text. Sent only while the report is open/acknowledged."""
    return {
        "id": report.pk,
        "region": report.region or "",
        "status": report.status,
        "lat": _num(report.latitude),
        "lng": _num(report.longitude),
        "created_at": _iso(report.created_at),
    }


def sos_marker_removed_payload(report):
    """``sos.marker`` removal (resolved / cancelled) — no coordinates."""
    return {"id": report.pk, "region": report.region or "", "removed": True}


# --------------------------------------------------------------------------- #
# chat (milestone 3)
#
# Chat payloads are per viewer (``mine``; a staff post shows "Куратор" to the
# parties but the curator's real name to staff). The server therefore
# publishes ONE internal payload per event and every socket personalises it
# with ``for_viewer`` right before sending (consumers.DELIVERY_TRANSFORMS);
# HTTP responses go through the very same function (services/chat.py
# ``message_payload``). Internal keys start with "_" and never leave the
# server:
#
#   _sender_id   author / reader / typist id → ``mine`` (then dropped)
#   _staff       overrides applied only for staff viewers (real name, the
#                body of a hidden message, image urls)
#   _skip_self   drop the event for the author's own sockets (typing)
#   _staff_only  drop the event for non-staff sockets (a curator's read receipt)
# --------------------------------------------------------------------------- #

CHAT_STAFF_LABEL_KEY = "chat.staff_label"
CHAT_SYSTEM_LABEL_KEY = "chat.system_label"


def chat_message_payload(*, task_id, message_id, kind, body, created_at, is_staff_post, hidden,
                         sender_display, sender_role, label_key, thumb_url, image_url):
    """The public (party, non-author) view of one chat message."""
    return {
        "task_id": task_id,
        "id": message_id,
        "kind": kind,
        "body": body,
        "sender_display": sender_display,
        "sender_role": sender_role,     # client | volunteer | staff | system | member
        "label_key": label_key,         # chat.staff_label / chat.system_label / None
        "is_staff_post": is_staff_post,
        "hidden": hidden,
        "thumb_url": thumb_url,
        "image_url": image_url,
        "created_at": _iso(created_at),
        "mine": False,
    }


def chat_internal(public, *, sender_id=None, staff=None, skip_self=False, staff_only=False):
    """Wrap a public payload with the private personalisation keys."""
    payload = dict(public)
    payload["_sender_id"] = sender_id
    if staff:
        payload["_staff"] = dict(staff)
    if skip_self:
        payload["_skip_self"] = True
    if staff_only:
        payload["_staff_only"] = True
    return payload


def for_viewer(payload, viewer_id, viewer_is_staff):
    """Personalise an internal chat payload for one viewer; None = don't send.
    Strips every "_" key, so the result is safe to serialise."""
    sender_id = payload.get("_sender_id")
    mine = sender_id is not None and viewer_id is not None and sender_id == viewer_id
    if payload.get("_skip_self") and mine:
        return None
    if payload.get("_staff_only") and not viewer_is_staff:
        return None
    out = {k: v for k, v in payload.items() if not str(k).startswith("_")}
    if "_sender_id" in payload and not payload.get("_skip_self"):
        out["mine"] = mine  # (always False where the author's sockets are skipped)
    if viewer_is_staff and isinstance(payload.get("_staff"), dict):
        out.update(payload["_staff"])
    return out


def chat_typing_payload(task_id, sender_display):
    return {"task_id": task_id, "sender_display": sender_display}


def chat_read_payload(task_id, user_display, last_id):
    return {"task_id": task_id, "user_display": user_display, "last_id": last_id}


def chat_hidden_payload(task_id, message_id, hidden):
    return {"task_id": task_id, "id": message_id, "hidden": bool(hidden)}
