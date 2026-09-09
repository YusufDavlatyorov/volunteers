"""CRM aggregate queries — kept out of views.py so the dashboard and
task-management views stay thin. Every function here is read-only and uses
aggregate/annotated queries (Count/Max with a `filter=`) instead of looping
in Python, so the number of SQL queries stays constant regardless of how
many volunteers/tasks exist.
"""

from django.db.models import Count
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from accounts.models import Profile, REGION_CHOICES, Users
from ..models import (
    EmergencyReport,
    HelpRequest,
    VolunteerApplication,
)
from .overdue import currently_overdue_q
from .stale import currently_stale_pending_q


def volunteer_availability_breakdown():
    """{'available': n, 'busy': n, 'offline': n} for active volunteers."""
    breakdown = {choice: 0 for choice, _ in Profile.AVAILABILITY_CHOICES}
    rows = (
        Users.objects.filter(is_volunteer=True, is_active=True)
        .values("profile__availability_status")
        .annotate(count=Count("id"))
    )
    for row in rows:
        status = row["profile__availability_status"] or Profile.AVAILABILITY_AVAILABLE
        breakdown[status] = breakdown.get(status, 0) + row["count"]
    return breakdown


def task_status_breakdown():
    """Counts of tasks per lifecycle bucket, plus overdue/urgent. Overdue and
    stale are derived (not their own status value), so they reuse the one
    predicate each — ``overdue.currently_overdue_q`` / ``stale.currently_stale_pending_q``
    — rather than re-inlining the threshold comparison here."""
    counts = HelpRequest.objects.aggregate(
        pending=Count("id", filter=Q(status="pending")),
        active=Count("id", filter=Q(status="active")),
        completed=Count("id", filter=Q(status="completed")),
        cancelled=Count("id", filter=Q(status="cancelled")),
        urgent=Count("id", filter=Q(is_urgent=True, status__in=["pending", "active"])),
        overdue=Count("id", filter=currently_overdue_q()),
        stale=Count("id", filter=currently_stale_pending_q()),
    )
    return counts


def emergency_breakdown():
    """{'open': n, 'acknowledged': n, 'active': open+acknowledged} — the SOS
    reports that still need staff attention."""
    counts = EmergencyReport.objects.aggregate(
        open=Count("id", filter=Q(status=EmergencyReport.STATUS_OPEN)),
        acknowledged=Count("id", filter=Q(status=EmergencyReport.STATUS_ACKNOWLEDGED)),
    )
    counts["active"] = counts["open"] + counts["acknowledged"]
    return counts


def dashboard_stats():
    """Every number the CRM dashboard tiles need, in one call."""
    availability = volunteer_availability_breakdown()
    tasks = task_status_breakdown()
    emergencies = emergency_breakdown()
    today_start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "emergencies_open": emergencies["open"],
        "emergencies_acknowledged": emergencies["acknowledged"],
        "emergencies_active": emergencies["active"],
        "volunteers_total": sum(availability.values()),
        "volunteers_available": availability.get(Profile.AVAILABILITY_AVAILABLE, 0),
        "volunteers_busy": availability.get(Profile.AVAILABILITY_BUSY, 0),
        "volunteers_offline": availability.get(Profile.AVAILABILITY_OFFLINE, 0),
        "clients_total": Users.objects.filter(is_client=True, is_active=True).count(),
        "tasks_pending": tasks["pending"],
        "tasks_active": tasks["active"],
        "tasks_completed": tasks["completed"],
        "tasks_overdue": tasks["overdue"],
        "tasks_stale": tasks["stale"],
        "tasks_urgent": tasks["urgent"],
        "tasks_completed_today": HelpRequest.objects.filter(
            status="completed", completed_at__gte=today_start
        ).count(),
        "pending_applications": VolunteerApplication.objects.filter(
            status=VolunteerApplication.STATUS_PENDING
        ).count(),
    }


def users_by_role():
    """Active user counts per role, for the admin platform view. Admin is
    ``is_superuser`` (not a role flag), so it is counted separately and excluded
    from the curator tally."""
    return Users.objects.filter(is_active=True).aggregate(
        admins=Count("id", filter=Q(is_superuser=True)),
        curators=Count("id", filter=Q(is_curator=True, is_superuser=False)),
        volunteers=Count("id", filter=Q(is_volunteer=True)),
        clients=Count("id", filter=Q(is_client=True)),
    )


def platform_totals():
    """All-time request totals for the admin platform view."""
    return HelpRequest.objects.aggregate(
        requests_total=Count("id"),
        requests_completed=Count("id", filter=Q(status="completed")),
    )


def region_task_breakdown():
    """Pending/active task counts per region — the operational picture for the
    curator dashboard. One annotated query, not a loop."""
    labels = dict(REGION_CHOICES)
    rows = (
        HelpRequest.objects.filter(status__in=["pending", "active"])
        .values("region")
        .annotate(
            pending=Count("id", filter=Q(status="pending")),
            active=Count("id", filter=Q(status="active")),
        )
        .order_by("region")
    )
    return [
        {
            "region": row["region"],
            "label": labels.get(row["region"]) or (row["region"] or "—"),
            "pending": row["pending"],
            "active": row["active"],
        }
        for row in rows
    ]


def recent_activity(limit=10):
    """A unified recent-activity feed built from existing models only — no
    ActivityLog model yet (that belongs to a later phase). Each source query
    is independently limited (constant number of queries, not one per row),
    then merged and re-sorted in Python. Each entry: {type, message, timestamp, url}.
    """
    events = []

    for application in VolunteerApplication.objects.select_related("user").order_by("-created_at")[:limit]:
        events.append({
            "type": "application",
            "message": f"{application.user.username} подал заявку на волонтёрство",
            "timestamp": application.created_at,
            "url": reverse("volunteer_application_detail", args=[application.pk]),
        })

    for task in HelpRequest.objects.select_related("client").order_by("-created_at")[:limit]:
        events.append({
            "type": "task_created",
            "message": f"Новый запрос #{task.id} от {task.client.username}",
            "timestamp": task.created_at,
            "url": reverse("task_detail", args=[task.pk]),
        })

    for task in (
        HelpRequest.objects.filter(accepted_at__isnull=False)
        .select_related("volunteer")
        .order_by("-accepted_at")[:limit]
    ):
        events.append({
            "type": "task_accepted",
            "message": f"Запрос #{task.id} принят волонтёром {task.volunteer.username if task.volunteer else '—'}",
            "timestamp": task.accepted_at,
            "url": reverse("task_detail", args=[task.pk]),
        })

    for task in HelpRequest.objects.filter(completed_at__isnull=False).order_by("-completed_at")[:limit]:
        events.append({
            "type": "task_completed",
            "message": f"Запрос #{task.id} выполнен",
            "timestamp": task.completed_at,
            "url": reverse("task_detail", args=[task.pk]),
        })

    for task in HelpRequest.objects.filter(alarm_sent=True, status="active").order_by("-updated_at")[:limit]:
        events.append({
            "type": "task_overdue",
            "message": f"Запрос #{task.id} просрочен (в работе более 3 часов)",
            "timestamp": task.updated_at,
            "url": reverse("task_detail", args=[task.pk]),
        })

    events.sort(key=lambda event: event["timestamp"], reverse=True)
    return events[:limit]
