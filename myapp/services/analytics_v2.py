"""CRM analytics charts (Feature Pack v2, feature 10) — data only, no HTML.

Every chart is built from **aggregate queries** (``Trunc*`` + ``Count`` / ``Avg``
with ``filter=``), so the number of SQL queries is constant however many
requests, volunteers or reviews exist — locked by query-count tests in
``myapp/tests_analytics.py``. Each chart is cached for ``CACHE_TTL`` (5 min;
Redis in production) per (chart, period, region).

Filters (``parse_filters`` — the ONE place the page's GET parameters are read;
every chart, KPI, CSV and the heatmap gets the same ``Filters``):
``?period=7d|30d|3m|1y|custom`` (default 30d; 3m = 90 days, 1y = 365), with
``from``/``to`` (ISO dates) only for ``custom`` (≤ ``MAX_RANGE_DAYS``, never in
the future, from ≤ to), plus an optional ``region``. Invalid input never raises:
the defaults are used and ``Filters.error`` says so. The old ``?days=7|30|90``
links keep working. Days are local (Asia/Dushanbe — ``TIME_ZONE`` is UTC); "to"
includes its whole day. Ranges longer than ``WEEKLY_AFTER_DAYS`` are bucketed by
week. ``Filters.previous()`` is the same-length period right before.

Thresholds are never re-inlined: the overdue / stale trends use
``overdue.was_overdue_q`` / ``stale.was_stale_q`` (the same
``OVERDUE_THRESHOLD`` / ``STALE_PENDING_THRESHOLD`` as the live predicates).

Chart shape (``CHARTS[name](filters) -> dict``)::

    {"kind": "line" | "bar" | "hbar" | "table", "labels": [...],
     "label_keys": [...] | None,           # i18n keys for category labels
     "series": [{"key", "label_key", "values": [...]}],
     "rows": [...]}                        # "table" charts only

Privacy: counts and averages only — no phone, email, address or comment
anywhere; the one named list (``top_volunteers``) carries a display name,
region and numbers staff already see on the CRM volunteer page. The heatmap
rounds coordinates to ~100 m and caps the point count.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Q
from django.db.models.functions import Round, TruncDate, TruncWeek
from django.utils import timezone

from accounts.models import REGION_CHOICES, Profile, Users

from ..models import (
    ChatMessage,
    DONATION_CATEGORY_CHOICES,
    DONATION_STATUS_CHOICES,
    HELP_TYPE_CHOICES,
    PRIORITY_CHOICES,
    Donation,
    EmergencyReport,
    HelpRequest,
    TaskReview,
)
from . import notify_texts
from .chat_flags import CATEGORIES as FLAG_CATEGORIES
from .geo import is_valid_coordinate
from .overdue import was_overdue_q
from .stale import was_stale_q

LOCAL_TZ = ZoneInfo("Asia/Dushanbe")
# period token -> days; the order is the dropdown order ("custom" comes last).
PERIODS = {"7d": 7, "30d": 30, "3m": 90, "1y": 365}
CUSTOM = "custom"
PRESETS = tuple(PERIODS.values())
DEFAULT_DAYS = 30
DEFAULT_PERIOD = "30d"
MAX_RANGE_DAYS = 366
WEEKLY_AFTER_DAYS = 92
CACHE_TTL = 300
CACHE_VERSION = "an1"
TOP_VOLUNTEERS = 10
HEAT_POINT_CAP = 2000
HEAT_PRECISION = 3  # decimal places ≈ 110 m
WORKLOAD_BUCKETS = ((1, 1, "1"), (2, 3, "2–3"), (4, 6, "4–6"), (7, 10, "7–10"), (11, None, "11+"))
REGIONS = [code for code, _ in REGION_CHOICES]


# --------------------------------------------------------------------------- #
# filters
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Filters:
    start_date: date      # inclusive, local
    end_date: date        # inclusive, local
    region: str = ""
    preset: int | None = DEFAULT_DAYS   # days of a preset period, None = custom range
    error: str = ""       # "" or "invalid": the URL's filters were replaced by the defaults

    @property
    def days(self) -> int:
        return (self.end_date - self.start_date).days + 1

    @property
    def period(self) -> str:
        """The ``?period=`` token: 7d / 30d / 3m / 1y, or custom."""
        return next((token for token, days in PERIODS.items() if days == self.preset), CUSTOM)

    @property
    def weekly(self) -> bool:
        return self.days > WEEKLY_AFTER_DAYS

    @property
    def start(self):
        return datetime.combine(self.start_date, time.min, tzinfo=LOCAL_TZ)

    @property
    def end(self):  # exclusive: midnight after end_date, so "to" includes its whole day
        return datetime.combine(self.end_date + timedelta(days=1), time.min, tzinfo=LOCAL_TZ)

    def previous(self) -> "Filters":
        """The period of the same length right before this one (same region)."""
        end = self.start_date - timedelta(days=1)
        return Filters(end - timedelta(days=self.days - 1), end, self.region, self.preset)

    def key(self, chart) -> str:
        return f"{CACHE_VERSION}:{chart}:{self.start_date}:{self.end_date}:{self.region or '-'}"

    def querystring(self) -> str:
        parts = [f"period={self.period}"]
        if self.period == CUSTOM:
            parts += [f"from={self.start_date}", f"to={self.end_date}"]
        if self.region:
            parts.append(f"region={self.region}")
        return "&".join(parts)


def local_today(now=None) -> date:
    return timezone.localtime(now or timezone.now(), LOCAL_TZ).date()


def _parse_date(value):
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


def _preset(days, region, today, error=""):
    return Filters(today - timedelta(days=days - 1), today, region, days, error)


def parse_filters(params, today=None) -> Filters:
    """From a QueryDict / dict; never raises.

    ``period`` = a PERIODS token or ``custom`` (+ ``from``/``to``). The legacy
    ``days`` (7/30/90) and a bare ``from``/``to`` pair are still understood.
    Anything invalid — an unknown period, a custom range with a missing or
    unreadable date, from after to, a date in the future — falls back to the
    last 30 days with ``error="invalid"`` (the page says so). A valid custom
    range longer than MAX_RANGE_DAYS is trimmed to its last MAX_RANGE_DAYS.
    An unknown region is simply "all regions"."""
    today = today or local_today()
    region = params.get("region", "") or ""
    region = region if region in REGIONS else ""
    period = (params.get("period") or "").strip()
    if not period:
        legacy = (params.get("days") or "").strip()
        if legacy:
            period = {7: "7d", 30: "30d", 90: "3m"}.get(_int(legacy), "?")
        elif params.get("from") or params.get("to"):
            period = CUSTOM
        else:
            period = DEFAULT_PERIOD
    if period in PERIODS:
        return _preset(PERIODS[period], region, today)
    if period != CUSTOM:
        return _preset(DEFAULT_DAYS, region, today, "invalid")
    start, end = _parse_date(params.get("from")), _parse_date(params.get("to"))
    if not start or not end or start > end or end > today:
        return _preset(DEFAULT_DAYS, region, today, "invalid")
    # Too long a range is trimmed to the last MAX_RANGE_DAYS, not dropped.
    start = max(start, end - timedelta(days=MAX_RANGE_DAYS - 1))
    return Filters(start, end, region, None)


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def bucket_dates(f: Filters) -> list:
    if f.weekly:
        first = f.start_date - timedelta(days=f.start_date.weekday())
        out, cur = [], first
        while cur <= f.end_date:
            out.append(cur)
            cur += timedelta(days=7)
        return out
    return [f.start_date + timedelta(days=i) for i in range(f.days)]


def _trunc(field, f):
    return TruncWeek(field, tzinfo=LOCAL_TZ) if f.weekly else TruncDate(field, tzinfo=LOCAL_TZ)


def _as_date(value):
    if isinstance(value, datetime):
        return timezone.localtime(value, LOCAL_TZ).date() if timezone.is_aware(value) else value.date()
    return value


def _timeline(qs, field, f, **aggregates):
    """{bucket date: {name: value}} — ONE grouped query."""
    rows = (
        qs.filter(**{f"{field}__gte": f.start, f"{field}__lt": f.end})
        .annotate(_b=_trunc(field, f))
        .values("_b")
        .annotate(**aggregates)
        .order_by("_b")
    )
    return {_as_date(row.pop("_b")): row for row in rows}


def _labels(dates):
    return [d.isoformat() for d in dates]


def _fill(dates, data, name, cast=int):
    out = []
    for d in dates:
        value = (data.get(d) or {}).get(name)
        out.append(cast(value) if value is not None else (0 if cast is int else None))
    return out


def _requests(f):
    qs = HelpRequest.objects.all()
    return qs.filter(region=f.region) if f.region else qs


def _hours(delta):
    if delta is None:
        return None
    return round(delta.total_seconds() / 3600, 1)


def _duration(end_field, start_field):
    return ExpressionWrapper(F(end_field) - F(start_field), output_field=DurationField())


def _series(key, label_key, values):
    return {"key": key, "label_key": label_key, "values": values}


def _category(choices, counts, key_prefix):
    codes = [code for code, _ in choices]
    return codes, [f"{key_prefix}{code}" for code in codes], [int(counts.get(code, 0)) for code in codes]


# --------------------------------------------------------------------------- #
# charts — each one or two aggregate queries
# --------------------------------------------------------------------------- #

def requests_timeline(f):
    dates = bucket_dates(f)
    qs = _requests(f)
    created = _timeline(qs, "created_at", f, n=Count("id"))
    completed = _timeline(qs.filter(status="completed"), "completed_at", f, n=Count("id"))
    cancelled = _timeline(qs.filter(status="cancelled"), "cancelled_at", f, n=Count("id"))
    return {"kind": "line", "labels": _labels(dates), "label_keys": None, "series": [
        _series("created", "an.s.created", _fill(dates, created, "n")),
        _series("completed", "an.s.completed", _fill(dates, completed, "n")),
        _series("cancelled", "an.s.cancelled", _fill(dates, cancelled, "n")),
    ]}


def _created_in(f):
    return _requests(f).filter(created_at__gte=f.start, created_at__lt=f.end)


def help_types(f):
    counts = dict(_created_in(f).values_list("help_type").annotate(n=Count("id")).order_by())
    codes, keys, values = _category(HELP_TYPE_CHOICES, counts, "an.help.")
    return {"kind": "hbar", "labels": codes, "label_keys": keys,
            "series": [_series("created", "an.s.created", values)]}


def regions(f):
    rows = (HelpRequest.objects.filter(created_at__gte=f.start, created_at__lt=f.end)
            .values("region").annotate(created=Count("id"),
                                       completed=Count("id", filter=Q(status="completed"))).order_by())
    by = {row["region"]: row for row in rows}
    codes = [r for r in REGIONS if not f.region or r == f.region]
    return {"kind": "hbar", "labels": codes, "label_keys": [f"region.{c}" for c in codes], "series": [
        _series("created", "an.s.created", [int((by.get(c) or {}).get("created", 0)) for c in codes]),
        _series("completed", "an.s.completed", [int((by.get(c) or {}).get("completed", 0)) for c in codes]),
    ]}


def priorities(f):
    counts = dict(_created_in(f).values_list("priority").annotate(n=Count("id")).order_by())
    codes, keys, values = _category(PRIORITY_CHOICES, counts, "an.priority.")
    return {"kind": "bar", "labels": codes, "label_keys": keys,
            "series": [_series("created", "an.s.created", values)]}


def funnel(f):
    """Requests created in the period → accepted → completed (+ cancelled),
    with the average hours to accept / to complete. One query."""
    agg = _created_in(f).aggregate(
        created=Count("id"),
        accepted=Count("id", filter=Q(accepted_at__isnull=False)),
        completed=Count("id", filter=Q(status="completed")),
        cancelled=Count("id", filter=Q(status="cancelled")),
        # Rows with inconsistent timestamps (imports, hand edits) never skew the means.
        to_accept=Avg(_duration("accepted_at", "created_at"), filter=Q(accepted_at__gte=F("created_at"))),
        to_complete=Avg(_duration("completed_at", "accepted_at"),
                        filter=Q(status="completed", completed_at__gte=F("accepted_at"))),
    )
    stages = ["created", "accepted", "completed"]
    return {
        "kind": "bar", "labels": stages, "label_keys": [f"an.s.{s}" for s in stages],
        "series": [_series("requests", "an.s.requests", [int(agg[s] or 0) for s in stages])],
        "kpis": {
            "created": int(agg["created"] or 0),
            "completed": int(agg["completed"] or 0),
            "cancelled": int(agg["cancelled"] or 0),
            "completion_rate": round(100 * agg["completed"] / agg["created"]) if agg["created"] else None,
            "hours_to_accept": _hours(agg["to_accept"]),
            "hours_to_complete": _hours(agg["to_complete"]),
        },
    }


def delays(f):
    """Overdue (by accept day) and stale (by creation day) — the historical
    trend of the two live CRM queues."""
    dates = bucket_dates(f)
    now = timezone.now()
    overdue = _timeline(_requests(f), "accepted_at", f, n=Count("id", filter=was_overdue_q(now)))
    stale = _timeline(_requests(f), "created_at", f, n=Count("id", filter=was_stale_q(now)))
    return {"kind": "line", "labels": _labels(dates), "label_keys": None, "series": [
        _series("overdue", "an.s.overdue", _fill(dates, overdue, "n")),
        _series("stale", "an.s.stale", _fill(dates, stale, "n")),
    ]}


def volunteer_activity(f):
    """Distinct volunteers who completed at least one request per day/week."""
    dates = bucket_dates(f)
    data = _timeline(_requests(f).filter(status="completed", volunteer__isnull=False), "completed_at", f,
                     n=Count("volunteer", distinct=True))
    return {"kind": "line", "labels": _labels(dates), "label_keys": None,
            "series": [_series("volunteers", "an.s.active_volunteers", _fill(dates, data, "n"))]}


def workload(f):
    """How many volunteers completed 1, 2–3, 4–6, 7–10, 11+ requests in the
    period — the shape of the load, not a leaderboard. One query (one row per
    active volunteer, bucketed here)."""
    per_volunteer = (_requests(f).filter(status="completed", completed_at__gte=f.start,
                                         completed_at__lt=f.end, volunteer__isnull=False)
                     .values("volunteer").annotate(n=Count("id")).values_list("n", flat=True))
    counts = [0] * len(WORKLOAD_BUCKETS)
    for n in per_volunteer:
        for i, (low, high, _) in enumerate(WORKLOAD_BUCKETS):
            if n >= low and (high is None or n <= high):
                counts[i] += 1
                break
    labels = [label for _, _, label in WORKLOAD_BUCKETS]
    return {"kind": "bar", "labels": labels, "label_keys": None,
            "series": [_series("volunteers", "an.s.volunteers", counts)]}


def top_volunteers(f):
    """Staff-only table: volunteers with the most completed requests in the
    period, *with* their review average next to the count (quality beside
    quantity). One query."""
    rows = (
        Users.objects.filter(is_volunteer=True)
        .annotate(done=Count("volunteer_tasks", filter=Q(
            volunteer_tasks__status="completed",
            volunteer_tasks__completed_at__gte=f.start, volunteer_tasks__completed_at__lt=f.end,
            **({"volunteer_tasks__region": f.region} if f.region else {}),
        )))
        .filter(done__gt=0)
        .order_by("-done", "id")
        .values("id", "username", "region", "profile__full_name", "profile__review_avg", "profile__review_count", "done")
        [:TOP_VOLUNTEERS]
    )
    table = [{
        "id": r["id"],
        "name": r["profile__full_name"] or r["username"],
        "region": r["region"] or "",
        "completed": r["done"],
        "review_avg": float(r["profile__review_avg"]) if r["profile__review_avg"] is not None else None,
        "review_count": r["profile__review_count"] or 0,
    } for r in rows]
    return {"kind": "table", "labels": [], "label_keys": None, "series": [], "rows": table}


def availability(f):
    """Live split of active volunteers (not period-bound). One query."""
    qs = Users.objects.filter(is_volunteer=True, is_active=True)
    if f.region:
        qs = qs.filter(region=f.region)
    counts = {
        (row["profile__availability_status"] or Profile.AVAILABILITY_AVAILABLE): row["n"]
        for row in qs.values("profile__availability_status").annotate(n=Count("id")).order_by()
    }
    codes = [code for code, _ in Profile.AVAILABILITY_CHOICES]
    return {"kind": "hbar", "labels": codes, "label_keys": [f"availability.{c}" for c in codes],
            "series": [_series("volunteers", "an.s.volunteers", [int(counts.get(c, 0)) for c in codes])]}


def emergencies(f):
    dates = bucket_dates(f)
    qs = EmergencyReport.objects.all()
    if f.region:
        qs = qs.filter(help_request__region=f.region)
    data = _timeline(qs, "created_at", f, n=Count("id"))
    agg = qs.filter(created_at__gte=f.start, created_at__lt=f.end).aggregate(
        total=Count("id"),
        response=Avg(_duration("acknowledged_at", "created_at"), filter=Q(acknowledged_at__gte=F("created_at"))),
    )
    minutes = None
    if agg["response"] is not None:
        minutes = round(agg["response"].total_seconds() / 60)
    return {"kind": "bar", "labels": _labels(dates), "label_keys": None,
            "series": [_series("sos", "an.s.sos", _fill(dates, data, "n"))],
            "kpis": {"total": int(agg["total"] or 0), "minutes_to_ack": minutes}}


def _donations(f):
    qs = Donation.objects.filter(created_at__gte=f.start, created_at__lt=f.end)
    return qs.filter(region=f.region) if f.region else qs


def donations_status(f):
    counts = dict(_donations(f).values_list("status").annotate(n=Count("id")).order_by())
    codes, keys, values = _category(DONATION_STATUS_CHOICES, counts, "don.status_")
    return {"kind": "hbar", "labels": codes, "label_keys": keys,
            "series": [_series("offers", "an.s.offers", values)]}


def donations_category(f):
    counts = dict(_donations(f).values_list("category").annotate(n=Count("id")).order_by())
    codes, keys, values = _category(DONATION_CATEGORY_CHOICES, counts, "don.category_")
    return {"kind": "hbar", "labels": codes, "label_keys": keys,
            "series": [_series("offers", "an.s.offers", values)]}


def _reviews(f):
    qs = TaskReview.objects.filter(hidden_at__isnull=True, created_at__gte=f.start, created_at__lt=f.end)
    return qs.filter(task__region=f.region) if f.region else qs


def ratings(f):
    """Distribution of visible review stars in the period (hidden never count)."""
    counts = dict(_reviews(f).values_list("stars").annotate(n=Count("id")).order_by())
    stars = [1, 2, 3, 4, 5]
    return {"kind": "bar", "labels": [str(s) for s in stars], "label_keys": [f"an.stars.{s}" for s in stars],
            "series": [_series("reviews", "an.s.reviews", [int(counts.get(s, 0)) for s in stars])]}


def ratings_timeline(f):
    dates = bucket_dates(f)
    qs = TaskReview.objects.filter(hidden_at__isnull=True)
    if f.region:
        qs = qs.filter(task__region=f.region)
    data = _timeline(qs, "created_at", f, avg=Avg("stars"))
    values = [round(v, 2) if v is not None else None for v in _fill(dates, data, "avg", cast=float)]
    return {"kind": "line", "labels": _labels(dates), "label_keys": None, "y_max": 5,
            "series": [_series("avg", "an.s.avg_rating", values)]}


def _party_messages(f):
    """Client / volunteer messages in the period (staff and system never flagged)."""
    qs = ChatMessage.objects.filter(created_at__gte=f.start, created_at__lt=f.end,
                                    sender__isnull=False, is_staff_post=False)
    return qs.filter(task__region=f.region) if f.region else qs


def _flag_rows(f):
    """(flags, help_type) of every flagged message in the period — one query;
    the per-category count happens here because a JSON-list "contains" lookup
    isn't portable (SQLite)."""
    return list(_party_messages(f).exclude(flags=[]).values_list("flags", "task__help_type"))


def chat_flags_topics(f):
    """Chat safety flags by topic (a message can carry several), the share of
    flagged party messages and how many still wait for a curator's review."""
    rows = _flag_rows(f)
    counts = {code: 0 for code in FLAG_CATEGORIES}
    for flags, _ in rows:
        for code in flags or ():
            if code in counts:
                counts[code] += 1
    total = _party_messages(f).count()
    unreviewed = _party_messages(f).exclude(flags=[]).filter(reviewed_at__isnull=True).count()
    return {"kind": "hbar", "labels": list(FLAG_CATEGORIES),
            "label_keys": [f"an.flag.{c}" for c in FLAG_CATEGORIES],
            "series": [_series("messages", "an.s.flagged_messages", [counts[c] for c in FLAG_CATEGORIES])],
            "kpis": {"flagged": len(rows), "messages": total,
                     "share": round(100 * len(rows) / total, 1) if total else None,
                     "unreviewed": unreviewed}}


def chat_flags_help_types(f):
    """Flagged messages per type of help, one series per topic — the table view
    is the help type × topic matrix."""
    matrix = {(h, c): 0 for h, _ in HELP_TYPE_CHOICES for c in FLAG_CATEGORIES}
    for flags, help_type in _flag_rows(f):
        for code in flags or ():
            if (help_type, code) in matrix:
                matrix[(help_type, code)] += 1
    codes = [h for h, _ in HELP_TYPE_CHOICES]
    return {"kind": "hbar", "labels": codes, "label_keys": [f"an.help.{h}" for h in codes],
            "series": [_series(c, f"an.flag.{c}", [matrix[(h, c)] for h in codes]) for c in FLAG_CATEGORIES]}


def registrations(f):
    dates = bucket_dates(f)
    qs = Users.objects.all()
    if f.region:
        qs = qs.filter(region=f.region)
    data = _timeline(qs, "date_joined", f,
                     volunteers=Count("id", filter=Q(is_volunteer=True)),
                     clients=Count("id", filter=Q(is_client=True)),
                     curators=Count("id", filter=Q(is_curator=True)),
                     other=Count("id", filter=Q(is_volunteer=False, is_client=False, is_curator=False)))
    return {"kind": "line", "labels": _labels(dates), "label_keys": None, "series": [
        _series("volunteers", "an.role.volunteers", _fill(dates, data, "volunteers")),
        _series("clients", "an.role.clients", _fill(dates, data, "clients")),
        _series("curators", "an.role.curators", _fill(dates, data, "curators")),
        _series("other", "an.role.other", _fill(dates, data, "other")),
    ]}


# name -> builder; the order is the page order.
CHARTS = {
    "requests_timeline": requests_timeline,
    "funnel": funnel,
    "help_types": help_types,
    "regions": regions,
    "priorities": priorities,
    "delays": delays,
    "volunteer_activity": volunteer_activity,
    "workload": workload,
    "availability": availability,
    "top_volunteers": top_volunteers,
    "emergencies": emergencies,
    "ratings": ratings,
    "ratings_timeline": ratings_timeline,
    "donations_status": donations_status,
    "donations_category": donations_category,
    "chat_flags_topics": chat_flags_topics,
    "chat_flags_help_types": chat_flags_help_types,
    "registrations": registrations,
}


def chart(name, f: Filters) -> dict:
    """One chart, cached for CACHE_TTL per (chart, period, region)."""
    builder = CHARTS[name]
    return cache.get_or_set(f.key(name), lambda: builder(f), CACHE_TTL)


def all_charts(f: Filters) -> dict:
    return {name: chart(name, f) for name in CHARTS}


# --------------------------------------------------------------------------- #
# card headers: the period's headline number + change vs the previous period
# --------------------------------------------------------------------------- #

def _sum_series(data, index=0):
    series = data["series"]
    return sum(v or 0 for v in series[index]["values"]) if len(series) > index else 0


def _sum_all(data):
    return sum(v or 0 for s in data["series"] for v in s["values"])


def _avg_stars(data):
    counts = data["series"][0]["values"]
    rated = sum(counts)
    return round(sum((i + 1) * n for i, n in enumerate(counts)) / rated, 1) if rated else None


# chart -> (how its headline is read from the chart data, given get(name) -> data).
# Read from the same cached chart dicts the cards draw, so a header can never
# disagree with its chart; nothing is recomputed. Active volunteers = the
# distinct volunteers who completed something in the period = the workload total.
HEADLINES = {
    "requests_timeline": lambda get: _sum_series(get("requests_timeline")),
    "funnel": lambda get: get("funnel")["kpis"]["completed"],
    "help_types": lambda get: _sum_series(get("help_types")),
    "regions": lambda get: _sum_series(get("regions")),
    "priorities": lambda get: _sum_series(get("priorities")),
    "delays": lambda get: _sum_all(get("delays")),
    "volunteer_activity": lambda get: _sum_series(get("workload")),
    "workload": lambda get: _sum_series(get("workload")),
    "availability": lambda get: _sum_series(get("availability")),
    "emergencies": lambda get: get("emergencies")["kpis"]["total"],
    "ratings": lambda get: _avg_stars(get("ratings")),
    "ratings_timeline": lambda get: _avg_stars(get("ratings")),
    "donations_status": lambda get: _sum_series(get("donations_status")),
    "donations_category": lambda get: _sum_series(get("donations_category")),
    "chat_flags_topics": lambda get: get("chat_flags_topics")["kpis"]["flagged"],
    "chat_flags_help_types": lambda get: get("chat_flags_topics")["kpis"]["flagged"],
    "registrations": lambda get: _sum_all(get("registrations")),
}
# Live snapshots: no "previous period" exists for them.
LIVE_CHARTS = {"availability"}
# Charts whose growth is bad news (the change chip turns red when it goes up).
UP_IS_BAD = {"delays", "emergencies", "chat_flags_topics", "chat_flags_help_types"}
DECIMAL_HEADLINES = {"ratings", "ratings_timeline"}


def change_percent(current, previous):
    """Whole-percent change, or None when it can't be stated honestly (an
    empty previous period has no "+∞%")."""
    if current is None or not previous:
        return None
    return round((current - previous) * 100 / previous)


def headlines(f: Filters) -> dict:
    """{chart: {value, previous, change, direction, good, live, decimals}}.
    ``previous`` comes from ``f.previous()`` (same length, right before, same
    region) through the same cached charts; ``direction`` is up/down/same, or
    None when there is nothing to compare."""
    prev_f = f.previous()
    out = {}
    for name, read in HEADLINES.items():
        value = read(lambda n: chart(n, f))
        live = name in LIVE_CHARTS
        previous = None if live else read(lambda n: chart(n, prev_f))
        change = None if live else change_percent(value, previous)
        direction = None
        if change is not None:
            direction = "same" if value == previous else ("up" if value > previous else "down")
        good = None
        if direction in ("up", "down"):
            good = (direction == "up") != (name in UP_IS_BAD)
        out[name] = {
            "value": value, "previous": previous, "change": change, "direction": direction,
            "good": good, "live": live, "decimals": 1 if name in DECIMAL_HEADLINES else 0,
        }
    return out


def dashboard_trend(days=14) -> dict:
    """The compact "requests, last 14 days" card on the curator/admin
    dashboard — the cached ``requests_timeline`` of that window."""
    today = local_today()
    f = Filters(today - timedelta(days=days - 1), today, "", None)
    data = chart("requests_timeline", f)
    values = {s["key"]: s["values"] for s in data["series"]}
    return {
        "days": days,
        "labels": data["labels"],
        "created": values["created"],
        "completed": values["completed"],
        "total_created": sum(values["created"]),
        "total_completed": sum(values["completed"]),
    }


# --------------------------------------------------------------------------- #
# heatmap + CSV
# --------------------------------------------------------------------------- #

def heat_points(f: Filters) -> list:
    """``[[lat, lng, weight], …]`` — requests created in the period grouped on
    coordinates rounded to ~110 m, heaviest first, capped. No ids, no PII."""
    def build():
        rows = (
            _created_in(f).filter(latitude__isnull=False, longitude__isnull=False)
            .annotate(_lat=Round("latitude", HEAT_PRECISION), _lng=Round("longitude", HEAT_PRECISION))
            .values("_lat", "_lng").annotate(n=Count("id")).order_by("-n", "_lat", "_lng")[:HEAT_POINT_CAP]
        )
        points = []
        for row in rows:
            lat, lng = float(row["_lat"]), float(row["_lng"])
            if is_valid_coordinate(lat, lng):
                points.append([round(lat, HEAT_PRECISION), round(lng, HEAT_PRECISION), int(row["n"])])
        return points
    return cache.get_or_set(f.key("heat"), build, CACHE_TTL)


_CSV_INJECTION = ("=", "+", "-", "@", "\t", "\r")


def _cell(value):
    if value is None:
        return ""
    text = str(value)
    # Spreadsheet formula injection: a name starting with "=" etc. stays text.
    if text and text[0] in _CSV_INJECTION and not _is_number(text):
        return "'" + text
    return text


def _is_number(text):
    try:
        float(text)
        return True
    except ValueError:
        return False


def _label_text(key, code, lang):
    return ui_table(lang).get(key) or code


def to_csv(name, data, lang="ru") -> str:
    """The chart's data as CSV (labels in ``lang``; UTF-8 with a BOM so Excel
    opens Cyrillic / Tajik correctly)."""
    buf = io.StringIO()
    buf.write("﻿")
    writer = csv.writer(buf)
    table = ui_table(lang)
    if data["kind"] == "table":
        writer.writerow([table.get(k, k) for k in
                         ("an.col.name", "an.col.region", "an.col.completed", "an.col.avg", "an.col.reviews")])
        for row in data["rows"]:
            region = table.get(f"region.{row['region']}", row["region"])
            writer.writerow([_cell(row["name"]), _cell(region), row["completed"],
                             "" if row["review_avg"] is None else row["review_avg"], row["review_count"]])
        return buf.getvalue()
    header = [table.get("an.col.period" if data["label_keys"] is None else "an.col.category", "")]
    header += [table.get(s["label_key"], s["key"]) for s in data["series"]]
    writer.writerow(header)
    keys = data["label_keys"] or [None] * len(data["labels"])
    for i, (label, key) in enumerate(zip(data["labels"], keys)):
        shown = _label_text(key, label, lang) if key else label
        writer.writerow([_cell(shown)] + ["" if s["values"][i] is None else s["values"][i] for s in data["series"]])
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# texts (source of the an.* keys in static/js/i18n.js)
# --------------------------------------------------------------------------- #

UI_STRINGS = {
    "an.title": {"en": "Analytics", "ru": "Аналитика", "tj": "Таҳлил"},
    "an.lede": {"en": "Platform trends for coordinators. Numbers are refreshed every 5 minutes.",
                "ru": "Динамика платформы для координаторов. Цифры обновляются раз в 5 минут.",
                "tj": "Тағйироти платформа барои ҳамоҳангсозон. Рақамҳо ҳар 5 дақиқа навсозӣ мешаванд."},
    "an.period": {"en": "Period", "ru": "Период", "tj": "Давра"},
    "an.from": {"en": "From", "ru": "С", "tj": "Аз"},
    "an.to": {"en": "To", "ru": "По", "tj": "То"},
    "an.apply": {"en": "Apply", "ru": "Применить", "tj": "Татбиқ кардан"},
    "an.p.7d": {"en": "7 days", "ru": "7 дней", "tj": "7 рӯз"},
    "an.p.30d": {"en": "30 days", "ru": "30 дней", "tj": "30 рӯз"},
    "an.p.3m": {"en": "3 months", "ru": "3 месяца", "tj": "3 моҳ"},
    "an.p.1y": {"en": "Year", "ru": "Год", "tj": "Сол"},
    "an.p.custom": {"en": "Custom range", "ru": "Свой период", "tj": "Давраи дилхоҳ"},
    "an.from_aria": {"en": "Start date", "ru": "Начало периода", "tj": "Санаи оғоз"},
    "an.to_aria": {"en": "End date", "ru": "Конец периода", "tj": "Санаи анҷом"},
    "an.reset": {"en": "Reset", "ru": "Сбросить", "tj": "Тоза кардан"},
    "an.err.required": {"en": "Pick both dates.", "ru": "Укажите обе даты.", "tj": "Ҳарду санаро интихоб кунед."},
    "an.err.future": {"en": "Dates can't be in the future.", "ru": "Дата не может быть в будущем.", "tj": "Сана наметавонад дар оянда бошад."},
    "an.err.order": {"en": "“From” can't be later than “To”.", "ru": "Дата «С» не может быть позже даты «По».", "tj": "Санаи «Аз» наметавонад аз санаи «То» дертар бошад."},
    "an.err.too_long": {"en": "The period can't be longer than 366 days.", "ru": "Период не может быть длиннее 366 дней.", "tj": "Давра наметавонад аз 366 рӯз дарозтар бошад."},
    "an.err.url": {"en": "The filters in the link were invalid — showing the last 30 days.", "ru": "Фильтры в ссылке некорректны — показаны последние 30 дней.", "tj": "Филтрҳои пайванд нодурустанд — 30 рӯзи охир нишон дода шуд."},
    "an.region": {"en": "Region", "ru": "Регион", "tj": "Минтақа"},
    "an.all_regions": {"en": "All regions", "ru": "Все регионы", "tj": "Ҳама минтақаҳо"},
    "an.updated": {"en": "Data as of", "ru": "Данные на", "tj": "Маълумот то"},
    "an.weekly_note": {"en": "Long period: values are grouped by week.",
                       "ru": "Длинный период: значения сгруппированы по неделям.",
                       "tj": "Давраи дароз: қиматҳо аз рӯи ҳафта гурӯҳбандӣ шудаанд."},
    "an.csv": {"en": "CSV", "ru": "CSV", "tj": "CSV"},
    "an.csv_hint": {"en": "Download this chart's data (CSV)", "ru": "Скачать данные графика (CSV)",
                    "tj": "Боргирии маълумоти диаграмма (CSV)"},
    "an.table": {"en": "Show as a table", "ru": "Показать таблицей", "tj": "Ҳамчун ҷадвал нишон додан"},
    "an.no_data": {"en": "No data for the selected period", "ru": "Нет данных за выбранный период", "tj": "Барои давраи интихобшуда маълумот нест"},
    "an.vs_prev": {"en": "vs previous period", "ru": "к прошлому периоду", "tj": "нисбат ба давраи гузашта"},
    "an.prev_value": {"en": "{n} in the previous period", "ru": "{n} в прошлом периоде", "tj": "{n} дар давраи гузашта"},
    "an.prev_empty": {"en": "No data in the previous period", "ru": "В прошлом периоде данных нет", "tj": "Дар давраи гузашта маълумот нест"},
    "an.delta_up": {"en": "up {p}", "ru": "рост на {p}", "tj": "афзоиш {p}"},
    "an.delta_down": {"en": "down {p}", "ru": "снижение на {p}", "tj": "коҳиш {p}"},
    "an.delta_same": {"en": "no change", "ru": "без изменений", "tj": "бетағйир"},
    "an.live_now": {"en": "current state", "ru": "текущее состояние", "tj": "ҳолати ҷорӣ"},
    "an.total": {"en": "total", "ru": "всего", "tj": "ҳамагӣ"},
    "an.date_short": {"en": "{m} {d}", "ru": "{d} {m}", "tj": "{d} {m}"},
    "an.u.h": {"en": "h", "ru": "ч", "tj": "соат"},
    "an.u.min": {"en": "min", "ru": "мин", "tj": "дақ"},
    "an.open": {"en": "Analytics", "ru": "Аналитика", "tj": "Таҳлил"},
    "an.trend_title": {"en": "Requests, last {days} days", "ru": "Запросы за {days} дней", "tj": "Дархостҳо дар {days} рӯзи охир"},
    # KPI tiles
    "an.kpi.created": {"en": "Requests created", "ru": "Создано запросов", "tj": "Дархостҳои сохташуда"},
    "an.kpi.completed": {"en": "Completed", "ru": "Выполнено", "tj": "Иҷро шуд"},
    "an.kpi.completion_rate": {"en": "Completion rate", "ru": "Доля выполненных", "tj": "Ҳиссаи иҷрошуда"},
    "an.kpi.to_accept": {"en": "Avg. time to accept", "ru": "Среднее время до принятия", "tj": "Вақти миёна то қабул"},
    "an.kpi.to_complete": {"en": "Avg. time to complete", "ru": "Среднее время выполнения", "tj": "Вақти миёнаи иҷро"},
    "an.kpi.avg_rating": {"en": "Average rating", "ru": "Средняя оценка", "tj": "Баҳои миёна"},
    "an.kpi.sos_ack": {"en": "Avg. SOS response", "ru": "Средняя реакция на SOS", "tj": "Вокуниши миёна ба SOS"},
    "an.unit.h": {"en": "{n} h", "ru": "{n} ч", "tj": "{n} соат"},
    "an.unit.min": {"en": "{n} min", "ru": "{n} мин", "tj": "{n} дақ"},
    # chart titles
    "an.c.requests_timeline": {"en": "Requests over time", "ru": "Запросы по дням", "tj": "Дархостҳо аз рӯи рӯзҳо"},
    "an.c.funnel": {"en": "Funnel: created → accepted → completed", "ru": "Воронка: создан → принят → выполнен",
                    "tj": "Қиф: сохта шуд → қабул шуд → иҷро шуд"},
    "an.c.help_types": {"en": "By type of help", "ru": "По видам помощи", "tj": "Аз рӯи навъи кумак"},
    "an.c.regions": {"en": "By region", "ru": "По регионам", "tj": "Аз рӯи минтақаҳо"},
    "an.c.priorities": {"en": "By priority", "ru": "По срочности", "tj": "Аз рӯи фаврият"},
    "an.c.delays": {"en": "Overdue and long-waiting requests", "ru": "Просроченные и долго ждущие запросы",
                    "tj": "Дархостҳои мӯҳлаташ гузашта ва дер интизоршуда"},
    "an.c.volunteer_activity": {"en": "Active volunteers", "ru": "Активные волонтёры", "tj": "Ихтиёриёни фаъол"},
    "an.c.workload": {"en": "Requests per volunteer", "ru": "Запросов на волонтёра", "tj": "Дархостҳо ба як ихтиёрӣ"},
    "an.c.availability": {"en": "Availability now", "ru": "Доступность сейчас", "tj": "Дастрасӣ ҳозир"},
    "an.c.top_volunteers": {"en": "Most completed requests", "ru": "Больше всего выполненных запросов",
                            "tj": "Бештарин дархостҳои иҷрошуда"},
    "an.c.emergencies": {"en": "SOS reports", "ru": "Сигналы SOS", "tj": "Сигналҳои SOS"},
    "an.c.ratings": {"en": "Rating distribution", "ru": "Распределение оценок", "tj": "Тақсимоти баҳоҳо"},
    "an.c.ratings_timeline": {"en": "Average rating over time", "ru": "Средняя оценка по дням",
                              "tj": "Баҳои миёна аз рӯи рӯзҳо"},
    "an.c.donations_status": {"en": "Donation offers by status", "ru": "Пожертвования по статусу",
                              "tj": "Хайрияҳо аз рӯи ҳолат"},
    "an.c.donations_category": {"en": "Donation offers by category", "ru": "Пожертвования по категориям",
                                "tj": "Хайрияҳо аз рӯи категория"},
    "an.c.registrations": {"en": "New registrations by role", "ru": "Новые регистрации по ролям",
                           "tj": "Бақайдгириҳои нав аз рӯи нақш"},
    "an.c.chat_flags_topics": {"en": "Chat safety flags by topic", "ru": "Флаги в чате по темам",
                               "tj": "Аломатҳо дар чат аз рӯи мавзӯъ"},
    "an.c.chat_flags_help_types": {"en": "Chat safety flags by type of help", "ru": "Флаги в чате по видам помощи",
                                   "tj": "Аломатҳо дар чат аз рӯи навъи кумак"},
    "an.n.chat_flags_topics": {"en": "Flags never block a message. A message can carry several topics.",
                               "ru": "Флаг не блокирует сообщение. У одного сообщения может быть несколько тем.",
                               "tj": "Аломат паёмро манъ намекунад. Як паём метавонад якчанд мавзӯъ дошта бошад."},
    "an.flag_summary": {"en": "Flagged messages: {n} of {total} ({share}%) · not reviewed yet: {open}",
                        "ru": "Сообщений с флагом: {n} из {total} ({share}%) · ещё не проверено: {open}",
                        "tj": "Паёмҳои бо аломат: {n} аз {total} ({share}%) · ҳанӯз санҷида нашуда: {open}"},
    "an.flag.sms_code": {"en": "SMS code / PIN", "ru": "Код из СМС / ПИН", "tj": "Коди SMS / рамз"},
    "an.flag.money": {"en": "Money and transfers", "ru": "Деньги и переводы", "tj": "Пул ва интиқол"},
    "an.flag.off_platform": {"en": "Other messenger", "ru": "Другой мессенджер", "tj": "Мессенҷери дигар"},
    "an.flag.phone": {"en": "Phone number", "ru": "Номер телефона", "tj": "Рақами телефон"},
    "an.s.flagged_messages": {"en": "Flagged messages", "ru": "Сообщения с флагом", "tj": "Паёмҳои бо аломат"},
    "an.c.heatmap": {"en": "Where requests come from", "ru": "Откуда приходят запросы", "tj": "Дархостҳо аз куҷо меоянд"},
    # chart notes
    "an.n.delays": {"en": "Overdue: in progress longer than 3 h (by accept day). Waiting: no volunteer for over 48 h (by creation day).",
                    "ru": "Просрочен: в работе дольше 3 ч (по дню принятия). Долго ждал: без волонтёра больше 48 ч (по дню создания).",
                    "tj": "Мӯҳлаташ гузашта: зиёда аз 3 соат дар кор (аз рӯи рӯзи қабул). Дер интизор: зиёда аз 48 соат бе ихтиёрӣ (аз рӯи рӯзи сохтан)."},
    "an.n.workload": {"en": "How the load is spread, not a contest.", "ru": "Как распределена нагрузка — не соревнование.",
                      "tj": "Сарбор чӣ тавр тақсим шудааст — на мусобиқа."},
    "an.n.top_volunteers": {"en": "Read together with the rating: quality matters as much as quantity.",
                            "ru": "Смотрите вместе с оценкой: качество важно не меньше количества.",
                            "tj": "Якҷоя бо баҳо бинед: сифат аз миқдор кам аҳамият надорад."},
    "an.n.ratings": {"en": "Visible reviews only.", "ru": "Только видимые отзывы.", "tj": "Танҳо фикрҳои намоён."},
    "an.n.availability": {"en": "Live, not limited to the period.", "ru": "Текущее состояние, без учёта периода.",
                          "tj": "Ҳолати ҷорӣ, бе назардошти давра."},
    "an.n.heatmap": {"en": "Locations are rounded to about 100 m.", "ru": "Координаты округлены примерно до 100 м.",
                     "tj": "Координатҳо тақрибан то 100 м яклухт карда шудаанд."},
    # series
    "an.s.created": {"en": "Created", "ru": "Создано", "tj": "Сохта шуд"},
    "an.s.accepted": {"en": "Accepted", "ru": "Принято", "tj": "Қабул шуд"},
    "an.s.completed": {"en": "Completed", "ru": "Выполнено", "tj": "Иҷро шуд"},
    "an.s.cancelled": {"en": "Cancelled", "ru": "Отменено", "tj": "Бекор шуд"},
    "an.s.requests": {"en": "Requests", "ru": "Запросы", "tj": "Дархостҳо"},
    "an.s.overdue": {"en": "Overdue", "ru": "Просрочено", "tj": "Мӯҳлаташ гузашта"},
    "an.s.stale": {"en": "Waited over 48 h", "ru": "Ждали больше 48 ч", "tj": "Зиёда аз 48 соат интизор"},
    "an.s.active_volunteers": {"en": "Volunteers who completed a request", "ru": "Волонтёры, выполнившие запрос",
                               "tj": "Ихтиёриёне, ки дархост иҷро карданд"},
    "an.s.volunteers": {"en": "Volunteers", "ru": "Волонтёры", "tj": "Ихтиёриён"},
    "an.s.sos": {"en": "SOS reports", "ru": "Сигналы SOS", "tj": "Сигналҳои SOS"},
    "an.s.reviews": {"en": "Reviews", "ru": "Отзывы", "tj": "Фикрҳо"},
    "an.s.avg_rating": {"en": "Average rating", "ru": "Средняя оценка", "tj": "Баҳои миёна"},
    "an.s.offers": {"en": "Offers", "ru": "Предложения", "tj": "Пешниҳодҳо"},
    "an.role.volunteers": {"en": "Volunteers", "ru": "Волонтёры", "tj": "Ихтиёриён"},
    "an.role.clients": {"en": "Clients", "ru": "Клиенты", "tj": "Мизоҷон"},
    "an.role.curators": {"en": "Curators", "ru": "Кураторы", "tj": "Кураторон"},
    "an.role.other": {"en": "Applicants / other", "ru": "Заявители / прочие", "tj": "Аризадиҳандагон / дигарон"},
    "an.priority.normal": {"en": "Normal", "ru": "Обычный", "tj": "Муқаррарӣ"},
    "an.priority.high": {"en": "High", "ru": "Высокий", "tj": "Баланд"},
    "an.priority.emergency": {"en": "Emergency", "ru": "Экстренный", "tj": "Фавқулода"},
    "an.stars.1": {"en": "1 star", "ru": "1 звезда", "tj": "1 ситора"},
    "an.stars.2": {"en": "2 stars", "ru": "2 звезды", "tj": "2 ситора"},
    "an.stars.3": {"en": "3 stars", "ru": "3 звезды", "tj": "3 ситора"},
    "an.stars.4": {"en": "4 stars", "ru": "4 звезды", "tj": "4 ситора"},
    "an.stars.5": {"en": "5 stars", "ru": "5 звёзд", "tj": "5 ситора"},
    # table / CSV columns
    "an.col.period": {"en": "Date", "ru": "Дата", "tj": "Сана"},
    "an.col.category": {"en": "Category", "ru": "Категория", "tj": "Категория"},
    "an.col.name": {"en": "Volunteer", "ru": "Волонтёр", "tj": "Ихтиёрӣ"},
    "an.col.region": {"en": "Region", "ru": "Регион", "tj": "Минтақа"},
    "an.col.completed": {"en": "Completed", "ru": "Выполнено", "tj": "Иҷро шуд"},
    "an.col.avg": {"en": "Average rating", "ru": "Средняя оценка", "tj": "Баҳои миёна"},
    "an.col.reviews": {"en": "Reviews", "ru": "Отзывов", "tj": "Фикрҳо"},
}


def ui_i18n_keys(lang) -> dict:
    """{key: text} of every ``an.*`` string for one language (incl. the help
    type labels, reused from notify_texts so there is one translation)."""
    flat = {key: texts[lang] for key, texts in UI_STRINGS.items()}
    help_labels = notify_texts.LABELS["help_type"].get(lang) or {}
    flat.update({f"an.help.{code}": help_labels.get(code, code) for code, _ in HELP_TYPE_CHOICES})
    return flat


def ui_table(lang) -> dict:
    """Labels for server-side output (CSV): the an.* keys plus the reused
    region / availability / donation tables."""
    lang = lang if lang in notify_texts.LANGS else "ru"
    table = ui_i18n_keys(lang)
    table.update({f"region.{c}": t for c, t in notify_texts.LABELS["region"][lang].items()})
    table.update({f"don.status_{c}": t for c, t in notify_texts.LABELS["donation_status"][lang].items()})
    from server.i18n_prerender import translation_table

    js = translation_table(lang)
    for code, _ in DONATION_CATEGORY_CHOICES:
        table.setdefault(f"don.category_{code}", js.get(f"don.category_{code}", code))
    for code, _ in Profile.AVAILABILITY_CHOICES:
        table.setdefault(f"availability.{code}", js.get(f"availability.{code}", code))
    return table
