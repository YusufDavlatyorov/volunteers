"""Help-request cancellation — the one cancel path (owner decision, FEATURES_PLAN §9.4).

Who may cancel:
- the request's **client**, while it is still ``pending`` (reason optional);
- **staff** (curator/admin), from any open state — ``pending`` or ``active``
  (reason required, it is shown to the client and volunteer).

The transition is a conditional ``UPDATE … WHERE status IN (…)`` — the same
guard as accept/complete — so a cancel racing a volunteer's accept (or a double
submit) has exactly one winner. Side effects run only for the winner: the
volunteer's availability is recomputed, task parties are told live
(``task.updated``) and through ``notify``; live tracking stops and the chat
closes (hooks filled in by milestones 2 and 3). The log line carries IDs only —
never the reason text.
"""

import logging
from dataclasses import dataclass

from django.urls import reverse
from django.utils import timezone

from myapp.models import HelpRequest

logger = logging.getLogger(__name__)

CANCEL_REASON_MAX_LENGTH = 500

OK = "ok"
FORBIDDEN = "forbidden"
NOT_OPEN = "not_open"
REASON_REQUIRED = "reason_required"

CLIENT_CANCELLABLE = ("pending",)
STAFF_CANCELLABLE = ("pending", "active")
CLOSED_STATUSES = ("completed", "cancelled")


@dataclass(frozen=True)
class CancelOutcome:
    ok: bool
    code: str


def _is_staff(user):
    return bool(user.is_superuser or user.is_curator)


def cancellable_statuses(user, task):
    """Statuses from which ``user`` may cancel ``task`` ("" tuple = never)."""
    if not user.is_authenticated or not user.is_active:
        return ()
    if _is_staff(user):
        return STAFF_CANCELLABLE
    if user.is_client and task.client_id == user.pk:
        return CLIENT_CANCELLABLE
    return ()


def can_cancel(user, task):
    """True when the cancel control should be offered to ``user`` right now."""
    return task.status in cancellable_statuses(user, task)


def cancel_request(task, actor, reason=""):
    """Cancel ``task`` on behalf of ``actor``. Never raises for an expected
    failure — returns a ``CancelOutcome`` with one of the codes above."""
    reason = (reason or "").strip()[:CANCEL_REASON_MAX_LENGTH]
    allowed = cancellable_statuses(actor, task)
    if not allowed:
        return CancelOutcome(False, FORBIDDEN)
    if task.status in CLOSED_STATUSES:
        return CancelOutcome(False, NOT_OPEN)
    if task.status not in allowed:  # e.g. a client on an already-active task
        return CancelOutcome(False, FORBIDDEN)
    if _is_staff(actor) and not reason:
        return CancelOutcome(False, REASON_REQUIRED)

    now = timezone.now()
    updated = HelpRequest.objects.filter(pk=task.pk, status__in=allowed).update(
        status="cancelled",
        cancelled_at=now,
        cancelled_by=actor,
        cancel_reason=reason,
        updated_at=now,
    )
    if not updated:
        return CancelOutcome(False, NOT_OPEN)

    previous_status = task.status
    task.refresh_from_db()
    logger.info(
        "task #%s cancelled by user #%s (%s, was %s)",
        task.pk, actor.pk, "staff" if _is_staff(actor) else "client", previous_status,
    )
    _after_cancel(task, actor)
    return CancelOutcome(True, OK)


def _after_cancel(task, actor):
    from myapp.notifications import staff_recipients
    from myapp.realtime import publish
    from myapp.services import presence
    from myapp.services.notify import notify

    # The volunteer of an active task is free again.
    if task.volunteer_id:
        presence.on_task_change(task)

    # Milestone 2: tracking.end(task) — stop live location, delete the trail.
    # Milestone 3: chat.close(task) — the task chat becomes read-only.

    publish.task_changed(task)

    params = {
        "task_id": task.pk, "help_type": task.help_type,
        "reason": task.cancel_reason, "by_staff": _is_staff(actor),
    }
    link = reverse("task_detail", args=[task.pk])
    parties = [u for u in (task.client, task.volunteer) if u is not None and u.pk != actor.pk]
    if parties:
        notify(parties, "task_cancelled", params=params, link=link)
    if not _is_staff(actor):
        # A client withdrew a request staff may have been dispatching.
        notify(staff_recipients(), "task_cancelled", params=params, link=link)
