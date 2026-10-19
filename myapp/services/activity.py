"""Activity calendar (GitHub contribution graph): what a volunteer or client
did, per day, over the last ``WEEKS`` (53) weeks — always the full grid,
whatever the registration date (owner 2026-10-19): Monday-first columns from
``grid_start`` (the Monday 52 weeks before this week's) to today; days after
today are not drawn, days before registration are ordinary empty days.

Two sources, merged per day (2026-10-18):

* the rows the user's actions left (below), one query per kind;
* ``UserDailyActivity.actions_count`` — the per-day counter written by
  ``DailyActivityMiddleware`` (``record_request``) and, for demo accounts, by
  ``seed_demo``. One more query.

A day's count is the larger of the two: the counter already includes the site
actions that left those rows, so they are never counted twice; what the
counter has on top shows as "other actions on the site" (kind ``site``).

One query per action kind (``TruncDate`` in Asia/Dushanbe + ``Count``), summed
by date in Python, so the query count is fixed per role (volunteer 8, client 5,
the counter included) whatever the number of days. The result is cached for ``CACHE_SECONDS`` per
user and per local day; a new action shows up within that window.

Action kinds (owner-approved 2026-10-09):
    volunteer — accepted / completed a request, photo report, donation
                picked up (``received_at``) and delivered (``distributed_at``,
                a second action when both dates are set);
    client    — created a request (cancelled ones too), left a review
                (hidden ones too);
    both      — offered a donation, reported a pet.
Curators and admins get no calendar (``shows_calendar``). There is no event
participation model, and volunteers don't review clients — neither is counted.

Text: the page is rendered in Russian (``SERVER_RENDER_LANGUAGE``) from the
i18n.js table itself (``translation_table``); static/js/activity.js re-labels
it in the visitor's language with ``Intl.PluralRules``.
"""

from __future__ import annotations

import datetime
import json
from collections import defaultdict
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, models, transaction
from django.db.models import Count, F
from django.db.models.functions import TruncDate
from django.utils import timezone

from server.i18n_prerender import translation_table

__all__ = [
    "TZ", "DAYS", "WEEKS", "START_DATE", "CACHE_SECONDS", "KINDS", "shows_calendar", "daily_counts", "grid_start",
    "level", "plural_category_ru", "calendar", "cache_key", "record_request", "summary",
]

TZ = ZoneInfo("Asia/Dushanbe")
DAYS = 365
WEEKS = 53  # always: this week and the 52 before it
# backfill_activity never restores a day before this (the platform's demo
# season) — the calendar itself always shows the full WEEKS.
START_DATE = datetime.date(2026, 9, 1)
CACHE_SECONDS = 10 * 60
LEVELS = 4

# Display order of the per-kind breakdown in tooltips.
KINDS = (
    "completed", "accepted", "photo_report", "donation_received", "donation_distributed",
    "request_created", "review", "donation_offered", "pet_report", "site",
)
# A day was marked for the user (DailyActivityMiddleware): cache flag lifetime.
SEEN_FLAG_SECONDS = 36 * 3600


def shows_calendar(user) -> bool:
    """Only volunteers and clients (never curators/admins)."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser or getattr(user, "is_curator", False):
        return False
    return bool(getattr(user, "is_volunteer", False) or getattr(user, "is_client", False))


def _sources(user):
    """(kind, queryset, field) for every action this user can take — a
    DateTimeField (bucketed by its Asia/Dushanbe day) or a DateField (as is)."""
    from myapp.models import Donation, HelpRequest, PetReport, PhotoReport, TaskReview

    out = []
    if user.is_volunteer:
        out += [
            ("accepted", HelpRequest.objects.filter(volunteer=user), "accepted_at"),
            ("completed", HelpRequest.objects.filter(volunteer=user, status="completed"), "completed_at"),
            # The day the help took place, not the upload day (a DateField).
            ("photo_report", PhotoReport.objects.filter(author=user), "event_date"),
            ("donation_received", Donation.objects.filter(assigned_volunteer=user), "received_at"),
            ("donation_distributed", Donation.objects.filter(assigned_volunteer=user), "distributed_at"),
        ]
    if user.is_client:
        out += [
            ("request_created", HelpRequest.objects.filter(client=user), "created_at"),
            ("review", TaskReview.objects.filter(client=user), "created_at"),
        ]
    out += [
        ("donation_offered", Donation.objects.filter(donor=user), "created_at"),
        ("pet_report", PetReport.objects.filter(reporter=user), "created_at"),
    ]
    return out


def local_today(now=None) -> datetime.date:
    return (now or timezone.now()).astimezone(TZ).date()


def grid_start(today: datetime.date) -> datetime.date:
    """The Monday ``WEEKS - 1`` weeks before the Monday of today's week."""
    return today - datetime.timedelta(days=today.weekday(), weeks=WEEKS - 1)


def window(today: datetime.date, joined=None):
    """First and last day (inclusive) of the calendar: ``grid_start`` → today.
    ``joined`` is accepted for old callers and ignored — the grid is always
    full; days before registration simply have no activity."""
    return grid_start(today), today


def daily_counts(user, today: datetime.date | None = None) -> dict:
    """{date: {kind: n}} for the window. One aggregate query per kind."""
    today = today or local_today()
    first, last = window(today, getattr(user, "date_joined", None))
    start = datetime.datetime.combine(first, datetime.time.min, tzinfo=TZ)
    end = datetime.datetime.combine(last + datetime.timedelta(days=1), datetime.time.min, tzinfo=TZ)
    days: dict = defaultdict(dict)
    for kind, queryset, field in _sources(user):
        if isinstance(queryset.model._meta.get_field(field), models.DateTimeField):
            bucketed = queryset.filter(**{f"{field}__gte": start, f"{field}__lt": end}).annotate(
                day=TruncDate(field, tzinfo=TZ)
            )
        else:  # already a local day
            bucketed = queryset.filter(**{f"{field}__gte": first, f"{field}__lte": last}).annotate(day=F(field))
        rows = (
            bucketed
            .values("day")
            .annotate(n=Count("pk"))
            .order_by()
        )
        for row in rows:
            if row["day"] is not None and row["n"]:
                days[row["day"]][kind] = days[row["day"]].get(kind, 0) + row["n"]
    # The per-day counter: whatever it has beyond the rows above is "site".
    from myapp.models import UserDailyActivity

    for day, n in UserDailyActivity.objects.filter(user=user, date__gte=first, date__lte=last).values_list(
        "date", "actions_count"
    ):
        extra = n - sum(days[day].values()) if day in days else n
        if extra > 0:
            days[day]["site"] = extra
    return {day: kinds for day, kinds in days.items() if kinds}


def level(count: int, maximum: int) -> int:
    """0 for no actions, else 1–4 by quartile of the user's own yearly max:
    ≤ ¼·max → 1, ≤ ½·max → 2, ≤ ¾·max → 3, above → 4."""
    if count <= 0 or maximum <= 0:
        return 0
    for lvl in range(1, LEVELS + 1):
        if count * LEVELS <= maximum * lvl:
            return lvl
    return LEVELS


def plural_category_ru(n: int) -> str:
    """CLDR Russian plural category (what Intl.PluralRules('ru') returns)."""
    if n % 10 == 1 and n % 100 != 11:
        return "one"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "few"
    return "many"


def cache_key(user_id, today) -> str:
    return f"activity:v3:{user_id}:{today.isoformat()}"


def summary(counts: dict, first: datetime.date, today: datetime.date) -> dict:
    """``{"active_days", "streak"}`` over the window: days with at least one
    action, and the current run of such days — ending today, or yesterday
    while today has nothing yet (the day isn't over)."""
    active = {day for day, kinds in counts.items() if first <= day <= today and sum(kinds.values()) > 0}
    day = today if today in active else today - datetime.timedelta(days=1)
    streak = 0
    while day in active and day >= first:
        streak += 1
        day -= datetime.timedelta(days=1)
    return {"active_days": len(active), "streak": streak}


def record_request(user, *, first_of_day_only=False) -> bool:
    """Count one action for ``user`` today (UserDailyActivity, local day).

    The first call of the day creates the row (count 1) — a cache flag
    remembers that, so later page views cost no query at all; a later
    ``first_of_day_only=False`` call is one ``F()`` UPDATE (+1). A demo
    account's row is a demo row. Returns True when it wrote."""
    from myapp.models import UserDailyActivity

    if user is None or not getattr(user, "pk", None):
        return False
    today = local_today()
    flag = f"activity:seen:{user.pk}:{today.isoformat()}"
    first_time = cache.add(flag, 1, SEEN_FLAG_SECONDS)
    if not first_time and first_of_day_only:
        return False
    if UserDailyActivity.objects.filter(user_id=user.pk, date=today).update(actions_count=F("actions_count") + 1):
        return True
    # No row yet (the first action today, or the flag outlived the row).
    try:
        with transaction.atomic():
            UserDailyActivity.objects.create(
                user_id=user.pk, date=today, actions_count=1, is_demo=bool(getattr(user, "is_demo", False)),
            )
    except IntegrityError:  # a parallel request created it first
        UserDailyActivity.objects.filter(user_id=user.pk, date=today).update(actions_count=F("actions_count") + 1)
    return True


def _text(table, key, **args):
    text = table.get(key, key)
    for name, value in args.items():
        text = text.replace("{" + name + "}", str(value))
    return text


def _date_label(table, day):
    return _text(table, "act.date", d=day.day, m=table.get(f"act.mg{day.month}", str(day.month)), y=day.year)


def _cell_label(table, day, kinds: dict) -> str:
    n = sum(kinds.values())
    when = _date_label(table, day)
    if not n:
        return f"{_text(table, 'act.none')} · {when}"
    label = f"{_text(table, 'act.count.' + plural_category_ru(n), n=n)} · {when}"
    if n > 1:
        parts = [f"{_text(table, 'act.k.' + k)}: {kinds[k]}" for k in KINDS if kinds.get(k)]
        label += ". " + " · ".join(parts)
    return label


def calendar(user, now=None) -> dict | None:
    """Everything the template needs, or None for roles without a calendar.

    ``weeks``: ``WEEKS`` columns from ``grid_start`` × 7 days (Monday first),
    the last one cut at today (future days are not drawn); each day is
    ``{date, iso, count, level, label, kinds, today}``. ``months``: a label
    over the column where each month begins (three columns apart at least).
    """
    if not shows_calendar(user):
        return None
    today = local_today(now)
    key = cache_key(user.pk, today)
    try:
        counts = cache.get(key)
    except Exception:  # noqa: BLE001 — a cache outage must not break the profile
        counts = None
    if counts is None:
        counts = daily_counts(user, today)
        try:
            cache.set(key, counts, CACHE_SECONDS)
        except Exception:  # noqa: BLE001
            pass

    table = translation_table(getattr(settings, "SERVER_RENDER_LANGUAGE", "ru"))
    first, last = window(today)
    maximum = max((sum(k.values()) for k in counts.values()), default=0)
    total = sum(sum(k.values()) for k in counts.values())

    weeks, months = [], []
    seen_month = None
    for w in range(WEEKS):
        column = []
        for d in range(7):
            day = first + datetime.timedelta(days=w * 7 + d)
            if day > last:
                break  # the future is not drawn
            kinds = counts.get(day, {})
            n = sum(kinds.values())
            column.append({
                "date": day,
                "iso": day.isoformat(),
                "count": n,
                "level": level(n, maximum),
                "today": day == today,
                "label": _cell_label(table, day, kinds),
                # Compact "kind:n,kind:n" for the JS re-labelling.
                "kinds": ",".join(f"{k}:{kinds[k]}" for k in KINDS if kinds.get(k)),
            })
        # A month label sits over the column holding that month's 1st (and
        # over the first column, for the month the grid starts in).
        month = column[0]["date"].month if w == 0 else next((c["date"].month for c in column if c["date"].day == 1), None)
        if month and month != seen_month:
            months.append({"col": w, "month": month, "key": f"act.m{month}",
                           "text": table.get(f"act.m{month}", str(month))})
            seen_month = month
        weeks.append(column)
    # The first label goes when the next month starts within 3 columns (GitHub
    # does the same); then no two labels closer than 3 columns.
    if len(months) > 1 and months[1]["col"] - months[0]["col"] < 3:
        months.pop(0)
    spaced = []
    for m in months:
        if not spaced or m["col"] - spaced[-1]["col"] >= 3:
            spaced.append(m)
    months = spaced

    category = plural_category_ru(total)
    role = "volunteer" if user.is_volunteer else "client"
    stats = summary(counts, first, today)
    return {
        "active_days": stats["active_days"],
        "active_days_text": _text(table, f"act.days.{plural_category_ru(stats['active_days'])}", n=stats["active_days"]),
        "streak": stats["streak"],
        "streak_text": _text(table, f"act.streak.{plural_category_ru(stats['streak'])}", n=stats["streak"]),
        "week_count": len(weeks),
        "weeks": weeks,
        "months": months,
        "total": total,
        "total_key": f"act.total.{category}",
        "total_args": json.dumps({"n": total}),
        "total_text": _text(table, f"act.total.{category}", n=total),
        "max": maximum,
        "empty": total == 0,
        "role": role,
        "first": first,
        "last": last,
        "today": today,
    }
