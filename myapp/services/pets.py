"""Lost & Found pets — creation, safe serialisation, deterministic match hints.

Views stay thin: they check permissions and HTTP, then call one function here.
Model methods (``PetReport.mark_matched`` / ``reopen`` / ``resolve`` / ``close``)
own the state transitions; this module owns the cross-cutting parts:

  * creation + the one staff notification and the "you might have a match" pings
  * ``public_point`` / ``public_card`` — the ONLY shapes that reach a non-owner,
    non-staff viewer or the map JSON (never the reporter's identity or phone)
  * ``possible_matches`` — a small, deterministic "these might be the same
    animal" list (opposite type, same species, nearby or same region). It reuses
    ``geo.haversine_km``; it is NOT a second matching engine and never writes.

Notifications go through the shared ``notify_users`` / ``staff_recipients``
helpers, the same as every other domain here.
"""

import logging

from django.db import transaction
from django.urls import reverse

from ..models import PET_OPEN_STATUSES, PetReport
from ..notifications import notify_users, staff_recipients
from .geo import haversine_km, is_valid_coordinate

logger = logging.getLogger(__name__)

# A match candidate must be within this straight-line distance when both reports
# are located; otherwise the two must at least share a region.
MATCH_RADIUS_KM = 25
# Upper bound on how many opposite-type reports we look at per match query, so
# the suggestion stays cheap no matter how large the board grows.
MATCH_SCAN_CAP = 50
# How many of the new report's matches trigger a "you might have a match" ping.
MATCH_NOTIFY_CAP = 3

NEW_REPORT_SUBJECT = "Новое объявление о животном"
MATCH_HINT_SUBJECT = "Возможное совпадение по вашему объявлению"
STATUS_SUBJECT = "Статус вашего объявления обновлён"

_TYPE_WORD = {PetReport.REPORT_LOST: "потерянном", PetReport.REPORT_FOUND: "найденном"}
_STATUS_PHRASE = {
    PetReport.STATUS_OPEN: "снова открыто",
    PetReport.STATUS_MATCHED: "отмечено как возможное совпадение",
    PetReport.STATUS_RESOLVED: "закрыто: питомец воссоединён с хозяином",
    PetReport.STATUS_CLOSED: "закрыто",
}


# --------------------------------------------------------------------------
# creation
# --------------------------------------------------------------------------

def create_pet_report(*, reporter, report_type, description, species="other",
                      pet_name="", breed="", region="", latitude=None, longitude=None,
                      contact_phone="", image=None):
    """Create a ``PetReport`` (status ``open``), alert staff once, and ping the
    reporters of any obvious counterpart reports. Returns the report.

    Invalid coordinates are dropped (never stored) — the same rule the form and
    ``services.geo`` already enforce elsewhere.
    """
    if not (latitude is not None and longitude is not None and is_valid_coordinate(latitude, longitude)):
        latitude = longitude = None

    with transaction.atomic():
        report = PetReport.objects.create(
            reporter=reporter,
            report_type=report_type,
            pet_name=(pet_name or "").strip()[:120],
            species=species,
            breed=(breed or "").strip()[:120],
            description=(description or "").strip(),
            region=region or "",
            latitude=latitude,
            longitude=longitude,
            contact_phone=(contact_phone or "").strip()[:30],
            image=image or None,
        )

    _notify_staff_new(report)
    _notify_possible_match_owners(report)
    return report


def _notify_staff_new(report):
    recipients = list(staff_recipients())
    body = (
        f"{report.get_report_type_display()}: {report.display_name}"
        f"{f' ({report.breed})' if report.breed else ''}.\n"
        f"Регион: {report.get_region_display() or '—'}\n"
        f"Описание: {report.description[:300]}\n"
        f"Автор: {report.reporter.username}"
    )
    delivered = notify_users(recipients, NEW_REPORT_SUBJECT, body)
    logger.info("pet report #%s created by user %s (notified %d staff)", report.pk, report.reporter_id, delivered)


def _notify_possible_match_owners(report):
    seen = set()
    for match in possible_matches(report, limit=MATCH_NOTIFY_CAP):
        other = match["report"]
        if other.reporter_id == report.reporter_id or other.reporter_id in seen:
            continue
        seen.add(other.reporter_id)
        notify_users(
            [other.reporter],
            MATCH_HINT_SUBJECT,
            (
                f"По вашему объявлению #{other.pk} появилось новое объявление о "
                f"{_TYPE_WORD.get(report.report_type, '')} животном (#{report.pk}, "
                f"{report.get_species_display()}, {report.get_region_display() or 'регион не указан'}). "
                f"Проверьте, не совпадают ли они."
            ),
        )


# --------------------------------------------------------------------------
# state transitions (thin wrappers that also notify the reporter)
# --------------------------------------------------------------------------

ACTIONS = {
    "match": lambda r, actor: r.mark_matched(actor),
    "reopen": lambda r, actor: r.reopen(actor),
    "resolve": lambda r, actor: r.resolve(actor),
    "close": lambda r, actor: r.close(actor),
}
STAFF_ONLY_ACTIONS = ("match", "reopen")


def apply_action(report, action, *, actor, notify_reporter=True):
    """Run one status action (raises ``ValueError`` on an illegal move, exactly
    like the model method it wraps) and tell the reporter what changed — unless
    they are the one who did it. ``KeyError`` for an unknown action name."""
    handler = ACTIONS[action]
    handler(report, actor)
    if notify_reporter and actor != report.reporter:
        notify_users(
            [report.reporter],
            STATUS_SUBJECT,
            f"Ваше объявление #{report.pk} ({report.display_name}) {_STATUS_PHRASE.get(report.status, report.status)}.",
        )
    return report


# --------------------------------------------------------------------------
# read helpers
# --------------------------------------------------------------------------

def board_reports(*, report_type="", species="", region="", status=""):
    """The active Lost & Found board — open + matched reports, newest first,
    optionally narrowed. ``status`` is only honoured when it is one of the open
    statuses (the public board never lists resolved/closed reports)."""
    qs = PetReport.objects.select_related("reporter").order_by("-created_at")
    if status in PET_OPEN_STATUSES:
        qs = qs.filter(status=status)
    else:
        qs = qs.filter(status__in=PET_OPEN_STATUSES)
    if report_type:
        qs = qs.filter(report_type=report_type)
    if species:
        qs = qs.filter(species=species)
    if region:
        qs = qs.filter(region=region)
    return qs


def reports_for(reporter):
    """Every report a user has filed, any status — their own history view."""
    return (
        PetReport.objects.filter(reporter=reporter)
        .select_related("reviewed_by")
        .order_by("-created_at")
    )


def open_board_points():
    """Safe map markers for every open/matched, located report. Reusable by the
    shared map_data view; carries no reporter identity or contact detail."""
    located = (
        PetReport.objects.filter(status__in=PET_OPEN_STATUSES, latitude__isnull=False)
        .only("id", "report_type", "species", "pet_name", "breed", "region", "status", "latitude", "longitude")
    )
    return [public_point(report) for report in located]


def public_point(report):
    """Map marker dict — safe for any viewer. No reporter, no phone, no notes."""
    return {
        "id": report.pk,
        "kind": "pet",
        "lat": float(report.latitude),
        "lng": float(report.longitude),
        "title": f"{report.get_report_type_display()}: {report.display_name}",
        "subtitle": f"{report.get_species_display()} · {report.get_region_display() or '—'}",
        "report_type": report.report_type,
        "species": report.species,
        "region": report.region,
        "status": report.status,
        "url": reverse("pet_report_detail", args=[report.pk]),
    }


def public_card(report):
    """List-row dict — safe for any viewer."""
    return {
        "id": report.pk,
        "report_type": report.report_type,
        "report_type_display": report.get_report_type_display(),
        "species_display": report.get_species_display(),
        "display_name": report.display_name,
        "breed": report.breed,
        "region_display": report.get_region_display(),
        "status": report.status,
        "status_display": report.get_status_display(),
        "has_location": report.has_location,
        "has_photo": bool(report.image),
        "created_at": report.created_at,
    }


def possible_matches(report, *, limit=5):
    """Opposite-type, same-species open reports that could be the same animal:
    within ``MATCH_RADIUS_KM`` when both are located, otherwise sharing a region.
    Deterministic, read-only, bounded. Returns a list of
    ``{"report", "distance_km", "same_region"}`` dicts, nearest first."""
    opposite = PetReport.REPORT_FOUND if report.report_type == PetReport.REPORT_LOST else PetReport.REPORT_LOST
    candidates = (
        PetReport.objects.filter(
            report_type=opposite,
            status__in=PET_OPEN_STATUSES,
            species=report.species,
        )
        .exclude(pk=report.pk)
        .select_related("reporter")
        .order_by("-created_at", "-id")[:MATCH_SCAN_CAP]
    )

    located = report.has_location
    matches = []
    for other in candidates:
        same_region = bool(report.region) and report.region == other.region
        distance = None
        if located and other.has_location:
            distance = round(
                haversine_km(
                    float(report.latitude), float(report.longitude),
                    float(other.latitude), float(other.longitude),
                ),
                1,
            )
            if distance > MATCH_RADIUS_KM:
                continue
        elif not same_region:
            # No distance signal and different (or unknown) region — not a lead.
            continue
        matches.append({"report": other, "distance_km": distance, "same_region": same_region})

    # Nearest first; the ones with no distance (region-only) sort after, by the
    # queryset's newest-first order which they already carry.
    matches.sort(key=lambda m: (m["distance_km"] is None, m["distance_km"] if m["distance_km"] is not None else 0.0))
    return matches[:limit]
