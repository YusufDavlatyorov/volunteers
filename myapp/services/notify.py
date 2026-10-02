"""Typed notification fan-out (Feature Pack v2, feature 11).

``notify(recipients, type, ...)`` is the one way the platform tells a user
something:

1. resolves recipients (users, a queryset, any iterable; duplicates and unsaved
   users dropped),
2. bulk-creates one in-app ``Notification`` row per recipient (in-app is always
   on), skipping recipients that already hold the same ``dedupe_key``,
3. publishes ``notification.new`` to each recipient's ``user_<id>`` WS group,
4. delivers email / Telegram per the type's default channels and the user's
   ``NotificationPreference`` overrides, through the existing Celery transport
   (``myapp.notifications._dispatch`` + ``myapp.tasks`` — one email per address,
   never a shared ``To:``, retries, inline fallback when the broker is down),
   rendered in the recipient's ``Profile.language``,
5. returns the number of recipients reached on at least one channel (a deduped
   recipient counts — they already have it). 0 means nobody could be reached,
   which the overdue / stale / emergency callers keep logging at ERROR.

Logs are ID-only. Sends stay explicit in services/views — never in signals.

Channels are a registry (``CHANNELS``): a later ``webpush`` channel is one more
``Channel`` subclass + a key in the types' defaults; no call site changes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from ..models import Notification, NotificationPreference
from ..realtime import events, publish
from . import notify_texts

logger = logging.getLogger(__name__)

ROLES_ALL = ("admin", "curator", "volunteer", "client", "guest")
STAFF = ("admin", "curator")

IN_APP = "in_app"
EMAIL = "email"
TELEGRAM = "telegram"


@dataclass(frozen=True)
class NotificationType:
    code: str
    roles: tuple            # who it is relevant to (the preferences page)
    defaults: frozenset     # channels on by default (in_app is implicit)
    forced: bool = False    # safety-critical: defaults can't be switched off
    role_defaults: dict = field(default_factory=dict)  # role -> frozenset override


_ET = frozenset({EMAIL, TELEGRAM})


def _t(code, roles, defaults=_ET, **kw):
    return code, NotificationType(code=code, roles=tuple(roles), defaults=frozenset(defaults), **kw)


# Every pre-existing send went to email + Telegram, so those stay the defaults.
NOTIFICATION_TYPES = dict([
    _t("task_new_in_region", ("volunteer",)),
    _t("task_assigned", ("volunteer", "client")),
    _t("task_accepted", ("volunteer", "client")),
    _t("task_stage_changed", ("client",)),
    _t("task_completed", ("client",)),
    # Parties get email/Telegram; staff (a client withdrew a pending request)
    # only in-app — it needs no action from them.
    _t("task_cancelled", ("client", "volunteer") + STAFF,
       role_defaults={"admin": frozenset(), "curator": frozenset()}),
    _t("sos_new", STAFF, forced=True),
    _t("sos_update", ("volunteer",)),
    _t("application_submitted", ("admin",)),
    _t("application_approved", ("guest", "volunteer", "client")),
    _t("application_rejected", ("guest", "volunteer", "client")),
    _t("donation_new", STAFF),
    _t("donation_assigned", ("volunteer",)),
    _t("donation_status", ROLES_ALL),
    _t("pet_new", STAFF),
    _t("pet_match", ROLES_ALL),
    _t("pet_status", ROLES_ALL),
    _t("broadcast", ("volunteer",)),
    _t("event_new", ("volunteer",)),
    _t("task_overdue", STAFF),
    _t("task_stale", STAFF),
    _t("task_escalated", STAFF),
    _t("volunteer_pinged", ("volunteer",)),
    # Later milestones (chat / reviews / achievements) — registered now so
    # preferences and the i18n keys exist before the features land.
    _t("chat_message", ("volunteer", "client") + STAFF, defaults={TELEGRAM}),
    _t("review_received", ("volunteer",)),
    _t("achievement_unlocked", ("volunteer",), defaults={TELEGRAM}),
])


# --------------------------------------------------------------------------- #
# channels
# --------------------------------------------------------------------------- #

class Channel:
    """One external delivery channel. ``send`` queues the delivery and returns
    True when something was queued for this user."""

    code = ""
    can_disable = True

    def available(self) -> bool:
        return True

    def address(self, user):
        raise NotImplementedError

    def send(self, address, subject, text) -> None:
        raise NotImplementedError


class EmailChannel(Channel):
    code = EMAIL

    def address(self, user):
        return (user.email or "").strip() or None

    def send(self, address, subject, text):
        from ..notifications import _dispatch
        from ..tasks import send_email_task

        _dispatch(send_email_task, address, subject, text)


class TelegramChannel(Channel):
    code = TELEGRAM

    def available(self):
        return bool(getattr(settings, "TELEGRAM_BOT_TOKEN", ""))

    def address(self, user):
        return user.telegram_id or None

    def send(self, address, subject, text):
        from ..notifications import _dispatch
        from ..tasks import send_telegram_task

        _dispatch(send_telegram_task, address, text)


# Order = delivery order. in_app is not here: it is the Notification row itself.
CHANNELS = {EMAIL: EmailChannel(), TELEGRAM: TelegramChannel()}
ALL_CHANNELS = (IN_APP,) + tuple(CHANNELS)


# --------------------------------------------------------------------------- #
# params safety
# --------------------------------------------------------------------------- #

_FORBIDDEN_KEY = re.compile(r"phone|email|e_mail|address|token|password", re.I)
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_PHONE_MIN_DIGITS = 9  # a TJ number is 9 digits after +992; dates stay below this


def safe_params(params):
    """Copy of ``params`` fit for a Notification row / WS payload: scalar
    values only, contact-ish keys dropped, email/phone-looking strings
    redacted. Defence in depth — callers already pass ids/codes/names."""
    clean = {}
    for key, value in (params or {}).items():
        if _FORBIDDEN_KEY.search(str(key)):
            continue
        if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
            clean[key] = value
            continue
        text = str(value)[:300]
        text = _EMAIL_RE.sub("…", text)
        text = _PHONE_RE.sub(
            lambda m: "…" if sum(c.isdigit() for c in m.group()) >= _PHONE_MIN_DIGITS else m.group(), text
        )
        clean[key] = text
    return clean


# --------------------------------------------------------------------------- #
# preferences
# --------------------------------------------------------------------------- #

def types_for_role(role):
    return [t for t in NOTIFICATION_TYPES.values() if role in t.roles]


def default_channels(ntype, role):
    return ntype.role_defaults.get(role, ntype.defaults)


def channel_enabled(ntype, role, channel, overrides):
    """``overrides``: {(type, channel): enabled} for one user."""
    if channel == IN_APP:
        return True
    default = channel in default_channels(ntype, role)
    if ntype.forced and default:
        return True
    return overrides.get((ntype.code, channel), default)


def preference_matrix(user):
    """Rows for the preferences page: one per type relevant to the user's
    role, with each channel's effective state and whether it can change."""
    role = user.role
    overrides = {
        (p.type, p.channel): p.enabled
        for p in NotificationPreference.objects.filter(user=user)
    }
    rows = []
    for ntype in types_for_role(role):
        cells = []
        for channel in ALL_CHANNELS:
            locked = channel == IN_APP or (ntype.forced and channel in default_channels(ntype, role))
            cells.append({
                "channel": channel,
                "enabled": channel_enabled(ntype, role, channel, overrides),
                "locked": locked,
            })
        rows.append({"type": ntype.code, "cells": cells})
    return rows


def save_preferences(user, enabled_pairs):
    """Store overrides from a submitted matrix. ``enabled_pairs`` = set of
    (type, channel) the user ticked. Only types relevant to the user's role
    and changeable channels are touched; a value equal to the default deletes
    the override (rows store overrides only)."""
    role = user.role
    for ntype in types_for_role(role):
        for channel in CHANNELS:
            default = channel in default_channels(ntype, role)
            if ntype.forced and default:
                continue
            wanted = (ntype.code, channel) in enabled_pairs
            if wanted == default:
                NotificationPreference.objects.filter(user=user, type=ntype.code, channel=channel).delete()
            else:
                NotificationPreference.objects.update_or_create(
                    user=user, type=ntype.code, channel=channel, defaults={"enabled": wanted}
                )


# --------------------------------------------------------------------------- #
# notify()
# --------------------------------------------------------------------------- #

def _resolve(recipients):
    seen, users = set(), []
    for user in recipients or []:
        if user is None or user.pk is None or user.pk in seen:
            continue
        seen.add(user.pk)
        users.append(user)
    return users


def serialize(notification):
    """The one public shape of a notification (WS ``notification.new``, bell
    JSON, poll endpoint). No recipient identity, no contact details."""
    return events.notification_payload(notification)


def notify(recipients, type, *, params=None, link="", dedupe_key=None, subject=None,
           message=None, extra=None, variant=None) -> int:
    """Fan one event out to ``recipients``. See the module docstring.

    ``params``  — safe data stored on the row + sent over WS (ids, codes,
                  display names). Sanitised again by ``safe_params``.
    ``extra``   — channel-only context for the email/Telegram text (may hold
                  the client's phone/address for the parties of a task); never
                  stored, never published.
    ``subject`` / ``message`` — free-text override of the email/Telegram text
                  (staff broadcasts); same text for every language.
    ``variant`` — picks a more specific in-app i18n key when one exists
                  (e.g. ``task_stage_changed`` + ``arrived``).
    """
    ntype = NOTIFICATION_TYPES.get(type)
    if ntype is None:
        raise ValueError(f"unknown notification type: {type}")
    users = _resolve(recipients)
    if not users:
        return 0

    stored = safe_params(params)
    title_key, body_key = notify_texts.inapp_keys(type, variant)
    ids = [u.pk for u in users]

    # One query each: languages, preference overrides, existing dedupe rows.
    from accounts.models import Profile

    langs = dict(Profile.objects.filter(user_id__in=ids).values_list("user_id", "language"))
    overrides = {}
    for user_id, ptype, channel, enabled in NotificationPreference.objects.filter(
        user_id__in=ids, type=type
    ).values_list("user_id", "type", "channel", "enabled"):
        overrides.setdefault(user_id, {})[(ptype, channel)] = enabled
    already = set()
    if dedupe_key:
        already = set(
            Notification.objects.filter(recipient_id__in=ids, dedupe_key=dedupe_key)
            .values_list("recipient_id", flat=True)
        )

    fresh = [u for u in users if u.pk not in already]
    rows = [
        Notification(
            recipient_id=u.pk, type=type, title_key=title_key, body_key=body_key,
            params=stored, link=link or "", dedupe_key=dedupe_key,
        )
        for u in fresh
    ]
    created = _bulk_create(rows, dedupe_key)
    created_for = {n.recipient_id for n in created}

    for n in created:
        publish.send_to_user(n.recipient_id, events.NOTIFICATION_NEW, serialize(n))

    reached = set(already) | created_for
    context = {**stored, **(extra or {})}
    sent_addresses = {code: set() for code in CHANNELS}
    rendered = {}
    queued = 0
    for user in fresh:
        if user.pk not in created_for:
            continue  # lost a dedupe race: the winner already delivers
        role = user.role
        lang = langs.get(user.pk) or notify_texts.DEFAULT_LANG
        for code, channel in CHANNELS.items():
            if not channel.available():
                continue
            if not channel_enabled(ntype, role, code, overrides.get(user.pk, {})):
                continue
            address = channel.address(user)
            if not address or address in sent_addresses[code]:
                continue
            if lang not in rendered:
                if subject is not None or message is not None:
                    rendered[lang] = (subject or "", message or "")
                else:
                    rendered[lang] = notify_texts.render_channel(type, lang, context)
            sent_addresses[code].add(address)
            channel.send(address, *rendered[lang])
            queued += 1
            reached.add(user.pk)

    logger.info(
        "notify type=%s recipients=%d in_app=%d deduped=%d external=%d",
        type, len(users), len(created), len(already), queued,
    )
    return len(reached)


def _bulk_create(rows, dedupe_key):
    if not rows:
        return []
    try:
        with transaction.atomic():
            return Notification.objects.bulk_create(rows)
    except IntegrityError:
        if not dedupe_key:
            raise
    # A concurrent notify() with the same dedupe_key won for some recipient:
    # fall back to one row at a time, keeping only the ones we created.
    created = []
    for row in rows:
        try:
            with transaction.atomic():
                row.pk = None
                row.save(force_insert=True)
            created.append(row)
        except IntegrityError:
            continue
    return created


# --------------------------------------------------------------------------- #
# reading / marking
# --------------------------------------------------------------------------- #

def unread_count(user):
    return Notification.objects.filter(recipient=user, read_at__isnull=True).count()


def mark_read(user, *, ids=None, all=False, publish_event=True):
    """Mark the user's own notifications read (others' ids are ignored).
    Returns the ids actually changed (or "all"). Publishes ``notification.read``
    to the user's group so other tabs update, unless ``publish_event=False``
    (the WS consumer publishes from its own loop)."""
    qs = Notification.objects.filter(recipient=user, read_at__isnull=True)
    now = timezone.now()
    if all:
        qs.update(read_at=now)
        payload = events.notification_read_payload(all=True, unread=0)
    else:
        clean_ids = []
        for raw in (ids or [])[:200]:
            try:
                clean_ids.append(int(raw))
            except (TypeError, ValueError):
                continue
        changed = list(qs.filter(pk__in=clean_ids).values_list("pk", flat=True))
        if changed:
            Notification.objects.filter(pk__in=changed).update(read_at=now)
        payload = events.notification_read_payload(ids=changed, unread=unread_count(user))
    if publish_event:
        publish.send_to_user(user.pk, events.NOTIFICATION_READ, payload)
    return payload


def prune(days=None, *, dry_run=False):
    """Delete notifications older than ``NOTIFICATION_RETENTION_DAYS``."""
    days = settings.NOTIFICATION_RETENTION_DAYS if days is None else days
    cutoff = timezone.now() - timedelta(days=days)
    qs = Notification.objects.filter(created_at__lt=cutoff)
    if dry_run:
        return qs.count()
    deleted, _ = qs.delete()
    return deleted
