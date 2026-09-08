"""SOS / danger reports raised by a volunteer on an active help request.

Model methods (``EmergencyReport.acknowledge`` / ``resolve`` / ``cancel``) own
the state transitions. This module owns the cross-cutting parts:

  * deduplication — three layers: a fast check-then-create, a partial
    ``UniqueConstraint`` (``help_request``, ``volunteer``) over the open
    statuses that catches a cross-worker race, and an ``IntegrityError`` handler
    that turns the loser of that race into "return the existing row"
  * location resolution — reuses ``services.geo.is_valid_coordinate``
  * notification fan-out — through the shared ``notify_users`` /
    ``staff_recipients`` infrastructure (email + Telegram), idempotent for the
    initial staff alert via ``EmergencyReport.notified_at``

Views stay thin: parse the request, call one function here, redirect.
"""

import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

from ..models import EmergencyReport
from ..notifications import notify_users, staff_recipients
from .geo import is_valid_coordinate

logger = logging.getLogger(__name__)

NEW_SUBJECT = "⚠️ SOS: волонтёр сообщил об опасности"
UPDATE_SUBJECT = "Сигнал опасности: обновление"


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


def _new_body(report):
    hr = report.help_request
    location = f"{report.latitude}, {report.longitude}" if report.has_location else "не указано"
    return (
        f"Волонтёр {report.volunteer.username} сообщил об опасности во время работы.\n\n"
        f"Запрос #{hr.id} — {hr.get_help_type_display()}\n"
        f"Клиент: {hr.client.username}, телефон: {hr.phone}\n"
        f"Регион: {report.get_region_display() or '—'}\n"
        f"Адрес: {hr.address}\n"
        f"Местоположение сигнала: {location}\n"
        f"Причина: {report.reason or '—'}\n"
        f"Время: {timezone.localtime(report.created_at):%d.%m.%Y %H:%M}\n"
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
    return report, True


def notify_staff(report, *, force=False):
    """Fan the report out to active curators + admins. Idempotent by default:
    ``report.notified_at`` is claimed with a conditional UPDATE, so a retry or a
    concurrent call sends at most one message. ``force=True`` is the manual
    "re-alert staff" path (used when the first delivery failed).

    ``notify_users`` never raises (email is ``fail_silently``; Telegram errors
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
    delivered = notify_users(recipients, NEW_SUBJECT, _new_body(report))
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


def _notify_reporter(report, phrase):
    notify_users(
        [report.volunteer],
        UPDATE_SUBJECT,
        f"Ваш сигнал по запросу #{report.help_request_id} {phrase}.",
    )


def _log_transition(report, action, actor):
    logger.info(
        "emergency #%s %s by user #%s (task #%s, status=%s)",
        report.pk, action, getattr(actor, "pk", None), report.help_request_id, report.status,
    )


def acknowledge(report, *, actor):
    report.acknowledge(actor)
    _log_transition(report, "acknowledged", actor)
    _notify_reporter(report, "принят координатором")
    return report


def resolve(report, *, actor, note=""):
    report.resolve(actor, note=note)
    _log_transition(report, "resolved", actor)
    _notify_reporter(report, "закрыт координатором")
    return report


def cancel(report, *, actor, note=""):
    report.cancel(actor, note=note)
    _log_transition(report, "cancelled", actor)
    _notify_reporter(report, "отменён координатором")
    return report


def open_reports():
    """Reports still needing attention (open or acknowledged), newest first."""
    return (
        EmergencyReport.objects.filter(status__in=EmergencyReport.OPEN_STATUSES)
        .select_related("help_request", "help_request__client", "volunteer")
        .order_by("-created_at")
    )
