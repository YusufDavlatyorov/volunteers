"""Chat retention (owner decision 2026-10-03, option A) — the daily sweep.

After a task has ended (completed / cancelled — ``access.chat_ended_at``):

* photos (original + WebP variants) are deleted after
  ``CHAT_PHOTO_RETENTION_DAYS`` (90) — the message row stays, the chat shows
  "the photo was deleted after the storage period";
* messages are deleted after ``CHAT_TEXT_RETENTION_DAYS`` (180), together with
  their reports, and the chat's read cursors once nothing is left.

**Hold**: a message that needs or had a curator's attention is kept until
``CHAT_HOLD_DAYS`` (180) after the case was closed — an unhandled report or
unreviewed safety flags hold it indefinitely (``chat.review`` / ``chat.hide``
close the case and start the clock). Open tasks are never touched.

Idempotent, batched, never deletes anything of a task that is still open;
``dry_run`` counts without touching rows or files. Logs counts only. Run by
the ``purge_chats`` management command (daily cron, DEPLOY checklist).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from ..models import ChatMessage, ChatReadState, ChatReport, HelpRequest
from . import images

logger = logging.getLogger(__name__)

BATCH = 500


@dataclass(frozen=True)
class PurgeResult:
    photos: int = 0
    messages: int = 0
    held: int = 0
    read_states: int = 0


def _days(name, default):
    return int(getattr(settings, name, default))


def ended_before_q(cutoff, prefix="task__"):
    """The task ended (completed / cancelled) before ``cutoff`` — same rule as
    ``access.chat_ended_at`` (the event time, else ``updated_at``)."""
    p = prefix
    return (
        Q(**{f"{p}status": "completed", f"{p}completed_at__lt": cutoff})
        | Q(**{f"{p}status": "completed", f"{p}completed_at__isnull": True, f"{p}updated_at__lt": cutoff})
        | Q(**{f"{p}status": "cancelled", f"{p}cancelled_at__lt": cutoff})
        | Q(**{f"{p}status": "cancelled", f"{p}cancelled_at__isnull": True, f"{p}updated_at__lt": cutoff})
    )


def held_q(now):
    """Messages kept past the normal periods (see the module docstring)."""
    hold_cut = now - timedelta(days=_days("CHAT_HOLD_DAYS", 180))
    open_report = ChatReport.objects.filter(message=OuterRef("pk"), handled_at__isnull=True)
    recent_report = ChatReport.objects.filter(message=OuterRef("pk"), handled_at__gte=hold_cut)
    return (
        Exists(open_report)
        | Exists(recent_report)
        | (~Q(flags=[]) & Q(reviewed_at__isnull=True))
        | Q(reviewed_at__gte=hold_cut)
    )


def _delete_files(message):
    storage = message.image.storage
    images.delete_variants(storage, message.image_variants)
    try:
        storage.delete(message.image.name)
    except Exception:  # noqa: BLE001 — a missing file must not stop the sweep
        logger.warning("chat purge: could not delete a photo file message=%s", message.pk)


def purge(now=None, *, dry_run=False) -> PurgeResult:
    now = now or timezone.now()
    photo_cut = now - timedelta(days=_days("CHAT_PHOTO_RETENTION_DAYS", 90))
    text_cut = now - timedelta(days=_days("CHAT_TEXT_RETENTION_DAYS", 180))
    held = held_q(now)

    photo_qs = ChatMessage.objects.filter(ended_before_q(photo_cut)).exclude(image="")
    text_qs = ChatMessage.objects.filter(ended_before_q(text_cut))
    held_count = (photo_qs | text_qs).filter(held).distinct().count()
    photo_qs = photo_qs.exclude(held)
    text_qs = text_qs.exclude(held)

    if dry_run:
        return PurgeResult(photos=photo_qs.count(), messages=text_qs.count(), held=held_count,
                           read_states=_read_states(text_cut).count())

    photos = 0
    while True:
        batch = list(photo_qs.only("id", "image", "image_variants")[:BATCH])
        if not batch:
            break
        for message in batch:
            _delete_files(message)
        photos += ChatMessage.objects.filter(pk__in=[m.pk for m in batch]).update(image="", image_variants={})

    messages = 0
    while True:
        batch = list(text_qs.only("id", "image", "image_variants")[:BATCH])
        if not batch:
            break
        for message in batch:
            if message.image:
                _delete_files(message)
        deleted, _ = ChatMessage.objects.filter(pk__in=[m.pk for m in batch]).delete()
        messages += len(batch)
        if not deleted:
            break

    read_states, _ = _read_states(text_cut).delete()
    result = PurgeResult(photos=photos, messages=messages, held=held_count, read_states=read_states)
    logger.info("chat purge photos=%s messages=%s held=%s read_states=%s",
                result.photos, result.messages, result.held, result.read_states)
    return result


def _read_states(text_cut):
    """Read cursors of ended chats with no message left."""
    has_messages = ChatMessage.objects.filter(task=OuterRef("task"))
    ended_tasks = HelpRequest.objects.filter(ended_before_q(text_cut, prefix="")).values("pk")
    return ChatReadState.objects.filter(task__in=ended_tasks).exclude(Exists(has_messages))
