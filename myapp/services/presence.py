"""System-computed volunteer availability (Feature Pack v2, feature 3).

`Profile.availability_status` stays a *stored* field (matching, the CRM lists,
the map and the analytics breakdown keep reading it), but nobody sets it by hand
anymore — this module is the only writer:

- ``busy``      — the volunteer has an ``active`` task;
- ``available`` — approved volunteer, no active task, seen within
  ``settings.AVAILABILITY_IDLE_MINUTES`` (default 30);
- ``offline``   — not seen for longer than that (or never).

"Seen" = an authenticated page view (``PresenceMiddleware``), a WS
connect/heartbeat, or a Telegram bot interaction — all through ``mark_seen``,
which writes ``last_seen_at`` at most once per user per
``SEEN_WRITE_INTERVAL_SECONDS`` (Django cache throttle). Task events call
``on_task_change``; the ``sweep_availability`` command (5-min cron) handles the
"went quiet" transition and expires do-not-disturb end dates.

Do-not-disturb (``Profile.is_away`` + optional ``away_until``) is deliberately
NOT an availability status: it only removes the volunteer from matching and from
new-task notifications (``not_away_q``), so staff still see the real presence.

Every status change publishes ``availability.changed {user_id, status, away}`` to
the ``staff`` group — IDs only, never names/phones/emails.
"""

import datetime
import logging

from django.conf import settings
from django.core.cache import cache
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

logger = logging.getLogger(__name__)

SEEN_WRITE_INTERVAL_SECONDS = 60
SOURCES = frozenset({"web", "ws", "telegram"})

AVAILABLE = "available"
BUSY = "busy"
OFFLINE = "offline"


def not_away_q(prefix="profile__"):
    """Q for users whose do-not-disturb is NOT in effect right now (an expired
    `away_until` counts as off). `prefix` is the path from the queried model to
    Profile: "profile__" from Users, "" from Profile."""
    now = timezone.now()
    return Q(**{f"{prefix}is_away": False}) | Q(**{f"{prefix}away_until__lte": now})


def idle_threshold():
    return datetime.timedelta(minutes=getattr(settings, "AVAILABILITY_IDLE_MINUTES", 30))


def compute_status(*, is_volunteer, has_active_task, last_seen_at, now):
    """Pure rule: busy if an active task; available if a volunteer seen within
    the idle threshold; offline otherwise (incl. never seen)."""
    if has_active_task:
        return BUSY
    if is_volunteer and last_seen_at is not None and now - last_seen_at <= idle_threshold():
        return AVAILABLE
    return OFFLINE


def _publish(user_id, status, away):
    from myapp.realtime import publish

    publish.send_to_staff(
        "availability.changed", {"user_id": user_id, "status": status, "away": bool(away)}
    )


def publish_availability(profile, *, now=None):
    """Tell staff about a volunteer's current status + do-not-disturb flag
    (e.g. after a profile-form edit of the away fields)."""
    now = now or timezone.now()
    away = profile.is_away and (profile.away_until is None or profile.away_until > now)
    _publish(profile.user_id, profile.availability_status, away)


def _sync_cached_profile(user, **values):
    """Keep an already-loaded `user.profile` consistent with a queryset update."""
    cached = getattr(user, "_state", None) and user._state.fields_cache.get("profile")
    if cached is not None:
        for name, value in values.items():
            setattr(cached, name, value)


def _has_active_task(user_id):
    from myapp.models import HelpRequest

    return HelpRequest.objects.filter(volunteer_id=user_id, status="active").exists()


def recompute(user, *, now=None):
    """Recompute and store one volunteer's availability. Saves (and publishes
    `availability.changed` to staff) only when the value actually changes.
    Returns the status, or None for a non-volunteer / missing profile."""
    from accounts.models import Profile

    if user is None or not getattr(user, "pk", None) or not getattr(user, "is_volunteer", False):
        return None
    row = (
        Profile.objects.filter(user_id=user.pk)
        .values("pk", "availability_status", "last_seen_at", "is_away", "away_until")
        .first()
    )
    if row is None:
        return None
    now = now or timezone.now()
    status = compute_status(
        is_volunteer=True,
        has_active_task=_has_active_task(user.pk),
        last_seen_at=row["last_seen_at"],
        now=now,
    )
    if status != row["availability_status"]:
        # Conditional UPDATE: of two concurrent recomputes reaching the same
        # answer, only one publishes.
        changed = (
            Profile.objects.filter(pk=row["pk"])
            .exclude(availability_status=status)
            .update(availability_status=status)
        )
        if changed:
            away = row["is_away"] and (row["away_until"] is None or row["away_until"] > now)
            logger.debug("availability user #%s %s -> %s", user.pk, row["availability_status"], status)
            _publish(user.pk, status, away)
    _sync_cached_profile(user, availability_status=status)
    return status


def mark_seen(user, *, source):
    """Record a sign of life. At most one `last_seen_at` DB write per user per
    SEEN_WRITE_INTERVAL_SECONDS (one cache op otherwise); recomputes only when
    being seen can change the status, i.e. the volunteer is stored as offline.
    Returns True when it wrote `last_seen_at`."""
    from accounts.models import Profile

    if source not in SOURCES:
        raise ValueError(f"unknown presence source: {source!r}")
    if user is None or not getattr(user, "pk", None):
        return False
    if not cache.add(f"presence:seen:{user.pk}", source, SEEN_WRITE_INTERVAL_SECONDS):
        return False
    now = timezone.now()
    if not Profile.objects.filter(user_id=user.pk).update(last_seen_at=now):
        return False
    _sync_cached_profile(user, last_seen_at=now)
    if getattr(user, "is_volunteer", False):
        stored = (
            Profile.objects.filter(user_id=user.pk)
            .values_list("availability_status", flat=True)
            .first()
        )
        if stored == OFFLINE:
            recompute(user, now=now)
    return True


def on_task_change(task):
    """Recompute the task's volunteer after accept / assign / complete / cancel."""
    from accounts.models import Users

    if not getattr(task, "volunteer_id", None):
        return None
    volunteer = Users.objects.filter(pk=task.volunteer_id).first()
    return recompute(volunteer)


def _away_until_from_date(value):
    """A calendar date means "away through that whole day": until the start of
    the next local day."""
    start = datetime.datetime.combine(value + datetime.timedelta(days=1), datetime.time.min)
    return timezone.make_aware(start)


def set_away(user, away, until=None):
    """Switch do-not-disturb on/off for a volunteer. `until` (datetime, or a
    date = through that day) must be in the future; clearing resets it.
    Raises ValueError("not_volunteer" | "until_in_past")."""
    from accounts.models import Profile

    if not getattr(user, "is_volunteer", False):
        raise ValueError("not_volunteer")
    away = bool(away)
    if not away:
        until = None
    elif until is not None:
        if isinstance(until, datetime.date) and not isinstance(until, datetime.datetime):
            until = _away_until_from_date(until)
        elif timezone.is_naive(until):
            until = timezone.make_aware(until)
        if until <= timezone.now():
            raise ValueError("until_in_past")
    Profile.objects.filter(user_id=user.pk).update(is_away=away, away_until=until)
    _sync_cached_profile(user, is_away=away, away_until=until)
    status = (
        Profile.objects.filter(user_id=user.pk).values_list("availability_status", flat=True).first()
    )
    logger.info("do-not-disturb user #%s -> %s", user.pk, "on" if away else "off")
    _publish(user.pk, status, away)
    return away, until


class SweepResult(int):
    """An int (total changes, per the §10 contract) that also carries the
    per-transition counts."""

    def __new__(cls, *, became_offline=0, became_available=0, became_busy=0, away_cleared=0):
        obj = super().__new__(cls, became_offline + became_available + became_busy + away_cleared)
        obj.became_offline = became_offline
        obj.became_available = became_available
        obj.became_busy = became_busy
        obj.away_cleared = away_cleared
        return obj

    def as_dict(self):
        return {
            "became_offline": self.became_offline,
            "became_available": self.became_available,
            "became_busy": self.became_busy,
            "away_cleared": self.away_cleared,
        }


def sweep(*, dry_run=False, now=None):
    """Periodic consistency pass (the `sweep_availability` cron, every 5 min):

    - volunteers with no active task, not seen within the threshold → offline
      (the "went quiet" transition no event announces);
    - repairs drift both ways (busy without an active task, active task but not
      busy, seen recently but stored offline);
    - expired `away_until` → `is_away=False`, `away_until=None`.

    A handful of set-based queries; per-row work is only the staff publish for
    the rows that actually changed."""
    from accounts.models import Profile
    from myapp.models import HelpRequest

    now = now or timezone.now()
    cutoff = now - idle_threshold()
    active_task = HelpRequest.objects.filter(volunteer_id=OuterRef("user_id"), status="active")
    volunteers = Profile.objects.filter(user__is_volunteer=True).annotate(_busy=Exists(active_task))
    seen_recently = Q(last_seen_at__gte=cutoff)

    targets = {
        BUSY: volunteers.filter(_busy=True),
        AVAILABLE: volunteers.filter(_busy=False).filter(seen_recently),
        OFFLINE: volunteers.filter(_busy=False).exclude(seen_recently),
    }
    counts = {}
    for status, qs in targets.items():
        rows = list(qs.exclude(availability_status=status).values_list("pk", "user_id", "is_away", "away_until"))
        changed = 0
        if rows and not dry_run:
            changed = (
                Profile.objects.filter(pk__in=[r[0] for r in rows])
                .exclude(availability_status=status)
                .update(availability_status=status)
            )
            for _pk, user_id, is_away, away_until in rows:
                _publish(user_id, status, is_away and (away_until is None or away_until > now))
        counts[status] = len(rows) if dry_run else changed

    expired = list(
        Profile.objects.filter(is_away=True, away_until__lte=now).values_list("pk", "user_id", "availability_status")
    )
    away_cleared = len(expired)
    if expired and not dry_run:
        away_cleared = (
            Profile.objects.filter(pk__in=[r[0] for r in expired], is_away=True, away_until__lte=now)
            .update(is_away=False, away_until=None)
        )
        for _pk, user_id, status in expired:
            _publish(user_id, status, False)

    result = SweepResult(
        became_offline=counts[OFFLINE],
        became_available=counts[AVAILABLE],
        became_busy=counts[BUSY],
        away_cleared=away_cleared,
    )
    if not dry_run and result:
        logger.info("availability sweep %s", result.as_dict())
    return result
