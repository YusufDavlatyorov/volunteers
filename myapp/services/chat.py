"""Client <-> volunteer chat (Feature Pack v2, feature 8) — the one write path.

Permissions come only from ``services/access.py`` (``can_read_chat`` /
``can_write_chat``); every function re-checks them, whatever the caller did.
Transport-agnostic: the HTTP views (``myapp/views_chat.py``) and the WebSocket
consumer (``myapp/realtime/consumers.py``) both call in here.

Public API (orchestrator / other services):

* ``send_message(user, task, *, text=None, image=None, client_msg_id="") -> Outcome``
* ``history(user, task, *, before_id=None, after_id=None, limit=30) -> dict | None``
* ``mark_read(user, task, last_id) -> int | None``
* ``unread_counts(user, task_ids) -> {task_id: int}``  (one query)
* ``report(user, message, reason="") -> Outcome``
* ``hide(staff, message)`` / ``unhide(staff, message)`` ``-> Outcome``
* ``review(staff, message) -> Outcome`` — "Проверено": closes flags + open reports
* ``close(task) -> ChatMessage | None`` — system "chat closed" message after
  complete / cancel; idempotent; no-op for a task that never had a volunteer.
* ``message_payload(message, viewer) -> dict`` — the per-viewer shape.

Privacy: payloads carry display names only (full name or username), never a
phone number or an email; a staff post shows the label "Куратор"
(``chat.staff_label``) to non-staff viewers — the curator's identity is only
in the staff view. Hidden messages are a stub (no body / image) for everyone
but staff.

Notifications (``chat_message``, in-app + Telegram by default) go to the
other *parties* (client / assigned volunteer — staff are never pinged about
party messages). To avoid spamming an elderly client a recipient is skipped
when either

1. they are **in the chat right now**: their read cursor is at (or past) the
   previous message and was moved within ``ACTIVE_READ_SECONDS`` (2 min), or
2. they already hold an **unread chat notification** for this task from the
   last ``NOTIFY_THROTTLE_SECONDS`` (10 min): the cache remembers the message
   id that triggered it; once their cursor passes it they can be notified
   again, otherwise at most one reminder per 10 min.

The message text reaches email/Telegram only through ``notify(extra=...)``
(a 200-char preview), never the in-app row / WS params.

Safety (milestone 4): a party message is scanned by ``chat_flags.detect``
(money / SMS codes / other messengers / phone numbers); the codes go to
``ChatMessage.flags`` (staff payload only) and curators get one quiet in-app
``chat_flagged`` per task × category × day — the message is never blocked.
Photo originals are stored re-encoded without EXIF / GPS / XMP
(``images.strip_metadata``), like their WebP variants.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Count, F, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce
from django.urls import reverse
from django.utils import timezone

from ..models import (
    CHAT_KIND_IMAGE,
    CHAT_KIND_SYSTEM,
    CHAT_KIND_TEXT,
    CHAT_MESSAGE_MAX_LENGTH,
    ChatMessage,
    ChatReadState,
    ChatReport,
    HelpRequest,
)
from ..realtime import events, publish
from . import access, chat_flags, images

logger = logging.getLogger(__name__)

# Outcome codes
OK = "ok"
FORBIDDEN = "forbidden"
EMPTY = "empty"
TOO_LONG = "too_long"
RATE_LIMITED = "rate_limited"
INVALID = "invalid"
ALREADY = "already_reported"
OWN_MESSAGE = "own_message"
# image upload codes come from services/images.py: too_large / invalid_image /
# unsupported_format

STAFF_LABEL = "Куратор"
SYSTEM_CLOSED = "chat.system.closed"
SYSTEM_CANCELLED = "chat.system.cancelled"
SYSTEM_BODIES = (SYSTEM_CLOSED, SYSTEM_CANCELLED)

PAGE_SIZE = 30
MAX_PAGE_SIZE = 50
RATE_LIMIT = 20          # messages
RATE_WINDOW = 60         # seconds, per user per task
CLIENT_MSG_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
REPORT_REASON_MAX = 500
PREVIEW_CHARS = 200
ACTIVE_READ_SECONDS = 120
NOTIFY_THROTTLE_SECONDS = 600


@dataclass(frozen=True)
class Outcome:
    ok: bool
    code: str
    message: ChatMessage | None = None
    created: bool = False


# --------------------------------------------------------------------------- #
# display / payloads
# --------------------------------------------------------------------------- #

def display_name(user):
    """Full name if set, else username — never contact details."""
    if user is None:
        return ""
    try:
        profile = user.profile
    except Exception:  # noqa: BLE001 — a user without a profile row
        profile = None
    full = (getattr(profile, "full_name", "") or "").strip()
    return full or user.username


def _sender_role(message, task):
    if message.sender_id is None:
        return "system"
    if message.is_staff_post:
        return "staff"
    if message.sender_id == task.client_id:
        return "client"
    if message.sender_id == task.volunteer_id:
        return "volunteer"
    return "member"  # e.g. a volunteer the task was later taken away from


def _url(storage, path):
    try:
        return storage.url(path)
    except Exception:  # noqa: BLE001
        return None


def _image_urls(message):
    """Chat photos are private: URLs point at the permission-checked
    ``chat_message_image`` view (X-Accel-Redirect in production), never at
    /media/ directly — nginx refuses /media/chat/."""
    if not message.image:
        return None, None
    variants = message.image_variants or {}
    thumb = reverse("chat_message_image", args=[message.pk, "thumb"]) if variants.get("thumb") else None
    full = reverse("chat_message_image", args=[message.pk, "full"])
    return thumb, full


def image_storage_path(message, variant):
    """Storage path behind a protected chat image URL (None if absent)."""
    if not message.image:
        return None
    variants = message.image_variants or {}
    if variant == "thumb":
        path = variants.get("thumb")
    else:
        path = variants.get("medium") or message.image.name
    if not path or ".." in path.split("/") or not path.startswith("chat/"):
        return None
    return path


def internal_payload(message, task=None):
    """The viewer-neutral payload published on ``task_<id>`` (see
    ``events.for_viewer`` for the "_" keys)."""
    task = task or message.task
    hidden = message.hidden_at is not None
    thumb, full = _image_urls(message)
    if message.sender_id is None:
        display, label = "", events.CHAT_SYSTEM_LABEL_KEY
    elif message.is_staff_post:
        display, label = STAFF_LABEL, events.CHAT_STAFF_LABEL_KEY
    else:
        display, label = display_name(message.sender), None
    public = events.chat_message_payload(
        task_id=message.task_id, message_id=message.pk, kind=message.kind,
        body="" if hidden else message.body, created_at=message.created_at,
        is_staff_post=message.is_staff_post, hidden=hidden, sender_display=display,
        sender_role=_sender_role(message, task), label_key=label,
        thumb_url=None if hidden else thumb, image_url=None if hidden else full,
    )
    flags = list(message.flags or [])
    open_reports = getattr(message, "open_reports", None)
    if open_reports is None:
        open_reports = message.reports.filter(handled_at__isnull=True).count() if message.pk else 0
    staff = {
        "body": message.body, "thumb_url": thumb, "image_url": full, "flags": flags,
        "open_reports": open_reports,
        "reviewed": message.reviewed_at is not None,
        # A curator should look: unreviewed flags, or reports nobody handled yet.
        "needs_review": bool((flags and message.reviewed_at is None) or open_reports),
    }
    if message.is_staff_post and message.sender_id is not None:
        staff["sender_display"] = display_name(message.sender)
    return events.chat_internal(public, sender_id=message.sender_id, staff=staff)


def message_payload(message, viewer, task=None):
    """What ``viewer`` may see of ``message`` — the same function the WS
    consumer applies to the published internal payload."""
    return events.for_viewer(
        internal_payload(message, task), getattr(viewer, "pk", None), access.is_staff_user(viewer)
    )


def chat_url(task_or_id):
    return reverse("task_chat", args=[getattr(task_or_id, "pk", task_or_id)])


# --------------------------------------------------------------------------- #
# send
# --------------------------------------------------------------------------- #

def _rate_ok(user, task):
    window = int(timezone.now().timestamp() // RATE_WINDOW)
    key = f"chat:rl:{user.pk}:{task.pk}:{window}"
    try:
        cache.add(key, 0, RATE_WINDOW + 1)
        count = cache.incr(key)
    except ValueError:
        cache.set(key, 1, RATE_WINDOW + 1)
        count = 1
    except Exception:  # noqa: BLE001 — a cache outage must not block chat
        return True
    return count <= RATE_LIMIT


def _existing(task, user, client_msg_id):
    if not client_msg_id:
        return None
    return (ChatMessage.objects.select_related("sender__profile")
            .filter(task=task, sender=user, client_msg_id=client_msg_id).first())


def send_message(user, task, *, text=None, image=None, client_msg_id=""):
    """Store and publish one message. ``Outcome.created`` is False for a
    deduplicated retry (same ``client_msg_id``) — then nothing is
    re-published or re-notified."""
    if not access.can_write_chat(user, task):
        return Outcome(False, FORBIDDEN)
    client_msg_id = (client_msg_id or "").strip() if isinstance(client_msg_id, str) else ""
    if client_msg_id and not CLIENT_MSG_ID_RE.match(client_msg_id):
        return Outcome(False, INVALID)
    if text is not None and not isinstance(text, str):
        return Outcome(False, INVALID)
    body = (text or "").strip()
    if not body and image is None:
        return Outcome(False, EMPTY)
    if len(body) > CHAT_MESSAGE_MAX_LENGTH:
        return Outcome(False, TOO_LONG)

    previous = _existing(task, user, client_msg_id)
    if previous is not None:
        return Outcome(True, OK, previous, created=False)
    if image is not None:
        error = images.validate_upload(image)
        if error:
            return Outcome(False, error)
    if not _rate_ok(user, task):
        return Outcome(False, RATE_LIMITED)

    is_staff = access.is_staff_user(user)
    message = ChatMessage(
        task=task, sender=user, kind=CHAT_KIND_IMAGE if image is not None else CHAT_KIND_TEXT,
        body=body, is_staff_post=is_staff, client_msg_id=client_msg_id,
        # Party messages only — a curator's own post is never flagged.
        flags=[] if is_staff else chat_flags.detect(body),
    )
    if image is not None:
        # The stored original is a re-encoded copy without EXIF / GPS / XMP
        # (images.strip_metadata) — not only the WebP thumbs.
        try:
            clean, ext = images.strip_metadata(image)
        except Exception:  # noqa: BLE001 — validated above; a codec failure is not the user's fault
            logger.exception("chat image metadata strip failed task=%s user=%s", task.pk, user.pk)
            return Outcome(False, images.ERR_INVALID)
        # Unguessable name: media URLs are not access-controlled.
        message.image.save(f"{uuid.uuid4().hex}.{ext}", clean, save=False)
    try:
        with transaction.atomic():
            message.save()
    except IntegrityError:
        # Lost a race with the same client_msg_id (double tap on two sockets).
        if image is not None and message.image:
            message.image.delete(save=False)
        previous = _existing(task, user, client_msg_id)
        if previous is None:
            raise
        return Outcome(True, OK, previous, created=False)

    message.open_reports = 0  # brand new: nothing reported yet (no extra query)
    publish.send_to_task(task, events.CHAT_MESSAGE, internal_payload(message, task))
    if image is not None:
        _queue_variants(message)
    _notify_parties(message, task)
    if message.flags:
        _flag_to_staff(message, task)
    return Outcome(True, OK, message, created=True)


def _flag_to_staff(message, task):
    """One quiet in-app ``chat_flagged`` notice to curators/admins per task ×
    category × day (the dedupe key) — never email/Telegram by default, never
    the message text (staff open the chat to read it). ID-only log."""
    from ..notifications import staff_recipients
    from . import notify

    category = message.flags[0]
    logger.info(
        "chat message flagged message=%s task=%s flags=%s",
        message.pk, task.pk, ",".join(message.flags),
    )
    day = timezone.localdate().isoformat()
    try:
        return notify.notify(
            staff_recipients(), "chat_flagged",
            params={"task_id": task.pk, "message_id": message.pk},
            link=chat_url(task), variant=category,
            dedupe_key=f"chat_flagged:{task.pk}:{category}:{day}",
        )
    except Exception:  # noqa: BLE001 — the message is already delivered
        logger.exception("chat flag notify failed message=%s", message.pk)
        return 0


def _queue_variants(message):
    from ..notifications import _dispatch
    from ..tasks import make_chat_image_variants

    _dispatch(make_chat_image_variants, message.pk)


def apply_image_variants(message_id):
    """Celery body: build the WebP variants, store them, tell the chat."""
    message = (ChatMessage.objects.select_related("task", "sender__profile")
               .filter(pk=message_id).first())
    if message is None or not message.image:
        return None
    variants = images.make_webp_variants(message.image)
    ChatMessage.objects.filter(pk=message.pk).update(image_variants=variants)
    message.image_variants = variants
    publish.send_to_task(message.task_id, events.CHAT_MESSAGE_UPDATED, internal_payload(message))
    return variants


# --------------------------------------------------------------------------- #
# notifications
# --------------------------------------------------------------------------- #

def _throttle_key(task_id, user_id):
    return f"chat:ntf:{task_id}:{user_id}"


def _notify_parties(message, task):
    from . import notify

    recipients = []
    party_ids = [uid for uid in (task.client_id, task.volunteer_id) if uid and uid != message.sender_id]
    if not party_ids:
        return 0
    states = {
        s.user_id: s
        for s in ChatReadState.objects.filter(task=task, user_id__in=party_ids)
    }
    prev_id = (ChatMessage.objects.filter(task=task, id__lt=message.pk)
               .order_by("-id").values_list("id", flat=True).first()) or 0
    now = timezone.now()
    from accounts.models import Users

    for user in Users.objects.filter(pk__in=party_ids, is_active=True):
        state = states.get(user.pk)
        cursor = state.last_read_id if state else 0
        if state and cursor >= prev_id and now - state.updated_at <= timedelta(seconds=ACTIVE_READ_SECONDS):
            continue  # in the chat right now — they see it live
        pending = cache.get(_throttle_key(task.pk, user.pk))
        if pending and cursor < pending:
            continue  # still holds an unread chat notification for this task
        recipients.append(user)
    if not recipients:
        return 0
    for user in recipients:
        cache.set(_throttle_key(task.pk, user.pk), message.pk, NOTIFY_THROTTLE_SECONDS)

    # An image without a caption → None → the channel text's "[photo]" fallback.
    preview = message.body[:PREVIEW_CHARS] if message.body else None
    sender_display = STAFF_LABEL if message.is_staff_post else display_name(message.sender)
    return notify.notify(
        recipients, "chat_message",
        params={"task_id": task.pk, "sender_display": "" if message.is_staff_post else sender_display},
        link=chat_url(task),
        variant="staff" if message.is_staff_post else None,
        extra={"sender_display": sender_display, "preview": preview},
    )


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #

def _receipts(task, viewer):
    """Read cursors of the two parties other than the viewer (read ticks)."""
    party_ids = [uid for uid in (task.client_id, task.volunteer_id) if uid and uid != viewer.pk]
    if not party_ids:
        return []
    rows = ChatReadState.objects.filter(task=task, user_id__in=party_ids).select_related("user__profile")
    return [
        {"role": "client" if r.user_id == task.client_id else "volunteer",
         "user_display": display_name(r.user), "last_id": r.last_read_id}
        for r in rows
    ]


def history(user, task, *, before_id=None, after_id=None, limit=PAGE_SIZE):
    """One page of messages, oldest first, as ``user`` may see them.

    ``before_id``: the page of older messages (scrolling up; cursor = the
    smallest id the client holds). ``after_id``: messages newer than it (the
    HTTP polling fallback while the socket is down). Neither: the latest
    page. Returns None when the user can't read the chat."""
    if not access.can_read_chat(user, task):
        return None
    limit = max(1, min(int(limit or PAGE_SIZE), MAX_PAGE_SIZE))
    qs = (ChatMessage.objects.filter(task=task).select_related("sender__profile")
          .annotate(open_reports=Count("reports", filter=Q(reports__handled_at__isnull=True))))
    if after_id is not None:
        rows = list(qs.filter(id__gt=after_id).order_by("id")[: limit + 1])
        has_more = len(rows) > limit
        rows = rows[:limit]
    else:
        if before_id is not None:
            qs = qs.filter(id__lt=before_id)
        rows = list(qs.order_by("-id")[: limit + 1])
        has_more = len(rows) > limit
        rows = list(reversed(rows[:limit]))
    state = ChatReadState.objects.filter(task=task, user=user).values_list("last_read_id", flat=True).first()
    return {
        "task_id": task.pk,
        "messages": [message_payload(m, user, task) for m in rows],
        "has_more": has_more,
        "cursor": rows[0].pk if rows else None,       # pass as ?before= for older
        "latest_id": rows[-1].pk if rows else after_id,  # pass as ?after= when polling
        "my_last_read_id": state or 0,
        "receipts": _receipts(task, user),
        "can_write": access.can_write_chat(user, task),
        "archived": access.chat_archived(task),
    }


def mark_read(user, task, last_id):
    """Move the user's read cursor forward (never back) to ``last_id``,
    clamped to the newest message. Publishes ``chat.read`` on a change.
    Returns the new cursor, or None (forbidden / nothing changed)."""
    if not access.can_read_chat(user, task):
        return None
    try:
        last_id = int(last_id)
    except (TypeError, ValueError):
        return None
    newest = ChatMessage.objects.filter(task=task).order_by("-id").values_list("id", flat=True).first() or 0
    last_id = min(last_id, newest)
    if last_id <= 0:
        return None
    state, created = ChatReadState.objects.get_or_create(task=task, user=user, defaults={"last_read_id": last_id})
    if not created:
        changed = (ChatReadState.objects.filter(pk=state.pk, last_read_id__lt=last_id)
                   .update(last_read_id=last_id, updated_at=timezone.now()))
        if not changed:
            # Same cursor: still refresh "is reading now" for the notify rule.
            ChatReadState.objects.filter(pk=state.pk).update(updated_at=timezone.now())
            return None
    staff = access.is_staff_user(user)
    public = events.chat_read_payload(task.pk, STAFF_LABEL if staff else display_name(user), last_id)
    publish.send_to_task(task, events.CHAT_READ, events.chat_internal(
        public, sender_id=user.pk, staff={"user_display": display_name(user)}, staff_only=staff,
    ))
    return last_id


def unread_counts(user, task_ids):
    """{task_id: unread messages} for the given tasks, in ONE query.

    Counts messages newer than the user's read cursor, not written by the
    user; hidden messages don't count for non-staff. Tasks the user isn't a
    party of (non-staff) are ignored. Tasks with nothing unread are omitted."""
    ids = [int(t) for t in task_ids or [] if t]
    if not ids or user is None or not getattr(user, "is_authenticated", False):
        return {}
    cursor = ChatReadState.objects.filter(task_id=OuterRef("task_id"), user=user).values("last_read_id")[:1]
    qs = ChatMessage.objects.filter(task_id__in=ids).exclude(sender=user)
    if not access.is_staff_user(user):
        qs = qs.filter(Q(task__client=user) | Q(task__volunteer=user), hidden_at__isnull=True)
    rows = (qs.annotate(cursor=Coalesce(Subquery(cursor), Value(0)))
            .filter(id__gt=F("cursor"))
            .values("task_id").annotate(n=Count("id")).order_by())
    return {row["task_id"]: row["n"] for row in rows}


# --------------------------------------------------------------------------- #
# moderation
# --------------------------------------------------------------------------- #

def report(user, message, reason=""):
    """A chat reader reports someone else's message to staff (once per
    reporter). Staff get one in-app ``chat_reported`` per message."""
    from ..notifications import staff_recipients
    from . import notify

    task = message.task
    if not access.can_read_chat(user, task):
        return Outcome(False, FORBIDDEN)
    if message.sender_id is None:
        return Outcome(False, INVALID)
    if message.sender_id == user.pk:
        return Outcome(False, OWN_MESSAGE)
    reason = (reason or "").strip()[:REPORT_REASON_MAX] if isinstance(reason, str) else ""
    try:
        with transaction.atomic():
            ChatReport.objects.create(message=message, reporter=user, reason=reason)
    except IntegrityError:
        return Outcome(True, ALREADY, message)
    logger.info("chat message reported message=%s task=%s reporter=%s", message.pk, task.pk, user.pk)
    message.open_reports = None  # recount for the staff view
    publish.send_to_task(task, events.CHAT_MESSAGE_UPDATED, internal_payload(message, task))
    reached = notify.notify(
        staff_recipients(), "chat_reported",
        params={"task_id": task.pk, "message_id": message.pk},
        link=chat_url(task), dedupe_key=f"chat_reported:{message.pk}",
    )
    if not reached:
        logger.error("chat report message=%s reached 0 staff recipients", message.pk)
    return Outcome(True, OK, message)


def _set_hidden(staff, message, hide):
    if not access.is_staff_user(staff) or not staff.is_active:
        return Outcome(False, FORBIDDEN)
    if message.kind == CHAT_KIND_SYSTEM:
        return Outcome(False, INVALID)
    now = timezone.now()
    if hide:
        changed = ChatMessage.objects.filter(pk=message.pk, hidden_at__isnull=True).update(
            hidden_at=now, hidden_by=staff)
    else:
        changed = ChatMessage.objects.filter(pk=message.pk, hidden_at__isnull=False).update(
            hidden_at=None, hidden_by=None)
    message.refresh_from_db()
    if not changed:
        return Outcome(True, OK, message)
    if hide:
        ChatReport.objects.filter(message=message, handled_at__isnull=True).update(handled_at=now, handled_by=staff)
    message.open_reports = None
    logger.info("chat message %s message=%s task=%s by=%s",
                "hidden" if hide else "unhidden", message.pk, message.task_id, staff.pk)
    publish.send_to_task(message.task_id, events.CHAT_HIDDEN,
                         events.chat_hidden_payload(message.task_id, message.pk, hide))
    publish.send_to_task(message.task_id, events.CHAT_MESSAGE_UPDATED, internal_payload(message))
    return Outcome(True, OK, message, created=True)


def hide(staff, message):
    return _set_hidden(staff, message, True)


def review(staff, message):
    """A curator looked at a flagged / reported message and closes the case
    ("Проверено"): stamps ``reviewed_at`` and handles every open report. The
    message stays visible (hide is the separate moderation step). Starts the
    CHAT_HOLD_DAYS retention clock (services/chat_retention.py). Idempotent."""
    if not access.is_staff_user(staff) or not staff.is_active:
        return Outcome(False, FORBIDDEN)
    if message.kind == CHAT_KIND_SYSTEM or message.sender_id is None:
        return Outcome(False, INVALID)
    now = timezone.now()
    reviewed = ChatMessage.objects.filter(pk=message.pk, reviewed_at__isnull=True).update(
        reviewed_at=now, reviewed_by=staff)
    handled = ChatReport.objects.filter(message=message, handled_at__isnull=True).update(
        handled_at=now, handled_by=staff)
    message.refresh_from_db()
    message.open_reports = 0
    if reviewed or handled:
        logger.info("chat message reviewed message=%s task=%s by=%s reports=%s",
                    message.pk, message.task_id, staff.pk, handled)
        publish.send_to_task(message.task_id, events.CHAT_MESSAGE_UPDATED, internal_payload(message))
    return Outcome(True, OK, message, created=bool(reviewed or handled))


def unhide(staff, message):
    return _set_hidden(staff, message, False)


# --------------------------------------------------------------------------- #
# lifecycle
# --------------------------------------------------------------------------- #

def close(task):
    """After the task was completed or cancelled: one system message
    ("chat closed" / "request cancelled") so the parties see why they can't
    write any more. Idempotent; a no-op while the task is still open or when
    it never had a volunteer (no chat existed). Never raises into the
    caller's request. Returns the new message or None."""
    try:
        return _close(task)
    except Exception:  # noqa: BLE001 — closing the chat must not break complete/cancel
        logger.exception("chat close failed task=%s", getattr(task, "pk", None))
        return None


def _close(task):
    if task.status not in ("completed", "cancelled") or not task.volunteer_id:
        return None
    body = SYSTEM_CLOSED if task.status == "completed" else SYSTEM_CANCELLED
    with transaction.atomic():
        # Serialise concurrent closes on the task row (Postgres; SQLite
        # serialises writers anyway).
        list(HelpRequest.objects.select_for_update().filter(pk=task.pk).values_list("pk", flat=True))
        if ChatMessage.objects.filter(task=task, kind=CHAT_KIND_SYSTEM, body__in=SYSTEM_BODIES).exists():
            return None
        message = ChatMessage.objects.create(task=task, sender=None, kind=CHAT_KIND_SYSTEM, body=body)
    publish.send_to_task(task, events.CHAT_MESSAGE, internal_payload(message, task))
    return message


# --------------------------------------------------------------------------- #
# UI strings (i18n keys used by templates/myapp/chat.html + static/js/chat.js).
# Source of the static/js/i18n.js snippet; RU/TJ/EN key sets are test-locked.
# --------------------------------------------------------------------------- #

UI_STRINGS = {
    "chat.title": {"en": "Chat", "ru": "Чат", "tj": "Чат"},
    "chat.lede": {"en": "Messages between the client, the volunteer and the curators.",
                  "ru": "Переписка клиента, волонтёра и кураторов.",
                  "tj": "Мукотибаи мизоҷ, ихтиёрӣ ва кураторон."},
    "chat.open": {"en": "Open chat", "ru": "Открыть чат", "tj": "Кушодани чат"},
    "chat.unread": {"en": "{count} new", "ru": "Новых: {count}", "tj": "Нав: {count}"},
    "chat.back_to_task": {"en": "Back to the request", "ru": "Назад к запросу", "tj": "Бозгашт ба дархост"},
    "chat.large_text": {"en": "Large text", "ru": "Крупный шрифт", "tj": "Ҳарфи калон"},
    "chat.load_older": {"en": "Show earlier messages", "ru": "Показать предыдущие сообщения", "tj": "Нишон додани паёмҳои пешина"},
    "chat.archived": {"en": "This chat is archived.", "ru": "Этот чат в архиве.", "tj": "Ин чат дар бойгонӣ аст."},
    "chat.read_only": {"en": "The request is finished — the chat is read-only.",
                       "ru": "Запрос завершён — чат доступен только для чтения.",
                       "tj": "Дархост анҷом ёфт — чат танҳо барои хондан аст."},
    "chat.staff_label": {"en": "Curator", "ru": "Куратор", "tj": "Куратор"},
    "chat.system_label": {"en": "System", "ru": "Система", "tj": "Система"},
    "chat.system.closed": {"en": "The request is completed. The chat is now closed.",
                           "ru": "Запрос выполнен. Чат закрыт.",
                           "tj": "Дархост иҷро шуд. Чат пӯшида шуд."},
    "chat.system.cancelled": {"en": "The request was cancelled. The chat is now closed.",
                              "ru": "Запрос отменён. Чат закрыт.",
                              "tj": "Дархост бекор шуд. Чат пӯшида шуд."},
    "chat.hidden_stub": {"en": "Message hidden by a moderator.", "ru": "Сообщение скрыто модератором.",
                         "tj": "Паём аз ҷониби модератор пинҳон шуд."},
    "chat.hidden_tag": {"en": "Hidden", "ru": "Скрыто", "tj": "Пинҳон"},
    # Staff-only safety flags on a message (services/chat_flags.py)
    "chat.flag.sms_code": {"en": "SMS code?", "ru": "Код из СМС?", "tj": "Коди SMS?"},
    "chat.flag.money": {"en": "Money?", "ru": "Деньги?", "tj": "Пул?"},
    "chat.flag.off_platform": {"en": "Other messenger?", "ru": "Другой мессенджер?", "tj": "Мессенҷери дигар?"},
    "chat.flag.phone": {"en": "Phone number", "ru": "Номер телефона", "tj": "Рақами телефон"},
    "chat.mark_reviewed": {"en": "Reviewed", "ru": "Проверено", "tj": "Санҷида шуд"},
    "chat.reviewed_tag": {"en": "Reviewed", "ru": "Проверено", "tj": "Санҷида шуд"},
    "chat.reports_tag": {"en": "Reported: {count}", "ru": "Жалоб: {count}", "tj": "Шикоятҳо: {count}"},
    "chat.photo_expired": {"en": "The photo was deleted after the storage period.",
                           "ru": "Фото удалено по истечении срока хранения.",
                           "tj": "Сурат пас аз ба охир расидани мӯҳлати нигоҳдорӣ нест карда шуд."},
    "chat.flag.hint": {"en": "Automatic flag — visible to curators only. The message was not blocked.",
                       "ru": "Автоматический флаг — его видят только кураторы. Сообщение не заблокировано.",
                       "tj": "Аломати худкор — онро танҳо кураторон мебинанд. Паём манъ карда нашудааст."},
    "chat.report": {"en": "Report", "ru": "Пожаловаться", "tj": "Шикоят"},
    "chat.report_confirm": {"en": "Report this message to the curators?", "ru": "Пожаловаться кураторам на это сообщение?",
                            "tj": "Ба кураторон аз ин паём шикоят кунед?"},
    "chat.reported": {"en": "Thank you — the curators will review it.", "ru": "Спасибо — кураторы проверят сообщение.",
                      "tj": "Ташаккур — кураторон паёмро месанҷанд."},
    "chat.hide": {"en": "Hide", "ru": "Скрыть", "tj": "Пинҳон кардан"},
    "chat.unhide": {"en": "Show", "ru": "Показать", "tj": "Нишон додан"},
    "chat.empty": {"en": "No messages yet.", "ru": "Сообщений пока нет.", "tj": "Ҳоло паём нест."},
    "chat.seen": {"en": "Seen", "ru": "Прочитано", "tj": "Хонда шуд"},
    "chat.typing": {"en": "{name} is typing…", "ru": "{name} печатает…", "tj": "{name} менависад…"},
    "chat.quick.on_way": {"en": "I'm on my way", "ru": "Я в пути", "tj": "Ман дар роҳам"},
    "chat.quick.arrived": {"en": "I'm here", "ru": "Я на месте", "tj": "Ман расидам"},
    "chat.quick.call_me": {"en": "Please call me", "ru": "Позвоните мне", "tj": "Ба ман занг занед"},
    "chat.placeholder": {"en": "Message", "ru": "Сообщение", "tj": "Паём"},
    "chat.send": {"en": "Send", "ru": "Отправить", "tj": "Фиристодан"},
    "chat.attach": {"en": "Attach a photo", "ru": "Прикрепить фото", "tj": "Замимаи сурат"},
    "chat.upload": {"en": "Send photo", "ru": "Отправить фото", "tj": "Фиристодани сурат"},
    "chat.error.generic": {"en": "Something went wrong. Please try again.", "ru": "Что-то пошло не так. Попробуйте ещё раз.",
                           "tj": "Хатое рух дод. Бори дигар кӯшиш кунед."},
    "chat.error.empty": {"en": "The message is empty.", "ru": "Сообщение пустое.", "tj": "Паём холӣ аст."},
    "chat.error.too_long": {"en": "The message is longer than 1000 characters.", "ru": "Сообщение длиннее 1000 символов.",
                            "tj": "Паём аз 1000 аломат дарозтар аст."},
    "chat.error.rate_limited": {"en": "Too many messages — please wait a minute.",
                                "ru": "Слишком много сообщений — подождите минуту.",
                                "tj": "Паёмҳо хеле зиёданд — як дақиқа сабр кунед."},
    "chat.error.forbidden": {"en": "You can't write in this chat now.", "ru": "В этот чат сейчас нельзя писать.",
                             "tj": "Ҳоло ба ин чат навиштан мумкин нест."},
    "chat.error.invalid": {"en": "The action could not be completed.", "ru": "Не удалось выполнить действие.",
                           "tj": "Амалро иҷро кардан нашуд."},
    "chat.error.bad_request": {"en": "The action could not be completed.", "ru": "Не удалось выполнить действие.",
                               "tj": "Амалро иҷро кардан нашуд."},
    "chat.error.server_error": {"en": "Server error. Please try again later.", "ru": "Ошибка сервера. Попробуйте позже.",
                                "tj": "Хатои сервер. Баъдтар кӯшиш кунед."},
    "chat.error.too_large": {"en": "The file is too large (max 5 MB).", "ru": "Файл слишком большой (максимум 5 МБ).",
                             "tj": "Файл хеле калон аст (ҳадди аксар 5 МБ)."},
    "chat.error.invalid_image": {"en": "This file is not an image.", "ru": "Файл не похож на изображение.",
                                 "tj": "Ин файл тасвир нест."},
    "chat.error.unsupported_format": {"en": "Only JPEG, PNG and WebP are supported.",
                                      "ru": "Поддерживаются только JPEG, PNG и WebP.",
                                      "tj": "Танҳо JPEG, PNG ва WebP дастгирӣ мешаванд."},
    "chat.error.own_message": {"en": "You can't report your own message.", "ru": "Нельзя пожаловаться на своё сообщение.",
                               "tj": "Аз паёми худ шикоят кардан мумкин нест."},
}


def ui_i18n_keys(lang):
    """{key: text} of the chat UI strings for one language."""
    return {key: texts[lang] for key, texts in UI_STRINGS.items()}
