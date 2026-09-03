"""Detection and alerting for help requests that have waited too long for a
volunteer.

``STALE_PENDING_THRESHOLD`` (myapp.models.help_requests) — 48 hours — is the
single definition of "waiting too long". This is the *pending*-side SLA, the
mirror of ``myapp.services.overdue`` (which watches tasks already in progress).
Both entry points call ``sweep_stale_pending()`` so the alerting logic lives in
exactly one place:

  * the ``check_stale_requests`` command -> run from cron (see README)

Idempotent via ``HelpRequest.stale_alert_sent``: each stale request is alerted
on at most once, so the command is safe to run on a short interval. The claim is
a conditional ``UPDATE ... WHERE stale_alert_sent=False`` — the same idiom the
overdue sweep and accept_task_view use to settle a race.
"""

import logging

from django.utils import timezone

from ..models import STALE_PENDING_THRESHOLD, HelpRequest
from ..notifications import notify_users, staff_recipients

logger = logging.getLogger(__name__)

STALE_SUBJECT = "Запрос долго ждёт волонтёра"


def _stale_message(task):
    waited_hours = int((timezone.now() - task.created_at).total_seconds() // 3600)
    return (
        f"Запрос #{task.id} ({task.get_help_type_display()}) в регионе "
        f"{task.get_region_display() or '—'} ждёт волонтёра уже {waited_hours} ч. "
        f"и до сих пор не принят. Клиент: {task.client.username}, телефон: {task.phone}."
    )


def find_stale_pending():
    """Pending help requests created more than ``STALE_PENDING_THRESHOLD`` ago
    that have not yet been alerted on (``stale_alert_sent=False``). Read-only
    queryset — the sweep's "still needs a first alert" set."""
    cutoff = timezone.now() - STALE_PENDING_THRESHOLD
    return (
        HelpRequest.objects.filter(
            status="pending", stale_alert_sent=False, created_at__lt=cutoff
        )
        .select_related("client")
        .order_by("created_at")
    )


def currently_stale_pending():
    """Every pending help request past ``STALE_PENDING_THRESHOLD`` — regardless
    of ``stale_alert_sent``. The "what is stuck right now" view for the CRM /
    curator dashboard; ``find_stale_pending()`` is the narrower "still needs a
    first alert" set used by the sweep."""
    cutoff = timezone.now() - STALE_PENDING_THRESHOLD
    return (
        HelpRequest.objects.filter(status="pending", created_at__lt=cutoff)
        .select_related("client")
        .order_by("created_at")
    )


def sweep_stale_pending():
    """Alert curators/admins about every request that has newly gone stale,
    exactly once each, and flag it so a later run is a no-op. Returns the
    requests alerted on this run.

    Each request is claimed with a conditional ``UPDATE ... WHERE
    stale_alert_sent=False`` before its notification is sent, so two sweeps
    running at once cannot produce a duplicate alert.
    """
    stale = list(find_stale_pending())
    if not stale:
        return []

    recipients = list(staff_recipients())
    alerted = []
    for task in stale:
        claimed = HelpRequest.objects.filter(pk=task.pk, stale_alert_sent=False).update(
            stale_alert_sent=True
        )
        if not claimed:
            continue  # another concurrent sweep already took this one
        task.stale_alert_sent = True
        notify_users(recipients, STALE_SUBJECT, _stale_message(task))
        alerted.append(task)

    if alerted:
        logger.info(
            "stale-pending sweep: alerted on %d request(s): %s",
            len(alerted), [t.id for t in alerted],
        )
    return alerted
