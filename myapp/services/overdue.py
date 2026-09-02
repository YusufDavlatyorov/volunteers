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


def sweep_overdue_tasks():
    """Alert curators/admins about every newly-overdue task, exactly once each,
    and flag it so a later run is a no-op. Returns the tasks alerted on this run.

    Each task is claimed with a conditional ``UPDATE ... WHERE alarm_sent=False``
    before its notification is sent, so two sweeps running at once (the cron job
    and the admin button, say) cannot produce a duplicate alert — the same idiom
    accept_task_view uses to settle a double-accept race.
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
        task.alarm_sent = True
        notify_users(recipients, OVERDUE_SUBJECT, _overdue_message(task))
        alerted.append(task)

    if alerted:
        logger.info("overdue sweep: alerted on %d task(s): %s", len(alerted), [t.id for t in alerted])
    return alerted
