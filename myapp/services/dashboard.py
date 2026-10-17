"""Role-scoped dashboard data.

One entry point — ``for_user(user)`` — dispatches on the viewer's role and
returns a flat context dict for ``myapp/dashboard.html`` (which then includes
the matching ``myapp/dashboard/_<role>.html`` partial). The profile page
(``accounts/profile.html``, "who I am") does not use it.

Every query here is filtered by the passed user. The data layer, not the
template, is what stops a volunteer/client from seeing another user's rows or
any CRM aggregate. Read-only throughout; reuses ``services.analytics`` /
``services.emergency`` / ``services.overdue`` / ``services.matching`` so no
aggregation logic is duplicated.
"""

from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.utils import timezone

from accounts.models import Profile

from ..models import HELP_TYPE_CHOICES, Donation, EmergencyReport, Event, HelpRequest, PetReport, VolunteerApplication
from . import analytics, analytics_v2, chat, donations, emergency, feedback, matching, overdue, pets, photo_reports, rating, stale

EVENTS_LIMIT = 3
LIST_LIMIT = 5
QUEUE_LIMIT = 8
# Staff dashboard cards: the request queue shows this many rows, the compact
# list cards (SOS, overdue, applications, offers, Lost & Found) this many items —
# the full lists are one click away. Cut here, never in the template.
DASH_QUEUE_LIMIT = 11
CARD_LIST_LIMIT = 4
# /panel/ request table: rows per page (the old two grids were cut at 100).
PANEL_PAGE_SIZE = 20
# /panel/ tabs → HelpRequest.status, and each tab's row order: urgent first,
# then the longest waiting (free) / longest running (busy, so overdue on top).
PANEL_TABS = {
    "free": ("pending", ("-is_urgent", "created_at", "id")),
    "busy": ("active", ("-is_urgent", "accepted_at", "id")),
}


def _upcoming_events(region):
    qs = Event.objects.filter(date__gte=timezone.now())
    if region:
        qs = qs.filter(Q(region=region) | Q(region="all"))
    return qs.select_related("curator").order_by("date")[:EVENTS_LIMIT]


def _public_volunteer(volunteer):
    """The only volunteer data a client is allowed to see: a display name, a
    region and the public review average/count (``feedback.public_summary`` —
    never comments). Never phone / email / points rating / other tasks."""
    if volunteer is None:
        return None
    profile = getattr(volunteer, "profile", None)
    reviews = feedback.public_summary(profile)
    return {
        "name": (profile.full_name if profile and profile.full_name else volunteer.username),
        "region": volunteer.get_region_display() or "",
        "review_avg": reviews["avg"],
        "review_count": reviews["count"],
    }


def _volunteer(user):
    tasks = user.volunteer_tasks.select_related("client")
    active_task = tasks.filter(status="active").first()

    active_task_emergency = None
    if active_task:
        active_task_emergency = (
            EmergencyReport.objects.filter(
                help_request=active_task, status__in=EmergencyReport.OPEN_STATUSES
            )
            .order_by("-created_at")
            .first()
        )

    month_start = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    counts = tasks.aggregate(
        completed=Count("id", filter=Q(status="completed")),
        completed_month=Count("id", filter=Q(status="completed", completed_at__gte=month_start)),
    )

    return {
        "dash_role": "volunteer",
        "active_task": active_task,
        # Unread chat messages on the active task (one query; {} without one).
        "chat_unread": chat.unread_counts(user, [active_task.pk]) if active_task else {},
        "active_task_emergency": active_task_emergency,
        "vol_completed": counts["completed"],
        "vol_completed_month": counts["completed_month"],
        "vol_points": user.profile.rating,
        # A volunteer holding an active task can't accept another, so there is
        # nothing to recommend. recommend_tasks() enforces this itself; the
        # short-circuit here just skips the query when we already know.
        "recommended_tasks": [] if active_task else matching.recommend_tasks(user, limit=3),
        "recent_completed": tasks.filter(status="completed").order_by("-completed_at")[:LIST_LIMIT],
        "my_emergencies": (
            EmergencyReport.objects.filter(volunteer=user)
            .select_related("help_request")
            .order_by("-created_at")[:LIST_LIMIT]
        ),
        # In-kind offers this volunteer has been asked to collect or deliver.
        "assigned_donations": list(donations.assigned_to(user)[:LIST_LIMIT]),
        # Their photo reports with the CRM review status (services/photo_reports).
        "my_photo_reports": photo_reports.recent_for_author(user, LIST_LIMIT),
        "upcoming_events": _upcoming_events(user.region),
    }


def _client(user):
    requests = list(
        user.client_requests.select_related("volunteer", "volunteer__profile").order_by("-created_at")
    )
    active = [r for r in requests if r.status in ("pending", "active")]
    # History = finished requests, incl. cancelled ones (so a cancelled request
    # doesn't silently vanish from the client's dashboard).
    completed = [r for r in requests if r.status in ("completed", "cancelled")]
    current = next((r for r in active if r.status == "active"), None) or (active[0] if active else None)

    return {
        "dash_role": "client",
        "client_current": current,
        "client_current_volunteer": _public_volunteer(current.volunteer) if current else None,
        # {task_id: unread chat messages} for every listed request — ONE query
        # however many requests the client has (chat.unread_counts).
        "chat_unread": chat.unread_counts(
            user, [r.pk for r in active + completed[:LIST_LIMIT] if r.volunteer_id]
        ),
        "client_active": active,
        "client_completed": completed[:LIST_LIMIT],
        "client_total": len(requests),
        "donation_count": Donation.objects.filter(donor=user).count(),
        "recent_donations": list(donations.donations_for(user)[:LIST_LIMIT]),
        "pet_report_count": PetReport.objects.filter(reporter=user).count(),
        "my_pet_reports": list(pets.reports_for(user)[:LIST_LIMIT]),
        "upcoming_events": _upcoming_events(user.region),
    }


def attach_assignment_suggestions(*task_lists):
    """Attach the single best-matching volunteer to each pending task as
    ``task.assignment_suggestion`` (a compact dict, or None), so staff see
    "send this to X" next to a stuck request (dashboard queue, /panel/ table).

    Read-only — the same answer as ``matching.recommend_volunteers(task,
    limit=1)``, which never assigns or notifies, but computed for all the tasks
    from ONE candidate query (``matching.best_volunteer_per_task``). A request
    that appears in several lists is scored once. The actual assign / notify
    actions stay on the task detail page.
    """
    unique = {task.id: task for tasks in task_lists for task in tasks}
    picks = matching.best_volunteer_per_task(list(unique.values()))
    scored = {}
    for task_id, top in picks.items():
        if top:
            volunteer = top["volunteer"]
            profile = getattr(volunteer, "profile", None)
            scored[task_id] = {
                "name": (profile.full_name if profile and profile.full_name else volunteer.username),
                "distance_km": top["distance_km"],
                "score": top["score"],
                "skill_match": top["skill_match"] == "match",
            }
        else:
            scored[task_id] = None
    for tasks in task_lists:
        for task in tasks:
            task.assignment_suggestion = scored[task.id]


def _person(user):
    """Display name + initials for a queue row avatar (``client__profile`` is
    select_related by the caller — no query here)."""
    profile = getattr(user, "profile", None)
    name = (profile.full_name if profile and profile.full_name else "") or user.username
    return {"name": name, "initials": "".join(word[0] for word in name.split()[:2]).upper() or "?"}


def _queue(unassigned):
    """The staff dashboard's request queue card: the first DASH_QUEUE_LIMIT
    pending requests of the already loaded ``unassigned`` list — urgent first,
    then the longest-waiting (so the stalled ones), oldest first — each flagged
    ``stale`` past STALE_PENDING_THRESHOLD. No extra query."""
    return [
        {"task": task, "stale": task.is_stale_pending, "client": _person(task.client)}
        for task in unassigned[:DASH_QUEUE_LIMIT]
    ]


def _queue_categories():
    """``[(help_type, count)]`` over the WHOLE pending queue (not the rows the
    card shows), in HELP_TYPE_CHOICES order — the card's category tabs, each a
    link to the full queue filtered by it. One grouped query."""
    counts = dict(
        HelpRequest.objects.filter(status="pending").order_by()
        .values("help_type").annotate(n=Count("id")).values_list("help_type", "n")
    )
    return [(key, counts[key]) for key, _ in HELP_TYPE_CHOICES if counts.get(key)]


def _region_share(breakdown):
    """Open (pending + active) requests per region with their share of the
    total, largest first — the dashboard doughnut + its legend. Pure Python
    over ``region_task_breakdown()``; rows with nothing open are dropped."""
    rows = [{"region": r["region"], "label": r["label"], "count": r["pending"] + r["active"]} for r in breakdown]
    rows = [row for row in rows if row["count"]]
    total = sum(row["count"] for row in rows)
    for row in rows:
        row["pct"] = round(row["count"] * 100 / total) if total else 0
    rows.sort(key=lambda row: (-row["count"], row["region"]))
    return rows, total


def _curator(user):
    stats = analytics.dashboard_stats()
    stale_pending = list(stale.currently_stale_pending().select_related("client__profile")[:QUEUE_LIMIT])
    unassigned = list(
        HelpRequest.objects.filter(status="pending")
        .select_related("client", "client__profile")
        # Enough rows for the queue card (DASH_QUEUE_LIMIT) as well.
        .order_by("-is_urgent", "created_at")[:max(QUEUE_LIMIT, DASH_QUEUE_LIMIT)]
    )
    attach_assignment_suggestions(stale_pending, unassigned)
    queue = _queue(unassigned)
    region_breakdown = analytics.region_task_breakdown()
    region_share, region_open_total = _region_share(region_breakdown)
    leaders = rating.top(1)
    return {
        "dash_role": "curator",
        "stats": stats,
        "open_emergencies": emergency.open_reports()[:CARD_LIST_LIMIT],
        # Preserved context key — Stage 4 dashboard tests assert on it.
        "open_emergency_count": stats["emergencies_open"],
        "overdue_tasks": overdue.currently_overdue_tasks()[:CARD_LIST_LIMIT],
        # Pending requests that have waited past STALE_PENDING_THRESHOLD — the
        # same "attention queue" treatment as overdue_tasks. Detected/alerted by
        # the check_stale_requests cron; shown here so a curator can act before
        # (or after) the alert lands. Each carries .assignment_suggestion.
        "stale_pending_tasks": stale_pending,
        "unassigned_tasks": unassigned,
        "pending_applications": (
            VolunteerApplication.objects.filter(status=VolunteerApplication.STATUS_PENDING)
            .select_related("user")
            .order_by("-created_at")[:CARD_LIST_LIMIT]
        ),
        # Same numbers dashboard_stats() already computed — no second query.
        "availability": {
            Profile.AVAILABILITY_AVAILABLE: stats["volunteers_available"],
            Profile.AVAILABILITY_BUSY: stats["volunteers_busy"],
            Profile.AVAILABILITY_OFFLINE: stats["volunteers_offline"],
        },
        "region_breakdown": region_breakdown,
        "region_share": region_share,
        "region_open_total": region_open_total,
        # The queue card: DASH_QUEUE_LIMIT rows (see _queue); the tabs count the whole queue.
        "queue": queue,
        "queue_categories": _queue_categories(),
        "recent_activity": analytics.recent_activity(limit=8),
        # Place 1 of the public rating (services/rating.py — the same cached,
        # all-time ranking as /rating/; None when nobody has completed a request).
        "rating_leader": leaders[0] if leaders else None,
        # Lost & Found board — a coordination surface, not a CRM aggregate. Open
        # count + the newest few so a curator can keep an eye on the board.
        "pets_open_count": PetReport.objects.filter(status__in=PetReport.OPEN_STATUSES).count(),
        "recent_pet_reports": list(
            PetReport.objects.filter(status__in=PetReport.OPEN_STATUSES)
            .order_by("-created_at")[:CARD_LIST_LIMIT]
        ),
        # In-kind donation offers waiting for a curator's review + a compact
        # operational summary (item counts, never money).
        "offer_summary": donations.offer_summary(),
        "donation_offers_pending": list(donations.pending_offers()[:CARD_LIST_LIMIT]),
        # 14-day requests sparkline (cached aggregate; links to CRM analytics).
        "trend": analytics_v2.dashboard_trend(),
    }


def _admin(user):
    data = _curator(user)
    data["dash_role"] = "admin"
    data["users_by_role"] = analytics.users_by_role()
    data["platform_totals"] = analytics.platform_totals()
    data["total_events"] = Event.objects.count()
    data["recent_applications"] = (
        VolunteerApplication.objects.select_related("user").order_by("-created_at")[:LIST_LIMIT]
    )
    return data


def for_user(user):
    """Return the dashboard context dict for `user`, scoped to their role."""
    if user.is_superuser:
        return _admin(user)
    if user.is_curator:
        return _curator(user)
    if user.is_volunteer:
        return _volunteer(user)
    if user.is_client:
        return _client(user)
    # Authenticated but role-less (e.g. a registered user whose volunteer
    # application is still pending) — the dashboard.html shell shows the
    # application card and Quick Actions, nothing role-specific.
    return {"dash_role": "guest"}


def panel_requests(requests, tab, page_number):
    """The /panel/ request table over the already filtered ``requests``
    queryset: both tab counters from ONE aggregate, the ``tab`` rows (free =
    pending, busy = active) one PANEL_PAGE_SIZE page at a time, each row in the
    dashboard queue's shape — client, "waiting long" / overdue flag, and the
    top candidate (free) or the assigned volunteer (busy). Query count is
    fixed: aggregate + page + (free) one candidate query."""
    tab = tab if tab in PANEL_TABS else "free"
    counts = requests.order_by().aggregate(
        free=Count("id", filter=Q(status="pending")),
        busy=Count("id", filter=Q(status="active")),
    )
    status, ordering = PANEL_TABS[tab]
    rows = requests.filter(status=status).select_related("client__profile", "volunteer__profile").order_by(*ordering)
    paginator = Paginator(rows, PANEL_PAGE_SIZE)
    paginator.count = counts[tab]  # already counted by the aggregate above — no second COUNT(*)
    page = paginator.get_page(page_number)
    tasks = list(page.object_list)
    if tab == "free":
        attach_assignment_suggestions(tasks)
    return {
        "tab": tab,
        "tab_counts": counts,
        "page_obj": page,
        "rows": [
            {
                "task": task,
                "client": _person(task.client),
                "stale": task.is_stale_pending,
                "overdue": task.is_overdue,
                "volunteer": _person(task.volunteer) if task.volunteer_id else None,
            }
            for task in tasks
        ],
    }
