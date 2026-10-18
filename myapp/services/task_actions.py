"""Task and coordination actions shared by the website and the Telegram bot
(Feature Pack v2, milestone 5 — extracted from myapp/views.py, behaviour
unchanged). One copy of each rule:

* ``create_request(client, form)`` — save a validated ``HelpRequestForm``
  (region = client's, best-effort geocode when no pin) and ping the region's
  volunteers (do-not-disturb excluded).
* ``accept(volunteer, task)`` — self-accept: per-volunteer cache lock, "one
  active task" rule, conditional ``UPDATE … WHERE status='pending'``.
* ``complete(volunteer, task)`` — conditional ``UPDATE … WHERE status='active'``
  (a double tap can't complete twice or award points twice), then tracking end,
  chat close, presence, client notice, review prompt; points + badges only
  with an approved photo report (else ``reward_pending``, services/photo_reports).
* ``assign(staff, task, volunteer, confirm_away=False)`` — direct dispatch with
  the same guard as accept; a do-not-disturb volunteer needs ``confirm_away``.
* ``approve_application`` / ``reject_application`` — admin review.
* ``send_broadcast(sender, region, subject, message)`` — staff broadcast.

Every function returns an ``Outcome(ok, code, …)`` (or the created object) and
logs IDs only. Views and bot handlers map the codes to their own wording.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from ..models import Broadcast, HelpRequest, VolunteerApplication
from ..notifications import volunteer_queryset_for_region
from ..realtime import publish
from . import maps, presence
from .notify import notify

logger = logging.getLogger(__name__)

OK = "ok"
BUSY = "busy"                 # the volunteer already holds an active task (or a parallel accept)
TAKEN = "taken"               # someone else accepted / it is no longer pending
NOT_ACTIVE = "not_active"
NOT_PENDING = "not_pending"
AWAY = "volunteer_away"
FORBIDDEN = "forbidden"
ALREADY = "already"

# accept()'s per-volunteer critical section. A HelpRequest-level DB constraint
# was deliberately ruled out: CRM/admin dispatch and matching's workload
# scoring legitimately model a volunteer holding several active rows — "one
# active task" is a rule of the self-service accept flow only.
ACCEPT_TASK_LOCK_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class Outcome:
    ok: bool
    code: str
    task: HelpRequest | None = None
    override: bool = False
    reward_pending: bool = False  # complete(): points/badges wait for an approved photo report


def _ping_region(task):
    """New-task ping to the region's volunteers (do-not-disturb excluded)."""
    volunteers = volunteer_queryset_for_region(task.region).filter(presence.not_away_q())
    return notify(
        volunteers, "task_new_in_region",
        params={"task_id": task.id, "region": task.region, "help_type": task.help_type},
        link=reverse("task_detail", args=[task.id]),
        dedupe_key=f"task_new:{task.id}",
        extra={"region_name": task.get_region_display() or None, "description": task.description[:180]},
    )


def create_request(client, form) -> HelpRequest:
    """``form`` is a valid ``HelpRequestForm``. The request takes the client's
    region; an address without a pin is geocoded best-effort."""
    help_request = form.save(commit=False)
    help_request.client = client
    help_request.region = client.region
    # A demo account (seed_demo / a demo stand) only ever makes demo requests.
    help_request.is_demo = bool(getattr(client, "is_demo", False))
    if not help_request.has_location and help_request.address:
        coords = maps.geocode(help_request.address, region=help_request.region)
        if coords:
            help_request.latitude, help_request.longitude = coords
    help_request.save()
    logger.info("help request #%s created by client #%s", help_request.id, client.pk)
    # Deliveries queued (0 = nobody reachable) — the website's confirmation
    # must not claim volunteers were told when none were.
    help_request.volunteers_notified = _ping_region(help_request)
    return help_request


def accept(volunteer, task) -> Outcome:
    lock_key = f"accept_task_lock:{volunteer.pk}"
    # cache.add() is atomic: of two concurrent accepts by the same volunteer
    # only one gets past here; the other is turned away immediately.
    if not cache.add(lock_key, "1", ACCEPT_TASK_LOCK_TIMEOUT_SECONDS):
        return Outcome(False, BUSY, task)
    try:
        if HelpRequest.objects.filter(volunteer=volunteer, status="active").exists():
            return Outcome(False, BUSY, task)
        # Conditional UPDATE: of two volunteers accepting the same task at the
        # same moment only the first still sees status="pending".
        updated = HelpRequest.objects.filter(pk=task.pk, status="pending").update(
            volunteer=volunteer, status="active", accepted_at=timezone.now(),
            work_stage=HelpRequest.WORK_STAGE_ASSIGNED,
        )
    finally:
        cache.delete(lock_key)
    if not updated:
        return Outcome(False, TAKEN, task)

    task.refresh_from_db()
    logger.info("task #%s self-accepted by volunteer #%s", task.id, volunteer.pk)
    presence.on_task_change(task)
    publish.task_changed(task)
    notify(
        [task.client, volunteer], "task_accepted",
        params={"task_id": task.id, "volunteer_name": volunteer.username},
        link=reverse("task_detail", args=[task.id]),
        dedupe_key=f"task_accepted:{task.id}:{volunteer.pk}",
        extra={"client_name": task.client.username, "phone": task.phone, "address": task.address},
    )
    return Outcome(True, OK, task)


def complete(volunteer, task) -> Outcome:
    """Complete an active task. The reward (points + badges) is paid now only
    when an approved photo report already exists; otherwise the task waits with
    ``reward_pending`` and ``photo_reports.credit_task`` pays on approval.
    ``Outcome.reward_pending`` tells the caller which happened."""
    from . import chat, feedback, photo_reports, tracking

    now = timezone.now()
    approved = photo_reports.has_approved_report(task, volunteer)
    completed = HelpRequest.objects.filter(pk=task.pk, volunteer=volunteer, status="active").update(
        status="completed", completed_at=now, updated_at=now, reward_pending=True
    )
    if not completed:
        return Outcome(False, NOT_ACTIVE, task)

    task.refresh_from_db()
    logger.info("task #%s completed by volunteer #%s", task.id, volunteer.pk)
    tracking.end(task)  # stop live location, delete the trail
    chat.close(task)  # the task chat becomes read-only (system message)
    presence.on_task_change(task)
    publish.task_changed(task)
    notify(
        [task.client], "task_completed", params={"task_id": task.id},
        link=reverse("task_detail", args=[task.id]), dedupe_key=f"task_completed:{task.id}",
    )
    feedback.review_request(task)  # "Оцените помощь" prompt to the client
    # Re-checked after the UPDATE too: a report approved between the check
    # above and the UPDATE found the task still active and credited nothing.
    if approved or photo_reports.has_approved_report(task, volunteer):
        photo_reports.credit_task(task)  # points + "task_completed" badges, once
    task.refresh_from_db(fields=["reward_pending"])
    return Outcome(True, OK, task, reward_pending=task.reward_pending)


def assign(staff, task, volunteer, *, confirm_away=False) -> Outcome:
    from . import dispatch

    override = dispatch.is_away_now(volunteer)
    if override and not confirm_away:
        return Outcome(False, AWAY, task, override=True)
    updated = HelpRequest.objects.filter(pk=task.pk, status="pending").update(
        volunteer=volunteer, status="active", accepted_at=timezone.now(),
        work_stage=HelpRequest.WORK_STAGE_ASSIGNED,
    )
    if not updated:
        return Outcome(False, NOT_PENDING, task)

    logger.info(
        "task #%s assigned to volunteer #%s by user #%s (direct dispatch%s)",
        task.id, volunteer.pk, staff.pk, ", do-not-disturb override" if override else "",
    )
    task.refresh_from_db()
    presence.on_task_change(task)
    publish.task_changed(task)
    common = dict(
        params={"task_id": task.id, "help_type": task.help_type},
        link=reverse("task_detail", args=[task.id]),
        dedupe_key=f"task_assigned:{task.id}:{volunteer.pk}",
        extra={"help_type_name": task.get_help_type_display(), "client_name": task.client.username,
               "phone": task.phone, "address": task.address},
    )
    if override:
        # The volunteer learns this was an urgent manual assignment despite
        # do-not-disturb; the client gets the ordinary "assigned" notice.
        notify([volunteer], "task_assigned", variant="urgent_override", **common)
        notify([task.client], "task_assigned", **common)
    else:
        notify([volunteer, task.client], "task_assigned", **common)
    return Outcome(True, OK, task, override=override)


def approve_application(admin, application) -> Outcome:
    if application.status == VolunteerApplication.STATUS_APPROVED:
        return Outcome(False, ALREADY)
    application.approve(admin)
    logger.info(
        "volunteer application #%s approved by user #%s (applicant #%s)",
        application.pk, admin.pk, application.user_id,
    )
    notify([application.user], "application_approved", link=reverse("task_list"))
    return Outcome(True, OK)


def reject_application(admin, application) -> Outcome:
    if application.status == VolunteerApplication.STATUS_REJECTED:
        return Outcome(False, ALREADY)
    application.reject(admin)
    logger.info(
        "volunteer application #%s rejected by user #%s (applicant #%s)",
        application.pk, admin.pk, application.user_id,
    )
    notify([application.user], "application_rejected", link=reverse("volunteer_application"))
    # Lost the volunteer role → out of the volunteer community chats too.
    from . import community

    community.revoke_safely(application.user, reason="application_rejected")
    return Outcome(True, OK)


def send_broadcast(sender, *, region, subject, message) -> Broadcast:
    broadcast = Broadcast(sender=sender, region=region or "all", subject=subject, message=message)
    volunteers = volunteer_queryset_for_region(broadcast.region)
    broadcast.sent_count = notify(
        volunteers, "broadcast",
        params={"subject": broadcast.subject, "message": broadcast.message[:300]},
        link=reverse("broadcast_list"),
        subject=broadcast.subject, message=broadcast.message,
    )
    broadcast.save()
    logger.info("broadcast #%s sent by user #%s region=%s reached=%s",
                broadcast.pk, sender.pk, broadcast.region, broadcast.sent_count)
    from . import community

    community.mirror_broadcast_safely(broadcast)
    return broadcast
