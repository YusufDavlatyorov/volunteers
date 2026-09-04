"""Role-scoped dashboard data.

One entry point — ``for_user(user)`` — dispatches on the viewer's role and
returns a flat context dict for ``accounts/profile.html`` (which then includes
the matching ``myapp/dashboard/_<role>.html`` partial).

Every query here is filtered by the passed user. The data layer, not the
template, is what stops a volunteer/client from seeing another user's rows or
any CRM aggregate. Read-only throughout; reuses ``services.analytics`` /
``services.emergency`` / ``services.overdue`` / ``services.matching`` so no
aggregation logic is duplicated.
"""

from django.db.models import Count, Q
from django.utils import timezone

from ..models import Donation, EmergencyReport, Event, HelpRequest, PetReport, VolunteerApplication
from . import analytics, donations, emergency, matching, overdue, pets, stale

EVENTS_LIMIT = 3
LIST_LIMIT = 5
QUEUE_LIMIT = 8


def _upcoming_events(region):
    qs = Event.objects.filter(date__gte=timezone.now())
    if region:
        qs = qs.filter(Q(region=region) | Q(region="all"))
    return qs.select_related("curator").order_by("date")[:EVENTS_LIMIT]


def _public_volunteer(volunteer):
    """The only volunteer data a client is allowed to see: a display name and a
    region. Never phone / email / rating / other tasks."""
    if volunteer is None:
        return None
    profile = getattr(volunteer, "profile", None)
    return {
        "name": (profile.full_name if profile and profile.full_name else volunteer.username),
        "region": volunteer.get_region_display() or "",
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
        "upcoming_events": _upcoming_events(user.region),
    }


def _client(user):
    requests = list(
        user.client_requests.select_related("volunteer", "volunteer__profile").order_by("-created_at")
    )
    active = [r for r in requests if r.status in ("pending", "active")]
    completed = [r for r in requests if r.status == "completed"]
    current = next((r for r in active if r.status == "active"), None) or (active[0] if active else None)

    return {
        "dash_role": "client",
        "client_current": current,
        "client_current_volunteer": _public_volunteer(current.volunteer) if current else None,
        "client_active": active,
        "client_completed": completed[:LIST_LIMIT],
        "client_total": len(requests),
        "donation_count": Donation.objects.filter(donor=user).count(),
        "recent_donations": list(donations.donations_for(user)[:LIST_LIMIT]),
        "pet_report_count": PetReport.objects.filter(reporter=user).count(),
        "my_pet_reports": list(pets.reports_for(user)[:LIST_LIMIT]),
        "upcoming_events": _upcoming_events(user.region),
    }


def _attach_assignment_suggestions(*task_lists):
    """Attach the single best-matching volunteer to each pending task as
    ``task.assignment_suggestion`` (a compact dict, or None), so the curator
    dashboard can show "send this to X" next to a stuck request.

    Read-only — it is exactly ``matching.recommend_volunteers(task, limit=1)``,
    which never assigns or notifies. Bounded: only runs over the already-capped
    (<= QUEUE_LIMIT each) dashboard queues, and memoises by task id so a request
    that appears in both the "stalled" and "unassigned" lists is scored once.
    The actual assign / notify actions stay on the task detail page.
    """
    scored = {}
    for tasks in task_lists:
        for task in tasks:
            if task.id not in scored:
                picks = matching.recommend_volunteers(task, limit=1) if task.has_location else []
                if picks:
                    top = picks[0]
                    volunteer = top["volunteer"]
                    profile = getattr(volunteer, "profile", None)
                    scored[task.id] = {
                        "name": (profile.full_name if profile and profile.full_name else volunteer.username),
                        "distance_km": top["distance_km"],
                        "score": top["score"],
                        "skill_match": top["skill_match"] == "match",
                    }
                else:
                    scored[task.id] = None
            task.assignment_suggestion = scored[task.id]


def _curator(user):
    stats = analytics.dashboard_stats()
    stale_pending = list(stale.currently_stale_pending()[:QUEUE_LIMIT])
    unassigned = list(
        HelpRequest.objects.filter(status="pending")
        .select_related("client")
        .order_by("-is_urgent", "created_at")[:QUEUE_LIMIT]
    )
    _attach_assignment_suggestions(stale_pending, unassigned)
    return {
        "dash_role": "curator",
        "stats": stats,
        "open_emergencies": emergency.open_reports()[:LIST_LIMIT],
        # Preserved context key — Stage 4 dashboard tests assert on it.
        "open_emergency_count": stats["emergencies_open"],
        "overdue_tasks": overdue.currently_overdue_tasks()[:QUEUE_LIMIT],
        # Pending requests that have waited past STALE_PENDING_THRESHOLD — the
        # same "attention queue" treatment as overdue_tasks. Detected/alerted by
        # the check_stale_requests cron; shown here so a curator can act before
        # (or after) the alert lands. Each carries .assignment_suggestion.
        "stale_pending_tasks": stale_pending,
        "unassigned_tasks": unassigned,
        "pending_applications": (
            VolunteerApplication.objects.filter(status=VolunteerApplication.STATUS_PENDING)
            .select_related("user")
            .order_by("-created_at")[:LIST_LIMIT]
        ),
        "availability": analytics.volunteer_availability_breakdown(),
        "region_breakdown": analytics.region_task_breakdown(),
        "recent_activity": analytics.recent_activity(limit=8),
        # Lost & Found board — a coordination surface, not a CRM aggregate. Open
        # count + the newest few so a curator can keep an eye on the board.
        "pets_open_count": PetReport.objects.filter(status__in=PetReport.OPEN_STATUSES).count(),
        "recent_pet_reports": list(
            PetReport.objects.filter(status__in=PetReport.OPEN_STATUSES)
            .order_by("-created_at")[:LIST_LIMIT]
        ),
    }


def _admin(user):
    data = _curator(user)
    data["dash_role"] = "admin"
    data["users_by_role"] = analytics.users_by_role()
    data["platform_totals"] = analytics.platform_totals()
    data["donation_summary"] = donations.donation_summary()
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
    # application is still pending) — the profile.html shell shows the
    # application card and the shared right rail, nothing role-specific.
    return {"dash_role": "guest"}
