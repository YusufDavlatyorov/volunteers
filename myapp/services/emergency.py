"""SOS / danger reports raised by a volunteer on an active help request.

Model methods (``EmergencyReport.acknowledge`` / ``resolve`` / ``cancel``) own
the state transitions. This module owns the cross-cutting parts:

  * deduplication — three layers: a fast check-then-create, a partial
    ``UniqueConstraint`` (``help_request``, ``volunteer``) over the open
    statuses that catches a cross-worker race, and an ``IntegrityError`` handler
    that turns the loser of that race into "return the existing row"
  * location resolution — reuses ``services.geo.is_valid_coordinate``
  * notification fan-out — typed ``services.notify.notify`` (in-app + WS +
    email/Telegram) to ``staff_recipients``, idempotent for the initial staff
    alert via ``EmergencyReport.notified_at`` (+ the ``sos:<id>`` dedupe key)
  * the live map — ``realtime.publish.sos_marker``: a ``sos.marker`` event to
    staff and the volunteers of the report's region (pulsing marker) while it
    is open/acknowledged, a removal when it ends; those volunteers also get one
    ``sos_nearby`` notification (no location, no names in it)

Views stay thin: parse the request, call one function here, redirect.
"""

import logging

from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from ..models import EmergencyReport
from ..notifications import staff_recipients, volunteer_queryset_for_region
from ..realtime import publish
from .notify import notify
from .notify_texts import CHANNEL_TEXTS
from .geo import is_valid_coordinate

logger = logging.getLogger(__name__)

NEW_SUBJECT = CHANNEL_TEXTS["sos_new"]["ru"][0]        # "⚠️ SOS: волонтёр сообщил об опасности"
UPDATE_SUBJECT = CHANNEL_TEXTS["sos_update"]["ru"][0]  # "Сигнал опасности: обновление"


def _resolve_location(help_request, volunteer, latitude, longitude):
    """Explicit valid coords -> linked task location -> volunteer profile
    location -> (None, None). Reuses the existing coordinate validator; adds no
    new location logic."""
    if latitude is not None and longitude is not None and is_valid_coordinate(latitude, longitude):
        return round(float(latitude), 6), round(float(longitude), 6)
    if help_request.has_location:
        return float(help_request.latitude), float(help_request.longitude)
    profile = getattr(volunteer, "profile", None)
    if profile and profile.has_location:
        return float(profile.latitude), float(profile.longitude)
    return None, None


def _notify_new(recipients, report, *, force=False):
    """The staff SOS alert (``sos_new``). In-app params carry ids + the
    volunteer's display name only; client phone/address/reason/location are
    channel-only (email/Telegram). The manual re-alert (``force``) skips the
    dedupe key so it really sends again."""
    hr = report.help_request
    return notify(
        recipients, "sos_new",
        params={"report_id": report.pk, "task_id": hr.id, "volunteer_name": report.volunteer.username,
                "region": report.region, "help_type": hr.help_type},
        link=reverse("emergency_detail", args=[report.pk]),
        dedupe_key=None if force else f"sos:{report.pk}",
        extra={
            "help_type_name": hr.get_help_type_display(),
            "client_name": hr.client.username,
            "phone": hr.phone,
            "region_name": report.get_region_display() or None,
            "address": hr.address,
            "location": f"{report.latitude}, {report.longitude}" if report.has_location else None,
            "reason": report.reason or None,
            "time": f"{timezone.localtime(report.created_at):%d.%m.%Y %H:%M}",
        },
    )


def _existing_active_report(volunteer, help_request):
    return (
        EmergencyReport.objects.filter(
            help_request=help_request,
            volunteer=volunteer,
            status__in=EmergencyReport.OPEN_STATUSES,
        )
        .order_by("-created_at")
        .first()
    )


def report_emergency(*, volunteer, help_request, reason="", latitude=None, longitude=None):
    """Create an open EmergencyReport for this volunteer + task, or return the
    volunteer's existing active one for the same task (deduplication — a repeated
    press never creates a second row or spams staff). Returns ``(report, created)``."""
    existing = _existing_active_report(volunteer, help_request)
    if existing:
        return existing, False

    lat, lng = _resolve_location(help_request, volunteer, latitude, longitude)
    try:
        with transaction.atomic():
            report = EmergencyReport.objects.create(
                help_request=help_request,
                volunteer=volunteer,
                reason=(reason or "").strip()[:2000],
                region=help_request.region or volunteer.region or "",
                latitude=lat,
                longitude=lng,
            )
    except IntegrityError:
        # A concurrent request (another Gunicorn worker) inserted first and the
        # partial UniqueConstraint rejected this one. Return the row that won.
        existing = _existing_active_report(volunteer, help_request)
        if existing:
            return existing, False
        raise
    notify_staff(report)
    publish.sos_marker(report)
    notify_region_volunteers(report)
    return report, True


def notify_region_volunteers(report):
    """One ``sos_nearby`` notification to the other active volunteers of the
    report's region (deduped per report). A report without a region reaches
    no volunteer — never "all regions". Returns the count reached."""
    if not report.region:
        return 0
    recipients = volunteer_queryset_for_region(report.region).exclude(pk=report.volunteer_id)
    return notify(
        recipients, "sos_nearby",
        params={"report_id": report.pk, "region": report.region},
        link=f"{reverse('map')}?sos={report.pk}",
        dedupe_key=f"sos_nearby:{report.pk}",
    )


def notify_staff(report, *, force=False):
    """Fan the report out to active curators + admins. Idempotent by default:
    ``report.notified_at`` is claimed with a conditional UPDATE, so a retry or a
    concurrent call sends at most one message. ``force=True`` is the manual
    "re-alert staff" path (used when the first delivery failed).

    ``notify`` never raises (email is ``fail_silently``; Telegram errors
    are caught) — it returns a delivered count. A zero count is logged as an
    error, but the report itself stays ``open`` and visible in the CRM and on
    the dashboard, so a failed push never hides the emergency.
    """
    if force:
        EmergencyReport.objects.filter(pk=report.pk).update(notified_at=timezone.now())
    else:
        claimed = EmergencyReport.objects.filter(pk=report.pk, notified_at__isnull=True).update(
            notified_at=timezone.now()
        )
        if not claimed:
            return False

    recipients = list(staff_recipients())
    delivered = _notify_new(recipients, report, force=force)
    if recipients and not delivered:
        logger.error(
            "emergency #%s: staff SOS notification reached 0 of %d recipient(s) "
            "(email/Telegram unavailable). The report stays visible in the CRM.",
            report.pk, len(recipients),
        )
    else:
        logger.info(
            "emergency #%s reported by user %s on task #%s (notified %d recipient(s))",
            report.pk, report.volunteer_id, report.help_request_id, delivered,
        )
    return True


def realert_staff(report):
    """Manual re-send of the initial staff alert for a still-open report."""
    if not report.is_open:
        return False
    return notify_staff(report, force=True)


def _notify_reporter(report, action):
    notify(
        [report.volunteer], "sos_update", variant=action,
        params={"report_id": report.pk, "task_id": report.help_request_id, "action": action},
        link=reverse("emergency_detail", args=[report.pk]),
        dedupe_key=f"sos_update:{report.pk}:{action}",
    )


def _log_transition(report, action, actor):
    logger.info(
        "emergency #%s %s by user #%s (task #%s, status=%s)",
        report.pk, action, getattr(actor, "pk", None), report.help_request_id, report.status,
    )


def acknowledge(report, *, actor):
    report.acknowledge(actor)
    _log_transition(report, "acknowledged", actor)
    publish.sos_marker(report)          # status changes, the marker stays
    _notify_reporter(report, "acknowledged")
    return report


def resolve(report, *, actor, note=""):
    report.resolve(actor, note=note)
    _log_transition(report, "resolved", actor)
    publish.sos_marker(report)          # removal: the location stops being shared
    _notify_reporter(report, "resolved")
    return report


def cancel(report, *, actor, note=""):
    report.cancel(actor, note=note)
    _log_transition(report, "cancelled", actor)
    publish.sos_marker(report)          # removal
    _notify_reporter(report, "cancelled")
    return report


def open_reports():
    """Reports still needing attention (open or acknowledged), newest first."""
    return (
        EmergencyReport.objects.filter(status__in=EmergencyReport.OPEN_STATUSES)
        .select_related("help_request", "help_request__client", "volunteer")
        .order_by("-created_at")
    )
