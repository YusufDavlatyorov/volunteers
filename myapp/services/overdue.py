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

from django.utils import timezone

from ..models import OVERDUE_THRESHOLD, HelpRequest
from ..notifications import notify_users, staff_recipients

logger = logging.getLogger(__name__)

# Identical to the message the manual admin button has always sent, so routing
# that button through this service is not an observable change.
OVERDUE_SUBJECT = "Просроченный запрос"


def _overdue_message(task):
    return f"Запрос #{task.id} в работе больше 3 часов. Волонтер: {task.volunteer}"


def find_overdue_tasks():
    """Active help requests accepted more than ``OVERDUE_THRESHOLD`` ago that
    have not yet been alerted on (``alarm_sent=False``). Read-only queryset."""
    cutoff = timezone.now() - OVERDUE_THRESHOLD
    return HelpRequest.objects.filter(
        status="active", alarm_sent=False, accepted_at__lt=cutoff
    ).select_related("volunteer")


def currently_overdue_tasks():
    """Every active help request past ``OVERDUE_THRESHOLD`` — regardless of
    ``alarm_sent``. This is the "what is overdue right now" view for the CRM /
    curator dashboard; ``find_overdue_tasks()`` is the narrower "still needs a
    first alert" set used by the sweep."""
    cutoff = timezone.now() - OVERDUE_THRESHOLD
    return (
        HelpRequest.objects.filter(status="active", accepted_at__lt=cutoff)
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

        delivered = notify_users(recipients, OVERDUE_SUBJECT, _overdue_message(task))
        if not delivered:
            # notify_users never raises (email is fail_silently, Telegram
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
