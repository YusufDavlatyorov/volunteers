"""Detection and alerting for help requests that have been active too long.

``OVERDUE_THRESHOLD`` (myapp.models.help_requests) — 3 hours — is the single
definition of "too long". Both entry points call ``sweep_overdue_tasks()`` so
the alerting logic lives in exactly one place:

  * the admin "check overdue" button -> myapp.views.check_overdue_view
  * the ``check_overdue_tasks`` command -> run from cron (see README)

Idempotent via ``HelpRequest.alarm_sent``: each overdue task is alerted on at
most once, so the command is safe to run on a short interval.
"""

import logging

from django.db.models import F, Q
from django.utils import timezone

from ..models import OVERDUE_THRESHOLD, HelpRequest
from django.urls import reverse

from ..notifications import staff_recipients
from .notify import notify
from .notify_texts import CHANNEL_TEXTS

logger = logging.getLogger(__name__)

# Identical to the message the manual admin button has always sent, so routing
# that button through this service is not an observable change.
OVERDUE_SUBJECT = CHANNEL_TEXTS["task_overdue"]["ru"][0]  # "Просроченный запрос"


def currently_overdue_q():
    """The predicate for "active and past OVERDUE_THRESHOLD right now", as a Q so
    every reader (this module's querysets, the CRM ``crm/tasks/?overdue=1``
    filter, analytics) applies the exact same rule against the one threshold —
    no re-inlined ``accepted_at__lt=now - OVERDUE_THRESHOLD`` copies to drift."""
    return Q(status="active", accepted_at__lt=timezone.now() - OVERDUE_THRESHOLD)


def was_overdue_q(now=None):
    """Historical counterpart for the CRM trend charts (services/analytics_v2):
    a task that *went* past OVERDUE_THRESHOLD between accept and completion,
    or is still active past it. Same one threshold as ``currently_overdue_q``."""
    now = now or timezone.now()
    return Q(accepted_at__isnull=False) & (
        Q(completed_at__gt=F("accepted_at") + OVERDUE_THRESHOLD)
        | Q(status="active", accepted_at__lt=now - OVERDUE_THRESHOLD)
    )


def _notify_overdue(recipients, task):
    """One typed ``task_overdue`` notification to staff; returns recipients
    reached (0 = nobody). dedupe_key makes a retried claim harmless."""
    return notify(
        recipients, "task_overdue",
        params={"task_id": task.id},
        link=reverse("task_detail", args=[task.id]),
        dedupe_key=f"overdue:{task.id}:{int(task.accepted_at.timestamp()) if task.accepted_at else 0}",
        extra={"volunteer_str": str(task.volunteer)},
    )


def find_overdue_tasks():
    """Active help requests accepted more than ``OVERDUE_THRESHOLD`` ago that
    have not yet been alerted on (``alarm_sent=False``). Read-only queryset."""
    return HelpRequest.objects.filter(
        currently_overdue_q(), alarm_sent=False
    ).select_related("volunteer")


def currently_overdue_tasks():
    """Every active help request past ``OVERDUE_THRESHOLD`` — regardless of
    ``alarm_sent``. This is the "what is overdue right now" view for the CRM /
    curator dashboard; ``find_overdue_tasks()`` is the narrower "still needs a
    first alert" set used by the sweep."""
    return (
        HelpRequest.objects.filter(currently_overdue_q())
        .select_related("client", "volunteer")
        .order_by("accepted_at")
    )


def sweep_overdue_tasks():
    """Alert curators/admins about every newly-overdue task, exactly once each,
    and flag it so a later run is a no-op. Returns the tasks actually alerted
    on this run.

    Each task is claimed with a conditional ``UPDATE ... WHERE alarm_sent=False``
    before its notification is sent, so two sweeps running at once (the cron job
    and the admin button, say) cannot produce a duplicate alert — the same idiom
    accept_task_view uses to settle a double-accept race. If delivery reaches
    zero recipients (no active curator/admin, or email/Telegram both down), the
    claim is released instead of kept, so the task is not permanently marked
    alerted for an alert nobody received — the next scheduled run retries it.
    """
    overdue = list(find_overdue_tasks())
    if not overdue:
        return []

    recipients = list(staff_recipients())
    alerted = []
    for task in overdue:
        claimed = HelpRequest.objects.filter(pk=task.pk, alarm_sent=False).update(alarm_sent=True)
        if not claimed:
            continue  # another concurrent sweep already took this one

        delivered = _notify_overdue(recipients, task)
        if not delivered:
            # notify never raises (email is fail_silently, Telegram
            # errors are caught) — a return of 0 is the only failure signal we
            # get. alarm_sent stayed True for the whole window above, so no
            # concurrent sweep could have claimed this row meanwhile; it's
            # safe to release it now for a later run to retry.
            HelpRequest.objects.filter(pk=task.pk).update(alarm_sent=False)
            logger.error(
                "overdue sweep: task #%s reached 0 of %d recipient(s) (no active "
                "curator/admin, or email/Telegram unavailable) — alarm_sent reset "
                "for retry on the next run.",
                task.pk, len(recipients),
            )
            continue

        task.alarm_sent = True
        alerted.append(task)

    if alerted:
        logger.info("overdue sweep: alerted on %d task(s): %s", len(alerted), [t.id for t in alerted])
    return alerted
