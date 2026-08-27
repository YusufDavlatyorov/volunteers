"""Smart Volunteer Matching — deterministic, explainable nearest-volunteer
scoring for admin/curator dispatch decisions.

This is intentionally NOT the LLM-based conversational assistant in
myapp/views.py::ai_chat_view — it never talks to an external API, never
hallucinates, and never assigns anything. It is plain scoring logic over
Profile/HelpRequest data, cheap enough to run on every page view and easy to
unit-test without mocking a network call. Admin/curator still have to choose
who to notify; nothing here touches HelpRequest.accept()/assign a task.
"""

from django.db.models import Count, Q
from django.utils import timezone

from accounts.models import Profile, Users
from .geo import haversine_km

# How much each signal contributes to the final 0-100 score. Urgent tasks
# lean harder on distance ("fastest/closest suitable volunteer") and lighter
# on region/workload — speed matters more than a perfect fit when it's urgent.
NORMAL_WEIGHTS = {"distance": 0.40, "availability": 0.30, "workload": 0.15, "region": 0.10, "freshness": 0.05}
URGENT_WEIGHTS = {"distance": 0.55, "availability": 0.25, "workload": 0.10, "region": 0.05, "freshness": 0.05}

DISTANCE_CAP_KM = 40  # beyond this a volunteer is technically included but the distance component floors at 0
WORKLOAD_CAP = 3  # active tasks at/above this floor the workload component at 0
AVERAGE_SPEED_KMH = 30  # rough regional estimate for the "estimated minutes" shown in the list

FRESH_WITHIN = timezone.timedelta(minutes=30)
RECENT_WITHIN = timezone.timedelta(hours=24)
AGING_WITHIN = timezone.timedelta(days=7)

AVAILABILITY_SCORE = {
    Profile.AVAILABILITY_AVAILABLE: 1.0,
    Profile.AVAILABILITY_BUSY: 0.3,
    # AVAILABILITY_OFFLINE volunteers never reach scoring — filtered out below.
}

CLOSE_KM = 2
NEARBY_KM = 10


def _distance_score(distance_km):
    return max(0.0, 1.0 - min(distance_km, DISTANCE_CAP_KM) / DISTANCE_CAP_KM)


def _workload_score(active_task_count):
    return max(0.0, 1.0 - min(active_task_count, WORKLOAD_CAP) / WORKLOAD_CAP)


def _freshness(location_updated_at, now):
    """Returns (score, label). An unknown or very old timestamp is treated as
    unreliable, not as "close enough" — a stale pin should not read as a live
    position."""
    if location_updated_at is None:
        return 0.2, "unknown"
    age = now - location_updated_at
    if age <= FRESH_WITHIN:
        return 1.0, "fresh"
    if age <= RECENT_WITHIN:
        return 0.8, "recent"
    if age <= AGING_WITHIN:
        return 0.5, "aging"
    return 0.2, "stale"


def location_freshness_label(location_updated_at, now=None):
    """Public wrapper around the same freshness rule used for scoring, for
    display elsewhere (e.g. the CRM volunteer profile) — one threshold
    definition, reused rather than re-implemented."""
    _, label = _freshness(location_updated_at, now or timezone.now())
    return label


def _reasons(distance_km, availability, active_task_count, same_region, freshness_label):
    reasons = []
    if distance_km <= CLOSE_KM:
        reasons.append("Совсем рядом")
    elif distance_km <= NEARBY_KM:
        reasons.append("Недалеко от задачи")
    reasons.append("Доступен сейчас" if availability == Profile.AVAILABILITY_AVAILABLE else "Сейчас занят")
    reasons.append("Нет активных задач" if active_task_count == 0 else f"{active_task_count} активных задач(и)")
    if same_region:
        reasons.append("Тот же регион, что и запрос")
    if freshness_label in ("aging", "stale"):
        reasons.append("Местоположение может быть устаревшим")
    elif freshness_label == "unknown":
        reasons.append("Местоположение никогда не обновлялось")
    return reasons


def recommend_volunteers(task, limit=5):
    """Rank available volunteers for `task` by suitability. Read-only — never
    assigns, notifies, or mutates anything.

    Returns a list of dicts (best first, at most `limit`), each with:
      volunteer, distance_km, estimated_minutes, score (0-100), availability,
      active_task_count, same_region, location_freshness, reasons (list[str]).

    Excluded entirely (not merely scored low): offline volunteers, and
    volunteers with no saved location — a distance-based recommendation is
    meaningless without one. Returns [] if the task itself has no location.
    """
    if not task.has_location:
        return []

    now = timezone.now()
    weights = URGENT_WEIGHTS if task.is_urgent else NORMAL_WEIGHTS

    candidates = (
        Users.objects.filter(is_volunteer=True, is_active=True)
        .exclude(profile__availability_status=Profile.AVAILABILITY_OFFLINE)
        .filter(profile__latitude__isnull=False, profile__longitude__isnull=False)
        .select_related("profile")
        .annotate(
            active_task_count=Count(
                "volunteer_tasks", filter=Q(volunteer_tasks__status="active"), distinct=True
            )
        )
    )
    if task.volunteer_id:
        # Already assigned — recommending them again is a no-op for the curator.
        candidates = candidates.exclude(pk=task.volunteer_id)

    task_lat, task_lng = float(task.latitude), float(task.longitude)
    ranked = []

    for volunteer in candidates:
        profile = volunteer.profile
        distance_km = round(
            haversine_km(task_lat, task_lng, float(profile.latitude), float(profile.longitude)), 2
        )
        availability = profile.availability_status
        same_region = bool(task.region) and volunteer.region == task.region
        freshness_score, freshness_label = _freshness(profile.location_updated_at, now)

        component_scores = {
            "distance": _distance_score(distance_km),
            "availability": AVAILABILITY_SCORE.get(availability, 0.0),
            "workload": _workload_score(volunteer.active_task_count),
            "region": 1.0 if same_region else 0.4,
            "freshness": freshness_score,
        }
        score = round(sum(component_scores[key] * weights[key] for key in weights) * 100, 1)

        ranked.append({
            "volunteer": volunteer,
            "distance_km": distance_km,
            "estimated_minutes": round((distance_km / AVERAGE_SPEED_KMH) * 60, 1),
            "score": score,
            "availability": availability,
            "active_task_count": volunteer.active_task_count,
            "same_region": same_region,
            "location_freshness": freshness_label,
            "location_updated_at": profile.location_updated_at,
            "reasons": _reasons(distance_km, availability, volunteer.active_task_count, same_region, freshness_label),
        })

    if ranked:
        closest = min(item["distance_km"] for item in ranked)
        for item in ranked:
            if item["distance_km"] == closest:
                item["reasons"].insert(0, "Ближайший подходящий волонтёр")

    ranked.sort(key=lambda item: (-item["score"], item["distance_km"]))
    return ranked[:limit]
