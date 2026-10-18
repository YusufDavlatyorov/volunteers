"""Restore ``UserDailyActivity`` from the real timestamps already in the database
(``manage.py backfill_activity``) — never invents anything.

``SOURCES`` is the whole list of what counts: one (timestamp, the user who did
it) pair per row of an existing table. A day's ``actions_count`` is the number
of such events of that user on that local (Asia/Dushanbe) day, from
max(``activity.START_DATE``, registration day) to today. Days without an event
get no row — so an account with no real event gets no row at all. Only real
accounts (``is_demo=False``) and real rows. Events made *for* a person by an
import or by staff are not theirs: the registration of an imported account
(``IMPORT_EMAIL_DOMAIN``), an application created and approved in one go, a
location stamped with the account itself.

Recounted, never added up — a second run changes nothing:

* no row yet → created with ``source="backfill"``;
* a ``backfill`` row → set to the recount (deleted when its events are gone);
* a ``live`` row (DailyActivityMiddleware) → raised to the recount when that is
  higher, never lowered (``source="merged"``); a ``merged`` row likewise;
* demo rows (``is_demo=True``) and demo accounts are never read or written;
* nor are deactivated accounts (``is_active=False``) and their rows.

Photo reports are only read (their upload / review times).
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from dataclasses import dataclass, field

from django.apps import apps
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, F, Q
from django.db.models.functions import TruncDate
from django.utils import timezone

from . import activity

TZ = activity.TZ
SAME_EVENT = datetime.timedelta(minutes=5)
# Accounts created by the owner's batch imports (2026-10-18) carry a placeholder
# e-mail on this reserved domain — the one marker the database has for them (no
# "source" / "created_by" column; every such account and only those were made
# in the import batches). Their creation time is the import, not their action.
IMPORT_EMAIL_DOMAIN = "khayrkhoh.invalid"

# (label, "app.Model", timestamp field, user field, extra filter, count distinct instants)
# The user field is who did the thing at that moment. Work stages (en route /
# arrived / in progress) store no time, and live-tracking trail points are
# deleted when a request ends — neither can be restored.
SOURCES = [
    # Not for imported accounts: their date_joined is the moment of the import.
    ("registration", "accounts.Users", "date_joined", "id", ~Q(email__iendswith="@" + IMPORT_EMAIL_DOMAIN), False),
    ("last login", "accounts.Users", "last_login", "id", None, False),
    ("last seen on the site", "accounts.Profile", "last_seen_at", "user", None, False),
    # A location stamped with the account itself (registration / an import)
    # is part of that one event, not a second one.
    ("saved own location", "accounts.Profile", "location_updated_at", "user",
     Q(location_updated_at__gte=F("user__date_joined") + SAME_EVENT), False),
    ("live-tracking consent", "accounts.Profile", "tracking_consent_at", "user", None, False),
    ("request created", "myapp.HelpRequest", "created_at", "client", None, False),
    ("request accepted", "myapp.HelpRequest", "accepted_at", "volunteer", None, False),
    ("request completed", "myapp.HelpRequest", "completed_at", "volunteer", Q(status="completed"), False),
    ("request cancelled", "myapp.HelpRequest", "cancelled_at", "cancelled_by", None, False),
    ("live location sent", "myapp.HelpRequest", "volunteer_last_at", "volunteer", None, False),
    ("live tracking stopped", "myapp.HelpRequest", "tracking_stopped_at", "volunteer", None, False),
    ("review written", "myapp.TaskReview", "created_at", "client", None, False),
    ("review hidden (staff)", "myapp.TaskReview", "hidden_at", "hidden_by", None, False),
    ("chat message", "myapp.ChatMessage", "created_at", "sender", None, False),
    ("chat message checked (staff)", "myapp.ChatMessage", "reviewed_at", "reviewed_by", None, False),
    ("chat message hidden (staff)", "myapp.ChatMessage", "hidden_at", "hidden_by", None, False),
    ("chat read", "myapp.ChatReadState", "updated_at", "user", None, False),
    ("chat complaint", "myapp.ChatReport", "created_at", "reporter", None, False),
    ("chat complaint handled (staff)", "myapp.ChatReport", "handled_at", "handled_by", None, False),
    # "Mark all as read" stamps many rows with one instant: one event per instant.
    ("notifications read", "myapp.Notification", "read_at", "recipient", None, True),
    ("photo report uploaded", "myapp.PhotoReport", "created_at", "uploaded_by", None, False),
    ("photo report uploaded", "myapp.PhotoReport", "created_at", "author", Q(uploaded_by__isnull=True), False),
    ("photo report reviewed (staff)", "myapp.PhotoReport", "reviewed_at", "reviewed_by", None, False),
    # An application created and approved in one go (staff, e.g. for imported
    # accounts) was not sent by the person; one they sent waits for review.
    ("volunteer application sent", "myapp.VolunteerApplication", "created_at", "user",
     Q(reviewed_at__isnull=True) | Q(reviewed_at__gte=F("created_at") + SAME_EVENT), False),
    ("volunteer application reviewed (staff)", "myapp.VolunteerApplication", "reviewed_at", "reviewed_by", None, False),
    ("SOS raised", "myapp.EmergencyReport", "created_at", "volunteer", None, False),
    ("SOS acknowledged (staff)", "myapp.EmergencyReport", "acknowledged_at", "acknowledged_by", None, False),
    ("SOS resolved (staff)", "myapp.EmergencyReport", "resolved_at", "resolved_by", None, False),
    ("SOS cancelled", "myapp.EmergencyReport", "cancelled_at", "cancelled_by", None, False),
    ("donation offered", "myapp.Donation", "created_at", "donor", None, False),
    ("donation approved (staff)", "myapp.Donation", "approved_at", "reviewed_by", None, False),
    ("donation picked up", "myapp.Donation", "received_at", "assigned_volunteer", None, False),
    ("donation delivered", "myapp.Donation", "distributed_at", "assigned_volunteer", None, False),
    ("pet notice posted", "myapp.PetReport", "created_at", "reporter", None, False),
    ("pet notice matched (staff)", "myapp.PetReport", "matched_at", "reviewed_by", None, False),
    ("event created", "myapp.Event", "created_at", "curator", None, False),
    ("broadcast sent", "myapp.Broadcast", "created_at", "sender", None, False),
    ("community invite requested", "myapp.CommunityInvite", "created_at", "user", None, False),
    ("community invite used", "myapp.CommunityInvite", "used_at", "user", None, False),
    ("community joined", "myapp.CommunityMember", "joined_at", "user", None, False),
    ("community moderation (staff)", "myapp.ModerationAction", "created_at", "actor", None, False),
]


@dataclass
class Plan:
    today: datetime.date
    counts: dict = field(default_factory=dict)        # (user_id, day) -> events
    per_source: dict = field(default_factory=dict)    # label -> events
    create: list = field(default_factory=list)        # [(user_id, day, n)]
    update: list = field(default_factory=list)        # [(row, new_count, new_source)]
    delete: list = field(default_factory=list)        # [row]

    def per_user(self):
        """{user_id: (active days, events)} of the recount."""
        out = defaultdict(lambda: [0, 0])
        for (uid, _day), n in self.counts.items():
            out[uid][0] += 1
            out[uid][1] += n
        return {uid: tuple(v) for uid, v in out.items()}


def _real_users():
    """Real, active accounts. A deactivated one (e.g. a service account) is
    skipped entirely: no row created, changed or deleted."""
    return apps.get_model("accounts", "Users").objects.filter(is_demo=False, is_active=True)


def count_events(today=None) -> tuple[dict, dict]:
    """``({(user_id, day): events}, {source label: events})`` — one aggregate
    query per source, whatever the size of the history."""
    today = today or activity.local_today()
    start = datetime.datetime.combine(activity.START_DATE, datetime.time.min, tzinfo=TZ)
    end = min(datetime.datetime.combine(today + datetime.timedelta(days=1), datetime.time.min, tzinfo=TZ),
              timezone.now())
    first_day = {uid: max(activity.START_DATE, joined.astimezone(TZ).date())
                 for uid, joined in _real_users().values_list("id", "date_joined")}
    counts, per_source = defaultdict(int), defaultdict(int)
    for label, model_path, stamp, who, extra, distinct in SOURCES:
        model = apps.get_model(model_path)
        user_id = "id" if who == "id" else f"{who}_id"
        real = Q(is_demo=False, is_active=True) if who == "id" else Q(**{f"{who}__is_demo": False,
                                                                          f"{who}__is_active": True})
        rows = model.objects.filter(real, **{f"{stamp}__gte": start, f"{stamp}__lt": end})
        if who != "id":
            rows = rows.filter(**{f"{who}__isnull": False})
        if extra is not None:
            rows = rows.filter(extra)
        rows = (rows.annotate(day=TruncDate(stamp, tzinfo=TZ))
                .values(user_id, "day")
                .annotate(n=Count(stamp, distinct=True) if distinct else Count("pk"))
                .order_by())
        for row in rows:
            uid, day, n = row[user_id], row["day"], row["n"]
            if uid in first_day and first_day[uid] <= day <= today and n:
                counts[(uid, day)] += n
                per_source[label] += n
    return dict(counts), dict(per_source)


def plan(today=None) -> Plan:
    """What a run would write — reads only."""
    from myapp.models import UserDailyActivity

    today = today or activity.local_today()
    counts, per_source = count_events(today)
    result = Plan(today=today, counts=counts, per_source=per_source)
    existing = {(row.user_id, row.date): row for row in
                UserDailyActivity.objects.filter(is_demo=False, user__is_demo=False, user__is_active=True)}
    for key, n in sorted(counts.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        row = existing.get(key)
        if row is None:
            result.create.append((key[0], key[1], n))
        elif row.source == "backfill":
            if row.actions_count != n:
                result.update.append((row, n, "backfill"))
        elif n > row.actions_count:  # live / merged: never lowered
            result.update.append((row, n, "merged"))
    for key, row in existing.items():
        if row.source == "backfill" and key not in counts:
            result.delete.append(row)
    return result


def apply(result: Plan) -> dict:
    """Write a plan in one transaction. Returns the numbers."""
    from myapp.models import UserDailyActivity

    with transaction.atomic():
        UserDailyActivity.objects.bulk_create(
            [UserDailyActivity(user_id=uid, date=day, actions_count=n, is_demo=False, source="backfill")
             for uid, day, n in result.create],
            batch_size=500,
        )
        for row, n, source in result.update:
            row.actions_count, row.source = n, source
        UserDailyActivity.objects.bulk_update([row for row, _n, _s in result.update],
                                              ["actions_count", "source"], batch_size=500)
        if result.delete:
            UserDailyActivity.objects.filter(pk__in=[row.pk for row in result.delete]).delete()
    touched = {uid for uid, _d, _n in result.create} | {row.user_id for row, _n, _s in result.update} \
        | {row.user_id for row in result.delete}
    try:  # the heatmap caches 10 minutes per user and day
        cache.delete_many([activity.cache_key(uid, result.today) for uid in touched])
    except Exception:  # noqa: BLE001 — a cache outage must not fail the backfill
        pass
    return {"created": len(result.create), "updated": len(result.update), "deleted": len(result.delete)}
