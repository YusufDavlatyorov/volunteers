"""Smart Volunteer Matching — deterministic, explainable nearest-volunteer
scoring for admin/curator dispatch decisions.

This is intentionally NOT the LLM-based conversational assistant in
myapp/views.py::ai_chat_view — it never talks to an external API, never
hallucinates, and never assigns anything. It is plain scoring logic over
Profile/HelpRequest data, cheap enough to run on every page view and easy to
unit-test without mocking a network call. Admin/curator still have to choose
who to notify; nothing here touches HelpRequest.accept()/assign a task.
"""

import math

from django.db.models import Count, F, FloatField, Q
from django.db.models.functions import Abs, Cast
from django.utils import timezone

from accounts.models import Profile, Users
from ..models import HelpRequest
from .geo import haversine_km

# How much each signal contributes to the final 0-100 score. Urgent tasks
# lean harder on distance ("fastest/closest suitable volunteer") and lighter
# on region/workload/skills — speed matters more than a perfect fit when it's urgent.
NORMAL_WEIGHTS = {"distance": 0.34, "availability": 0.26, "workload": 0.14, "region": 0.09, "skills": 0.12, "freshness": 0.05}
URGENT_WEIGHTS = {"distance": 0.50, "availability": 0.22, "workload": 0.10, "region": 0.05, "skills": 0.08, "freshness": 0.05}

DISTANCE_CAP_KM = 40  # beyond this a volunteer is technically included but the distance component floors at 0
WORKLOAD_CAP = 3  # active tasks at/above this floor the workload component at 0
# Upper bound on how many located volunteers a single recommend_volunteers()
# call scores. Applied to the volunteers *nearest* the task (SQL proximity
# pre-filter, exactly the trick recommend_tasks uses the other direction), so on
# a large deployment one dispatch/dashboard call can't load and haversine the
# whole volunteer directory. Generous: any realistic region has far fewer than
# this many available volunteers with a saved pin, so the true top-`limit` is
# never dropped — the cap only bites on an implausibly dense directory.
CANDIDATE_CAP = 75
# Haversine estimate only — calling OSRM for every candidate on every page load
# is too slow. The precise driving ETA is on the route card once a volunteer is assigned.
AVERAGE_SPEED_KMH = 30

SKILL_SCORE_MATCH = 1.0
SKILL_SCORE_NEUTRAL = 0.6   # volunteer has listed no skills — "no info", not a penalty
SKILL_SCORE_MISMATCH = 0.3  # has skills, none cover this task's help type

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


def _skill_score(profile_skills, task_help_type):
    """Returns (score, label). Empty skills -> neutral, never a penalty."""
    if not profile_skills:
        return SKILL_SCORE_NEUTRAL, "none"
    if task_help_type in profile_skills:
        return SKILL_SCORE_MATCH, "match"
    return SKILL_SCORE_MISMATCH, "mismatch"


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


def _reasons(distance_km, availability, active_task_count, same_region, freshness_label, skill_label):
    reasons = []
    if distance_km <= CLOSE_KM:
        reasons.append("Совсем рядом")
    elif distance_km <= NEARBY_KM:
        reasons.append("Недалеко от задачи")
    reasons.append("Доступен сейчас" if availability == Profile.AVAILABILITY_AVAILABLE else "Сейчас занят")
    reasons.append("Нет активных задач" if active_task_count == 0 else f"{active_task_count} активных задач(и)")
    if same_region:
        reasons.append("Тот же регион, что и запрос")
    if skill_label == "match":
        reasons.append("Подходит по навыкам")
    elif skill_label == "mismatch":
        reasons.append("Навыки не совпадают с типом запроса")
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

    # Bound the work: keep only the CANDIDATE_CAP volunteers nearest the task.
    # Approximate distance in SQL with absolute degree offsets (longitude scaled
    # by cos(latitude) so both axes carry their real length here) — this only
    # decides which rows survive the cap; the exact haversine ranking still runs
    # in Python below. Same approach and trade-off as recommend_tasks().
    lng_weight = math.cos(math.radians(task_lat)) or 1.0
    proximity = Abs(Cast(F("profile__latitude"), FloatField()) - task_lat) + (
        Abs(Cast(F("profile__longitude"), FloatField()) - task_lng) * lng_weight
    )
    candidates = candidates.alias(_proximity=proximity).order_by("_proximity", "id")[:CANDIDATE_CAP]

    ranked = []

    for volunteer in candidates:
        profile = volunteer.profile
        distance_km = round(
            haversine_km(task_lat, task_lng, float(profile.latitude), float(profile.longitude)), 2
        )
        availability = profile.availability_status
        same_region = bool(task.region) and volunteer.region == task.region
        freshness_score, freshness_label = _freshness(profile.location_updated_at, now)
        skill_score, skill_label = _skill_score(profile.skills, task.help_type)

        component_scores = {
            "distance": _distance_score(distance_km),
            "availability": AVAILABILITY_SCORE.get(availability, 0.0),
            "workload": _workload_score(volunteer.active_task_count),
            "region": 1.0 if same_region else 0.4,
            "skills": skill_score,
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
            "skill_match": skill_label,
            "location_freshness": freshness_label,
            "location_updated_at": profile.location_updated_at,
            "reasons": _reasons(distance_km, availability, volunteer.active_task_count, same_region, freshness_label, skill_label),
        })

    if ranked:
        closest = min(item["distance_km"] for item in ranked)
        for item in ranked:
            if item["distance_km"] == closest:
                item["reasons"].insert(0, "Ближайший подходящий волонтёр")

    # Deterministic order: score desc, then nearer first, then volunteer id as a
    # stable final tie-breaker so two identically-scored, equidistant volunteers
    # never swap places between calls (the candidate queryset has no inherent
    # ordering the DB is obliged to keep). Mirrors recommend_tasks().
    ranked.sort(key=lambda item: (-item["score"], item["distance_km"], item["volunteer"].id))
    return ranked[:limit]


# ---------------------------------------------------------------------------
# Volunteer -> task adapter
# ---------------------------------------------------------------------------
# recommend_tasks(volunteer) is the mirror image of recommend_volunteers(task):
# given a volunteer, which nearby *pending* requests are the best pick for them
# right now. It is NOT a second matching algorithm — it reuses the exact same
# primitives (_distance_score, _skill_score, haversine_km, AVERAGE_SPEED_KMH,
# CLOSE_KM / NEARBY_KM / DISTANCE_CAP_KM) and the same distance/skill semantics.
# It only drops the components that don't apply from the volunteer's own point
# of view (their availability / workload / location freshness are not signals
# about a task) and adds two task-side signals: the request's priority and how
# long it has been waiting. Read-only; never accepts or assigns anything —
# accepting stays the explicit accept_task_view flow.

TASK_REC_WEIGHTS = {"distance": 0.55, "skills": 0.25, "urgency": 0.15, "recency": 0.05}
# Score at most this many pending tasks per dashboard load. The cap is applied
# to the tasks *nearest* the volunteer (see recommend_tasks), so it bounds work
# without ever hiding a close task behind a backlog of far ones.
TASK_REC_CANDIDATE_CAP = 30

_PRIORITY_SCORE = {
    HelpRequest.PRIORITY_EMERGENCY: 1.0,
    HelpRequest.PRIORITY_HIGH: 0.7,
    HelpRequest.PRIORITY_NORMAL: 0.3,
}


def _recency_score(age):
    """A request that has been waiting longer scores slightly higher, so the
    dashboard surfaces things at risk of being forgotten. Bounded 0.4..1.0."""
    age_days = max(0.0, age.total_seconds() / 86400)
    return min(1.0, 0.4 + 0.15 * age_days)  # ~1.0 once it has waited ~4 days


def _task_reasons(distance_km, skill_label, task, age):
    reasons = []
    if distance_km <= CLOSE_KM:
        reasons.append("Совсем рядом с вами")
    elif distance_km <= NEARBY_KM:
        reasons.append("Недалеко от вас")
    if skill_label == "match":
        reasons.append("Подходит по вашим навыкам")
    if task.priority == HelpRequest.PRIORITY_EMERGENCY:
        reasons.append("Экстренный запрос")
    elif task.priority == HelpRequest.PRIORITY_HIGH:
        reasons.append("Высокий приоритет")
    if age >= timezone.timedelta(days=1):
        reasons.append("Ждёт волонтёра больше суток")
    return reasons


def recommend_tasks(volunteer, limit=3):
    """Rank nearby pending help requests for `volunteer`. Read-only.

    Returns a list of dicts (best first, at most `limit`):
      task, distance_km, estimated_minutes, score (0-100), skill_match, reasons.

    Returns [] when:
      - the volunteer has no saved location (distance ranking is meaningless);
      - the volunteer already holds an active task — the self-service accept
        flow allows only one at a time (accept_task_view), so none of these
        would be acceptable and the recommendation would only be noise; or
      - there is no region-eligible pending task with coordinates.
    """
    profile = getattr(volunteer, "profile", None)
    if not profile or not profile.has_location:
        return []
    if volunteer.volunteer_tasks.filter(status="active").exists():
        return []

    now = timezone.now()
    v_lat, v_lng = float(profile.latitude), float(profile.longitude)

    candidates = HelpRequest.objects.filter(
        status="pending", latitude__isnull=False, longitude__isnull=False
    ).select_related("client")
    if volunteer.region:
        candidates = candidates.filter(Q(region=volunteer.region) | Q(region=""))

    # The candidate cap must keep the tasks *nearest* the volunteer, not the
    # newest/most-urgent ones. Ordering by `-is_urgent, -created_at` (the old
    # behaviour) let a backlog of far-away urgent requests fill every slot, so a
    # close, acceptable task past position TASK_REC_CANDIDATE_CAP could never be
    # recommended. We approximate distance in SQL with absolute degree offsets,
    # longitude scaled by cos(latitude) so both axes carry their real length at
    # this latitude. This only decides which rows survive the cap — the exact
    # haversine ranking still runs in Python below. Trade-off: near the cap
    # boundary a mostly east-west task can be mis-ordered by a few positions,
    # but only among tasks already too far to lead the ranking.
    lng_weight = math.cos(math.radians(v_lat)) or 1.0
    proximity = Abs(Cast(F("latitude"), FloatField()) - v_lat) + (
        Abs(Cast(F("longitude"), FloatField()) - v_lng) * lng_weight
    )
    candidates = list(
        candidates.alias(_proximity=proximity).order_by("_proximity", "id")[:TASK_REC_CANDIDATE_CAP]
    )
    if not candidates:
        return []

    ranked = []
    for task in candidates:
        distance_km = round(haversine_km(v_lat, v_lng, float(task.latitude), float(task.longitude)), 2)
        skill_score, skill_label = _skill_score(profile.skills, task.help_type)
        age = now - task.created_at
        components = {
            "distance": _distance_score(distance_km),
            "skills": skill_score,
            "urgency": _PRIORITY_SCORE.get(task.priority, 0.3),
            "recency": _recency_score(age),
        }
        score = round(sum(components[key] * TASK_REC_WEIGHTS[key] for key in TASK_REC_WEIGHTS) * 100, 1)
        ranked.append({
            "task": task,
            "distance_km": distance_km,
            "estimated_minutes": round((distance_km / AVERAGE_SPEED_KMH) * 60, 1),
            "score": score,
            "skill_match": skill_label,
            "reasons": _task_reasons(distance_km, skill_label, task, age),
        })

    ranked.sort(key=lambda item: (-item["score"], item["distance_km"], item["task"].id))
    return ranked[:limit]
