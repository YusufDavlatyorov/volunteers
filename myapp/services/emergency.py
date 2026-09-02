"""SOS / danger reports raised by a volunteer on an active help request.

Model methods (``EmergencyReport.acknowledge`` / ``resolve`` / ``cancel``) own
the state transitions. This module owns the cross-cutting parts:

  * deduplication — a second press never creates a second row or a second alert
  * location resolution — reuses ``services.geo.is_valid_coordinate``
  * notification fan-out — through the shared ``notify_users`` /
    ``staff_recipients`` infrastructure (email + Telegram), idempotent for the
    initial staff alert via ``EmergencyReport.notified_at``

Views stay thin: parse the request, call one function here, redirect.
"""

import logging

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


def report_emergency(*, volunteer, help_request, reason="", latitude=None, longitude=None):
    """Create an open EmergencyReport for this volunteer + task, or return the
    volunteer's existing open one for the same task (deduplication — a repeated
    press never spams staff). Returns ``(report, created)``."""
    existing = (
        EmergencyReport.objects.filter(
            help_request=help_request,
            volunteer=volunteer,
            status__in=EmergencyReport.OPEN_STATUSES,
        )
        .order_by("-created_at")
        .first()
    )
    if existing:
        return existing, False

    lat, lng = _resolve_location(help_request, volunteer, latitude, longitude)
    report = EmergencyReport.objects.create(
        help_request=help_request,
        volunteer=volunteer,
        reason=(reason or "").strip()[:2000],
        region=help_request.region or volunteer.region or "",
        latitude=lat,
        longitude=lng,
    )
    notify_staff(report)
    return report, True


def notify_staff(report):
    """Fan the report out to active curators + admins, exactly once. Idempotent
    via ``report.notified_at`` — a conditional UPDATE claims the send, so calling
    this twice (retry, future "re-alert" button) sends at most one message."""
    claimed = EmergencyReport.objects.filter(pk=report.pk, notified_at__isnull=True).update(
        notified_at=timezone.now()
    )
    if not claimed:
        return False
    notify_users(list(staff_recipients()), NEW_SUBJECT, _new_body(report))
    logger.info(
        "emergency #%s reported by user %s on task #%s",
        report.pk, report.volunteer_id, report.help_request_id,
    )
    return True


def _notify_reporter(report, phrase):
    notify_users(
        [report.volunteer],
        UPDATE_SUBJECT,
        f"Ваш сигнал по запросу #{report.help_request_id} {phrase}.",
    )


def acknowledge(report, *, actor):
    report.acknowledge(actor)
    _notify_reporter(report, "принят координатором")
    return report


def resolve(report, *, actor, note=""):
    report.resolve(actor, note=note)
    _notify_reporter(report, "закрыт координатором")
    return report


def cancel(report, *, actor, note=""):
    report.cancel(actor, note=note)
    _notify_reporter(report, "отменён координатором")
    return report


def open_reports():
    """Reports still needing attention (open or acknowledged), newest first."""
    return (
        EmergencyReport.objects.filter(status__in=EmergencyReport.OPEN_STATUSES)
        .select_related("help_request", "help_request__client", "volunteer")
        .order_by("-created_at")
    )
