"""Activity calendar (GitHub-style): what a volunteer or client did, per day,
over the last 365 days — computed from existing rows, no model of its own.

One query per action kind (``TruncDate`` in Asia/Dushanbe + ``Count``), summed
by date in Python, so the query count is fixed per role (volunteer 7, client 4)
whatever the number of days. The result is cached for ``CACHE_SECONDS`` per
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
from django.db import models
from django.db.models import Count, F
from django.db.models.functions import TruncDate
from django.utils import timezone

from server.i18n_prerender import translation_table

__all__ = [
    "TZ", "DAYS", "WEEKS", "CACHE_SECONDS", "KINDS", "shows_calendar", "daily_counts",
    "level", "plural_category_ru", "calendar", "cache_key",
]

TZ = ZoneInfo("Asia/Dushanbe")
DAYS = 365
WEEKS = 53
CACHE_SECONDS = 10 * 60
LEVELS = 4

# Display order of the per-kind breakdown in tooltips.
KINDS = (
    "completed", "accepted", "photo_report", "donation_received", "donation_distributed",
    "request_created", "review", "donation_offered", "pet_report",
)


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


def window(today: datetime.date):
    """First and last day (inclusive) of the 365-day window ending today."""
    return today - datetime.timedelta(days=DAYS - 1), today


def daily_counts(user, today: datetime.date | None = None) -> dict:
    """{date: {kind: n}} for the window. One aggregate query per kind."""
    today = today or local_today()
    first, last = window(today)
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
    return dict(days)


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
    return f"activity:v1:{user_id}:{today.isoformat()}"


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

    ``weeks``: 53 columns × 7 days (Monday first); each day is
    ``{date, iso, count, level, label, kinds, out, today}`` — ``out`` marks the
    padding before the window and after today. ``months``: label positions.
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
    grid_start = first - datetime.timedelta(days=first.weekday())   # the Monday on/before
    maximum = max((sum(k.values()) for k in counts.values()), default=0)
    total = sum(sum(k.values()) for k in counts.values())

    weeks, months = [], []
    seen_month = None
    for w in range(WEEKS):
        column = []
        for d in range(7):
            day = grid_start + datetime.timedelta(days=w * 7 + d)
            kinds = counts.get(day, {})
            n = sum(kinds.values())
            out = day < first or day > last
            column.append({
                "date": day,
                "iso": day.isoformat(),
                "count": n,
                "level": level(n, maximum),
                "out": out,
                "today": day == today,
                "label": "" if out else _cell_label(table, day, kinds),
                # Compact "kind:n,kind:n" for the JS re-labelling.
                "kinds": ",".join(f"{k}:{kinds[k]}" for k in KINDS if kinds.get(k)),
            })
        # A month label sits over the first column holding that month's 1st…
        # (or the first column at all), unless it would crowd the previous one.
        in_range = [c["date"] for c in column if not c["out"]]
        if in_range:
            month = in_range[0].month if w == 0 else next((c.month for c in in_range if c.day == 1), None)
            if month and month != seen_month and (not months or w - months[-1]["col"] >= 3):
                months.append({"col": w, "month": month, "key": f"act.m{month}",
                               "text": table.get(f"act.m{month}", str(month))})
                seen_month = month
        weeks.append(column)
    # The first label is dropped if the next one follows too closely (GitHub does the same).
    if len(months) > 1 and months[1]["col"] - months[0]["col"] < 3:
        months.pop(0)

    category = plural_category_ru(total)
    role = "volunteer" if user.is_volunteer else "client"
    return {
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
