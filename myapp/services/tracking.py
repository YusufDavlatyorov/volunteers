"""Live volunteer location during an active task (Feature Pack v2, feature 7).

Contract: FEATURES_PLAN §12. Never raises for an expected failure — every
rejection is a ``PointOutcome`` code the WS consumer / HTTP view / Telegram bot
relay as-is.

Rules enforced here (one copy for every transport):

- Only the **assigned volunteer** of an **active** task, after their one-time
  **consent** (``Profile.tracking_consent_at``), may share a position.
- Every point is validated (``geo.is_valid_coordinate``); points with an
  accuracy worse than ``MAX_ACCURACY_M``, an implied speed above
  ``MAX_SPEED_KMH`` versus the last accepted point (a GPS jump) or arriving less
  than ``MIN_INTERVAL_SECONDS`` after it are dropped. A jump is accepted after
  all when the next point agrees with it (two mutually consistent points beat
  one stale reference) — otherwise one bad first fix would block the volunteer
  for good.
- The latest accepted point lives on the task (``HelpRequest.volunteer_last_*``)
  plus a short trail (``TaskLocationPoint``). The trail is deleted when the task
  ends (``end``) or the volunteer stops sharing (``stop``), and never kept longer
  than ``TRAIL_MAX_AGE`` (``purge`` / ``purge_tracking`` hourly cron).
- Payloads published over WS carry ids, coordinates, timestamps and accuracy
  only — never a name, phone or email. Who receives them is the realtime
  layer's job (``access.can_see_location`` re-checked per socket).
"""

from __future__ import annotations

import datetime
import logging
import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from django.core.cache import cache
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from myapp.realtime import events, publish

from . import access, presence
from .geo import haversine_km, is_valid_coordinate
from .notify import notify

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_ACCURACY_M", "MAX_SPEED_KMH", "MIN_INTERVAL_SECONDS", "TRAIL_MAX_AGE", "TRAIL_LIMIT",
    "OK", "FORBIDDEN", "INVALID", "INACCURATE", "JUMP", "TOO_SOON",
    "PointOutcome", "give_consent", "revoke_consent", "has_consent", "can_share",
    "accept_point", "stop", "end", "latest", "trail", "purge", "live_markers",
    "location_payload", "marker_payload",
]

MAX_ACCURACY_M = 200
MAX_SPEED_KMH = 150
MIN_INTERVAL_SECONDS = 5
TRAIL_MAX_AGE = datetime.timedelta(hours=24)
TRAIL_LIMIT = 500
# A client-supplied ``recorded_at`` (e.g. a Telegram live-location edit date)
# is trusted only inside this window; otherwise the server clock is used.
RECORDED_AT_MAX_SKEW = datetime.timedelta(minutes=2)
# A rejected jump stays a "candidate" this long: if the next point agrees with
# it, the old reference was the outlier.
JUMP_CANDIDATE_SECONDS = 300

OK = "ok"
FORBIDDEN = "forbidden"
INVALID = "invalid"
INACCURATE = "inaccurate"
JUMP = "jump"
TOO_SOON = "too_soon"

EVENT_LOCATION_UPDATED = "location.updated"
EVENT_TRACKING_STOPPED = "tracking.stopped"
EVENT_MAP_MARKER = "map.marker"

_STORED_SOURCE = {"web": "web", "ws": "web", "http": "web", "telegram": "telegram"}
_PRESENCE_SOURCE = {"web": "web", "ws": "ws", "http": "web", "telegram": "telegram"}
_Q6 = Decimal("0.000001")


@dataclass(frozen=True)
class PointOutcome:
    ok: bool
    code: str
    payload: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# consent
# --------------------------------------------------------------------------- #

def _profile_qs(user):
    from accounts.models import Profile

    return Profile.objects.filter(user_id=getattr(user, "pk", None))


def _sync_profile(user, value):
    cached = getattr(user, "_state", None) and user._state.fields_cache.get("profile")
    if cached is not None:
        cached.tracking_consent_at = value


def has_consent(user) -> bool:
    if user is None or not getattr(user, "pk", None):
        return False
    return _profile_qs(user).filter(tracking_consent_at__isnull=False).exists()


def give_consent(user) -> bool:
    """Record the volunteer's consent (first answer wins; idempotent).
    Returns True when consent is in place afterwards."""
    if user is None or not getattr(user, "pk", None) or not getattr(user, "is_volunteer", False):
        return False
    now = timezone.now()
    if _profile_qs(user).filter(tracking_consent_at__isnull=True).update(tracking_consent_at=now):
        _sync_profile(user, now)
        logger.info("tracking consent given user=%s", user.pk)
    return has_consent(user)


def revoke_consent(user) -> bool:
    """Withdraw consent and stop every live share of this volunteer at once.
    Returns True when consent was actually withdrawn."""
    if user is None or not getattr(user, "pk", None):
        return False
    changed = bool(_profile_qs(user).filter(tracking_consent_at__isnull=False)
                   .update(tracking_consent_at=None))
    _sync_profile(user, None)
    if changed:
        logger.info("tracking consent revoked user=%s", user.pk)
        from myapp.models import HelpRequest

        for task in HelpRequest.objects.filter(volunteer_id=user.pk, status="active"):
            stop(user, task, reason="consent_revoked")
    return changed


def can_share(user, task) -> bool:
    """Assigned volunteer of an active task who has given consent."""
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    if not getattr(user, "is_volunteer", False):
        return False
    if task is None or task.status != "active" or task.volunteer_id != user.pk:
        return False
    return has_consent(user)


# --------------------------------------------------------------------------- #
# payloads (the only shapes that leave this module)
# --------------------------------------------------------------------------- #

def _iso(value):
    return value.isoformat() if value else None


# Payload shapes are defined once, in myapp/realtime/events.py (the module the
# WS no-PII tests inspect); these only normalise the numbers.

def location_payload(task_id, lat, lng, ts, accuracy=None) -> dict:
    return events.location_updated_payload(
        int(task_id), round(float(lat), 6), round(float(lng), 6), ts,
        accuracy=None if accuracy is None else round(float(accuracy)),
    )


def marker_payload(task_id, lat=None, lng=None, ts=None, *, removed=False) -> dict:
    if removed:
        return events.volunteer_live_marker_removed_payload(int(task_id))
    return events.volunteer_live_marker_payload(int(task_id), round(float(lat), 6), round(float(lng), 6), ts)


# --------------------------------------------------------------------------- #
# accepting points
# --------------------------------------------------------------------------- #

def _num(value):
    """float or None; non-finite / garbage → None."""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _recorded_at(value, now):
    if isinstance(value, str):
        try:
            value = datetime.datetime.fromisoformat(value)
        except ValueError:
            value = None
    if isinstance(value, datetime.datetime):
        if timezone.is_naive(value):
            value = timezone.make_aware(value, datetime.timezone.utc)
        if now - RECORDED_AT_MAX_SKEW <= value <= now + datetime.timedelta(seconds=5):
            return min(value, now)
    return now


def _speed_kmh(a_lat, a_lng, a_ts, b_lat, b_lng, b_ts):
    seconds = abs((b_ts - a_ts).total_seconds())
    km = haversine_km(float(a_lat), float(a_lng), float(b_lat), float(b_lng))
    if seconds <= 0:
        return math.inf if km > 0.05 else 0.0
    return km / (seconds / 3600)


def _candidate_key(task_id):
    return f"tracking:jump:{task_id}"


def _is_jump(task, lat, lng, recorded_at):
    """True when the point implies an impossible speed vs the last accepted
    point — unless it agrees with the previous rejected one."""
    if task.volunteer_last_lat is None or task.volunteer_last_at is None:
        return False
    speed = _speed_kmh(task.volunteer_last_lat, task.volunteer_last_lng, task.volunteer_last_at,
                       lat, lng, recorded_at)
    if speed <= MAX_SPEED_KMH:
        cache.delete(_candidate_key(task.pk))
        return False
    candidate = cache.get(_candidate_key(task.pk))
    if candidate:
        c_lat, c_lng, c_ts = candidate
        c_ts = datetime.datetime.fromisoformat(c_ts)
        if (recorded_at - c_ts).total_seconds() >= MIN_INTERVAL_SECONDS and \
                _speed_kmh(c_lat, c_lng, c_ts, lat, lng, recorded_at) <= MAX_SPEED_KMH:
            cache.delete(_candidate_key(task.pk))
            return False  # two consistent points: the stored reference was the outlier
    cache.set(_candidate_key(task.pk), (lat, lng, recorded_at.isoformat()), JUMP_CANDIDATE_SECONDS)
    return True


def _q6(value):
    return Decimal(str(value)).quantize(_Q6, rounding=ROUND_HALF_UP)


def accept_point(volunteer, task_id, lat, lng, *, accuracy=None, speed=None, heading=None,
                 source="web", recorded_at=None) -> PointOutcome:
    """Validate and store one position of ``volunteer`` for task ``task_id``,
    then publish it. See the module docstring for the rules."""
    from myapp.models import HelpRequest, TaskLocationPoint

    try:
        task_pk = int(task_id)
    except (TypeError, ValueError):
        return PointOutcome(False, INVALID)
    task = HelpRequest.objects.filter(pk=task_pk).first()
    if task is None or not can_share(volunteer, task):
        return PointOutcome(False, FORBIDDEN)

    if not is_valid_coordinate(lat, lng) or not (math.isfinite(float(lat)) and math.isfinite(float(lng))):
        return PointOutcome(False, INVALID)
    lat, lng = round(float(lat), 6), round(float(lng), 6)
    accuracy = _num(accuracy)
    if accuracy is not None and accuracy < 0:
        return PointOutcome(False, INVALID)
    if accuracy is not None and accuracy > MAX_ACCURACY_M:
        return PointOutcome(False, INACCURATE)
    speed = _num(speed)
    if speed is not None and speed < 0:
        speed = None
    heading = _num(heading)
    if heading is not None and not 0 <= heading <= 360:
        heading = None

    now = timezone.now()
    recorded_at = _recorded_at(recorded_at, now)
    last_at = task.volunteer_last_at
    if last_at is not None and (recorded_at - last_at).total_seconds() < MIN_INTERVAL_SECONDS:
        return PointOutcome(False, TOO_SOON)
    if _is_jump(task, lat, lng, recorded_at):
        return PointOutcome(False, JUMP)

    # Conditional UPDATE: still active + still this volunteer's task, and no
    # other point was accepted in the meantime (two transports racing).
    guard = Q(volunteer_last_at__isnull=True) | Q(
        volunteer_last_at__lte=recorded_at - datetime.timedelta(seconds=MIN_INTERVAL_SECONDS)
    )
    updated = (
        HelpRequest.objects.filter(pk=task.pk, status="active", volunteer_id=volunteer.pk)
        .filter(guard)
        .update(volunteer_last_lat=_q6(lat), volunteer_last_lng=_q6(lng),
                volunteer_last_at=recorded_at, tracking_stopped_at=None)
    )
    if not updated:
        still_active = HelpRequest.objects.filter(
            pk=task.pk, status="active", volunteer_id=volunteer.pk
        ).exists()
        return PointOutcome(False, TOO_SOON if still_active else FORBIDDEN)

    TaskLocationPoint.objects.create(
        task_id=task.pk, volunteer_id=volunteer.pk, latitude=_q6(lat), longitude=_q6(lng),
        accuracy_m=accuracy, speed_mps=speed, heading=heading,
        source=_STORED_SOURCE.get(source, "web"), recorded_at=recorded_at,
    )

    payload = location_payload(task.pk, lat, lng, recorded_at, accuracy)
    publish.send_to_task(task.pk, EVENT_LOCATION_UPDATED, payload)
    publish.send_to_map_staff(EVENT_MAP_MARKER, marker_payload(task.pk, lat, lng, recorded_at))
    try:
        presence.mark_seen(volunteer, source=_PRESENCE_SOURCE.get(source, "web"))
    except Exception:  # noqa: BLE001 — presence is best-effort here
        logger.warning("tracking: presence.mark_seen failed user=%s", volunteer.pk)
    return PointOutcome(True, OK, payload)


# --------------------------------------------------------------------------- #
# stopping / ending
# --------------------------------------------------------------------------- #

def _delete_trail(task_id):
    from myapp.models import TaskLocationPoint

    deleted, _ = TaskLocationPoint.objects.filter(task_id=task_id).delete()
    cache.delete(_candidate_key(task_id))
    return deleted


def _publish_stopped(task_id, reason):
    publish.send_to_task(task_id, EVENT_TRACKING_STOPPED, events.tracking_stopped_payload(int(task_id), reason))
    publish.send_to_map_staff(EVENT_MAP_MARKER, marker_payload(task_id, removed=True))


def stop(volunteer, task, reason="volunteer") -> bool:
    """The volunteer stops sharing (button, WS ``tracking.stop``, consent
    withdrawn). Sets ``tracking_stopped_at``, clears the latest position,
    deletes the trail, publishes ``tracking.stopped`` and tells staff once per
    stop. Returns False when ``volunteer`` isn't the assigned volunteer of this
    active task."""
    from myapp.models import HelpRequest
    from myapp.notifications import staff_recipients

    if volunteer is None or task is None or task.volunteer_id != getattr(volunteer, "pk", None):
        return False
    if task.status != "active":
        return False
    now = timezone.now()
    changed = (
        HelpRequest.objects.filter(pk=task.pk, tracking_stopped_at__isnull=True)
        .update(tracking_stopped_at=now, volunteer_last_lat=None, volunteer_last_lng=None,
                volunteer_last_at=None)
    )
    task.tracking_stopped_at = task.tracking_stopped_at or now
    task.volunteer_last_lat = task.volunteer_last_lng = task.volunteer_last_at = None
    _delete_trail(task.pk)
    _publish_stopped(task.pk, reason)
    if changed:
        logger.info("tracking stopped task=%s volunteer=%s reason=%s", task.pk, volunteer.pk, reason)
        notify(staff_recipients(), "tracking_stopped", params={"task_id": task.pk},
               link=reverse("task_detail", args=[task.pk]))
    return True


def end(task, reason="task_ended") -> int:
    """The task completed / was cancelled: delete the trail, clear the latest
    position, publish ``tracking.stopped``. Idempotent; returns the number of
    trail points deleted."""
    from myapp.models import HelpRequest

    if task is None or not getattr(task, "pk", None):
        return 0
    HelpRequest.objects.filter(pk=task.pk).exclude(
        volunteer_last_lat__isnull=True, volunteer_last_lng__isnull=True, volunteer_last_at__isnull=True,
    ).update(volunteer_last_lat=None, volunteer_last_lng=None, volunteer_last_at=None)
    task.volunteer_last_lat = task.volunteer_last_lng = task.volunteer_last_at = None
    deleted = _delete_trail(task.pk)
    if task.volunteer_id:  # a pending task never had anyone sharing a location
        _publish_stopped(task.pk, reason)
    if deleted:
        logger.info("tracking ended task=%s points_deleted=%d", task.pk, deleted)
    return deleted


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #

def latest(task):
    """``{lat, lng, ts}`` of the volunteer's latest shared position, or None
    (not active, never shared, stopped). Callers check
    ``access.can_see_location`` first."""
    if task is None or task.status != "active" or task.volunteer_last_at is None \
            or task.volunteer_last_lat is None or task.volunteer_last_lng is None:
        return None
    return {
        "lat": round(float(task.volunteer_last_lat), 6),
        "lng": round(float(task.volunteer_last_lng), 6),
        "ts": _iso(task.volunteer_last_at),
    }


def trail(task, limit=TRAIL_LIMIT):
    """The newest ``limit`` trail points, oldest first: ``[{lat, lng, ts}]``.
    Empty unless the task is active."""
    from myapp.models import TaskLocationPoint

    if task is None or task.status != "active":
        return []
    limit = max(1, min(int(limit or TRAIL_LIMIT), TRAIL_LIMIT))
    rows = list(
        TaskLocationPoint.objects.filter(task_id=task.pk)
        .order_by("-recorded_at")
        .values_list("latitude", "longitude", "recorded_at")[:limit]
    )
    rows.reverse()
    return [{"lat": round(float(a), 6), "lng": round(float(b), 6), "ts": _iso(ts)} for a, b, ts in rows]


def live_markers(limit=200):
    """Current live volunteer positions for the staff ops map's initial load
    (``map.marker`` keeps them fresh afterwards). Same shape as the WS event.
    Staff-only data: the caller gates on curator/admin."""
    from myapp.models import HelpRequest

    rows = (
        HelpRequest.objects.filter(status="active", volunteer_last_at__isnull=False,
                                   volunteer_last_lat__isnull=False, volunteer_last_lng__isnull=False,
                                   volunteer_last_at__gte=timezone.now() - TRAIL_MAX_AGE)
        .order_by("-volunteer_last_at")
        .values_list("pk", "volunteer_last_lat", "volunteer_last_lng", "volunteer_last_at")[:limit]
    )
    return [marker_payload(pk, lat, lng, ts) for pk, lat, lng, ts in rows]


def can_see(user, task) -> bool:
    """Alias of the shared rule, for callers that only import this module."""
    return access.can_see_location(user, task)


# --------------------------------------------------------------------------- #
# retention
# --------------------------------------------------------------------------- #

def purge(max_age=TRAIL_MAX_AGE, *, dry_run=False, now=None) -> int:
    """Retention sweep (``purge_tracking``, hourly cron): delete trail points
    older than ``max_age`` and every point of a task that is no longer active,
    and clear latest positions left on ended tasks or older than ``max_age``
    (e.g. a task closed through the Django admin, which skips ``end``).
    Returns the number of trail points deleted (or that would be)."""
    from myapp.models import HelpRequest, TaskLocationPoint

    now = now or timezone.now()
    cutoff = now - max_age
    points = TaskLocationPoint.objects.filter(Q(created_at__lt=cutoff) | Q(recorded_at__lt=cutoff)
                                              | ~Q(task__status="active"))
    stale_latest = HelpRequest.objects.filter(volunteer_last_at__isnull=False).filter(
        ~Q(status="active") | Q(volunteer_last_at__lt=cutoff)
    )
    if dry_run:
        return points.count()
    deleted, _ = points.delete()
    cleared = stale_latest.update(volunteer_last_lat=None, volunteer_last_lng=None, volunteer_last_at=None)
    if deleted or cleared:
        logger.info("tracking purge points_deleted=%d latest_cleared=%d", deleted, cleared)
    return deleted
