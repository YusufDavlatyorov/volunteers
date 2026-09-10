"""Read-only tool handlers.

Each handler takes the trusted ``UserContext`` and validated kwargs and returns a
small JSON-serializable dict. Handlers:

* delegate aggregates to the existing services (``analytics``, ``overdue``,
  ``stale``, ``emergency``, ``matching``, ``maps``) — no query logic is
  duplicated;
* scope every ORM query to what the role may see, and re-check per-object
  visibility with ``access.py`` (mirrors the view gates);
* never serialize secrets, tokens, phone numbers of third parties, or email
  addresses;
* cap result sizes and always report ``total_count``.

Authorization by role is enforced one layer up, in ``tools.dispatch`` (via the
registry's ``roles``); the ownership checks here are the second layer.
"""

from __future__ import annotations

from django.db.models import Count, Q
from django.utils import timezone

from accounts.models import Profile, REGION_CHOICES, Users

from ...models import (
    Broadcast,
    Donation,
    EmergencyReport,
    Event,
    HelpRequest,
    PetReport,
    VolunteerApplication,
)
from .. import analytics, emergency, maps, matching, overdue, stale
from . import access

DEFAULT_LIMIT = 20
MAX_LIMIT = 50
_REGION_LABELS = dict(REGION_CHOICES)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _ok(**data) -> dict:
    return {"success": True, **data}


def _err(error: str, **extra) -> dict:
    return {"success": False, "error": error, **extra}


def _clamp_limit(limit) -> int:
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(limit, MAX_LIMIT))


def _clamp_offset(offset) -> int:
    try:
        return max(0, int(offset))
    except (TypeError, ValueError):
        return 0


def _iso(dt):
    return timezone.localtime(dt).isoformat(timespec="minutes") if dt else None


def _page(queryset, limit, offset, serializer) -> dict:
    limit = _clamp_limit(limit)
    offset = _clamp_offset(offset)
    total = queryset.count()
    rows = list(queryset[offset:offset + limit])
    return _ok(
        total_count=total,
        offset=offset,
        limit=limit,
        returned=len(rows),
        items=[serializer(row) for row in rows],
    )


def _region_label(code) -> str:
    return _REGION_LABELS.get(code) or (code or "")


# --------------------------------------------------------------------------- #
# serializers
# --------------------------------------------------------------------------- #

def _request_brief(r: HelpRequest) -> dict:
    data = {
        "id": r.id,
        "help_type": r.help_type,
        "help_type_display": r.get_help_type_display(),
        "status": r.status,
        "status_display": r.get_status_display(),
        "priority": r.priority,
        "is_urgent": r.is_urgent,
        "region": r.region or None,
        "region_display": _region_label(r.region) or None,
        "created_at": _iso(r.created_at),
        "client_username": r.client.username if r.client_id else None,
        "volunteer_username": r.volunteer.username if r.volunteer_id else None,
        "has_location": r.has_location,
    }
    if r.status == "active":
        data["work_stage"] = r.work_stage
        data["work_stage_display"] = r.get_work_stage_display()
        data["is_overdue"] = r.is_overdue
    if r.status == "pending":
        data["is_stale"] = r.is_stale_pending
    return data


def _request_detail(r: HelpRequest, *, ctx) -> dict:
    data = _request_brief(r)
    data["description"] = r.description
    party = (
        ctx.is_staff
        or (ctx.user_id is not None and r.client_id == ctx.user_id)
        or (ctx.user_id is not None and r.volunteer_id == ctx.user_id)
    )
    if party:
        data["address"] = r.address
        data["phone"] = r.phone
    data["accepted_at"] = _iso(r.accepted_at)
    data["completed_at"] = _iso(r.completed_at)
    return data


def _volunteer_brief(u: Users) -> dict:
    profile = getattr(u, "profile", None)
    return {
        "id": u.id,
        "username": u.username,
        "full_name": (profile.full_name if profile and profile.full_name else ""),
        "region": u.region or None,
        "region_display": _region_label(u.region) or None,
        "availability": getattr(profile, "availability_status", None),
        "availability_display": (
            profile.get_availability_status_display() if profile else None
        ),
        "rating": getattr(profile, "rating", 0),
        "active_task_count": getattr(u, "active_task_count", None),
        "has_location": bool(profile and profile.has_location),
    }


def _user_safe(u: Users) -> dict:
    profile = getattr(u, "profile", None)
    return {
        "id": u.id,
        "username": u.username,
        "full_name": (profile.full_name if profile and profile.full_name else ""),
        "role": u.role,
        "region_display": _region_label(u.region) or None,
        "is_active": u.is_active,
        "date_joined": _iso(u.date_joined),
    }


def _emergency_brief(e: EmergencyReport) -> dict:
    return {
        "id": e.id,
        "status": e.status,
        "status_display": e.get_status_display(),
        "help_request_id": e.help_request_id,
        "volunteer_username": e.volunteer.username if e.volunteer_id else None,
        "region_display": _region_label(e.region) or None,
        "has_location": e.has_location,
        "created_at": _iso(e.created_at),
        "reason": (e.reason or "")[:400],
    }


def _event_brief(ev: Event) -> dict:
    return {
        "id": ev.id,
        "title": ev.title,
        "region_display": _region_label(ev.region) if ev.region != "all" else "Все регионы",
        "date": _iso(ev.date),
        "curator_username": ev.curator.username if ev.curator_id else None,
    }


def _application_brief(a: VolunteerApplication) -> dict:
    return {
        "id": a.id,
        "username": a.user.username if a.user_id else None,
        "status": a.status,
        "status_display": a.get_status_display(),
        "region_display": _region_label(getattr(a.user, "region", "")) or None,
        "created_at": _iso(a.created_at),
    }


def _donation_brief(d: Donation) -> dict:
    return {
        "id": d.id,
        "item": d.item_name or (d.product.name if d.product_id else ""),
        "quantity": d.quantity,
        "unit": d.unit,
        "category_display": d.get_category_display(),
        "status": d.status,
        "status_display": d.get_status_display(),
        "region_display": _region_label(d.region) or None,
        "created_at": _iso(d.created_at),
    }


def _pet_brief(p: PetReport) -> dict:
    return {
        "id": p.id,
        "kind": p.report_type,
        "species_display": p.get_species_display(),
        "status": p.status,
        "status_display": p.get_status_display(),
        "region_display": _region_label(p.region) or None,
        "created_at": _iso(p.created_at),
    }


# --------------------------------------------------------------------------- #
# common (any authenticated role)
# --------------------------------------------------------------------------- #

_PLATFORM_HELP = {
    "how_it_works": (
        "A client submits a help request (type, description, address, phone, "
        "region). Volunteers in that region are notified and one accepts it. The "
        "client is notified on email/Telegram when it is accepted and when it is "
        "completed. Curators and admins coordinate nationally and can dispatch."
    ),
    "roles": (
        "Roles: client (asks for help), volunteer (gives help, reviewed on "
        "application), curator (national coordinator), admin (full oversight). "
        "A person has exactly one role."
    ),
    "create_request": (
        "To create a request a client provides: type of help (medical errand, "
        "groceries, transport, household, companionship, documents, other), a "
        "description, address, phone, and optionally a map location. Priority is "
        "normal / high / emergency."
    ),
    "volunteering": (
        "Register as a volunteer, an admin reviews the application. Once approved "
        "you see free requests in your region and can hold one active task at a "
        "time via self-accept. Completing tasks raises your rating."
    ),
    "regions": "Five regions: Dushanbe, Sogd, Khatlon, GBAO, RRP.",
    "contact": (
        "Coordination happens through curators and admins inside the platform. "
        "Notifications are sent by email and Telegram."
    ),
}


def get_platform_help(ctx, *, topic: str = "how_it_works") -> dict:
    key = (topic or "how_it_works").strip().lower()
    if key not in _PLATFORM_HELP:
        return _ok(topics=list(_PLATFORM_HELP), note="Unknown topic; pick one of 'topics'.")
    return _ok(topic=key, text=_PLATFORM_HELP[key])


def get_my_profile(ctx) -> dict:
    user = ctx.user
    if user is None:
        return _err("not_authenticated")
    profile = getattr(user, "profile", None)
    return _ok(
        username=user.username,
        full_name=(profile.full_name if profile and profile.full_name else ""),
        role=ctx.role,
        region_display=ctx.region_display or None,
        rating=getattr(profile, "rating", 0),
        availability=getattr(profile, "availability_status", None),
        skills=list(getattr(profile, "skills", []) or []),
        telegram_linked=bool(user.telegram_id),
        has_location=bool(profile and profile.has_location),
    )


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #

def get_my_requests(ctx, *, status: str = None, limit=DEFAULT_LIMIT, offset=0) -> dict:
    qs = HelpRequest.objects.filter(client_id=ctx.user_id).select_related(
        "client", "volunteer"
    ).order_by("-created_at")
    if status in {s for s, _ in HelpRequest._meta.get_field("status").choices}:
        qs = qs.filter(status=status)
    return _page(qs, limit, offset, _request_brief)


def get_request_status(ctx, *, request_id: int) -> dict:
    try:
        r = HelpRequest.objects.select_related("client", "volunteer").get(pk=request_id)
    except (HelpRequest.DoesNotExist, ValueError, TypeError):
        return _err("not_found")
    if r.client_id != ctx.user_id and not ctx.is_staff:
        return _err("permission_denied")
    return _ok(request=_request_detail(r, ctx=ctx))


def get_my_donations(ctx, *, limit=DEFAULT_LIMIT, offset=0) -> dict:
    qs = Donation.objects.filter(donor_id=ctx.user_id).select_related("product").order_by("-created_at")
    return _page(qs, limit, offset, _donation_brief)


def get_my_pet_reports(ctx, *, limit=DEFAULT_LIMIT, offset=0) -> dict:
    qs = PetReport.objects.filter(reporter_id=ctx.user_id).order_by("-created_at")
    return _page(qs, limit, offset, _pet_brief)


def get_my_events(ctx, *, limit=DEFAULT_LIMIT, offset=0) -> dict:
    qs = Event.objects.filter(date__gte=timezone.now()).select_related("curator")
    if ctx.region:
        qs = qs.filter(Q(region=ctx.region) | Q(region="all"))
    return _page(qs.order_by("date"), limit, offset, _event_brief)


# --------------------------------------------------------------------------- #
# volunteer
# --------------------------------------------------------------------------- #

def get_my_active_task(ctx) -> dict:
    r = (
        HelpRequest.objects.filter(volunteer_id=ctx.user_id, status="active")
        .select_related("client", "volunteer")
        .order_by("-accepted_at")
        .first()
    )
    if r is None:
        return _ok(active_task=None, note="No active task right now.")
    return _ok(active_task=_request_detail(r, ctx=ctx))


def get_my_tasks(ctx, *, status: str = None, limit=DEFAULT_LIMIT, offset=0) -> dict:
    qs = HelpRequest.objects.filter(volunteer_id=ctx.user_id).select_related(
        "client", "volunteer"
    ).order_by("-created_at")
    if status in {s for s, _ in HelpRequest._meta.get_field("status").choices}:
        qs = qs.filter(status=status)
    return _page(qs, limit, offset, _request_brief)


def get_recommended_tasks(ctx) -> dict:
    picks = matching.recommend_tasks(ctx.user, limit=5)
    return _ok(
        count=len(picks),
        items=[
            {
                "request": _request_brief(item["task"]),
                "distance_km": item["distance_km"],
                "estimated_minutes": item["estimated_minutes"],
                "score": item["score"],
                "skill_match": item["skill_match"],
                "reasons": item["reasons"],
            }
            for item in picks
        ],
    )


def get_task_details(ctx, *, request_id: int) -> dict:
    try:
        r = HelpRequest.objects.select_related("client", "volunteer").get(pk=request_id)
    except (HelpRequest.DoesNotExist, ValueError, TypeError):
        return _err("not_found")
    if not access.can_view_task(ctx.user, r):
        return _err("permission_denied")
    return _ok(request=_request_detail(r, ctx=ctx))


def get_route_for_task(ctx, *, request_id: int) -> dict:
    try:
        r = HelpRequest.objects.select_related("client", "volunteer").get(pk=request_id)
    except (HelpRequest.DoesNotExist, ValueError, TypeError):
        return _err("not_found")
    if not access.can_view_task(ctx.user, r):
        return _err("permission_denied")
    if not r.has_location:
        return _err("no_task_location")
    profile = getattr(ctx.user, "profile", None)
    if not (profile and profile.has_location):
        return _err("no_start_location", note="Set your location in your profile first.")
    result = maps.route(
        (float(profile.latitude), float(profile.longitude)),
        (float(r.latitude), float(r.longitude)),
    )
    return _ok(
        request_id=r.id,
        routing_available=result["success"],
        distance_km=result["distance_km"],
        duration_min=result["duration_min"],
        note=None if result["success"] else "Straight-line estimate (routing provider unavailable).",
    )


# --------------------------------------------------------------------------- #
# curator / admin — operational
# --------------------------------------------------------------------------- #

def get_dashboard_stats(ctx) -> dict:
    return _ok(stats=analytics.dashboard_stats())


def region_activity(ctx) -> dict:
    return _ok(regions=analytics.region_task_breakdown())


def _filtered_requests(status=None, region=None, priority=None, help_type=None):
    qs = HelpRequest.objects.select_related("client", "volunteer")
    valid_status = {s for s, _ in HelpRequest._meta.get_field("status").choices}
    valid_priority = {p for p, _ in HelpRequest._meta.get_field("priority").choices}
    valid_type = {t for t, _ in HelpRequest._meta.get_field("help_type").choices}
    if status in valid_status:
        qs = qs.filter(status=status)
    if region in _REGION_LABELS:
        qs = qs.filter(region=region)
    if priority in valid_priority:
        qs = qs.filter(priority=priority)
    if help_type in valid_type:
        qs = qs.filter(help_type=help_type)
    return qs.order_by("-created_at")


def list_help_requests(ctx, *, status=None, region=None, priority=None,
                       help_type=None, limit=DEFAULT_LIMIT, offset=0) -> dict:
    qs = _filtered_requests(status, region, priority, help_type)
    return _page(qs, limit, offset, _request_brief)


def get_help_request(ctx, *, request_id: int) -> dict:
    try:
        r = HelpRequest.objects.select_related("client", "volunteer").get(pk=request_id)
    except (HelpRequest.DoesNotExist, ValueError, TypeError):
        return _err("not_found")
    return _ok(request=_request_detail(r, ctx=ctx))


def list_overdue_tasks(ctx, *, limit=DEFAULT_LIMIT, offset=0) -> dict:
    return _page(overdue.currently_overdue_tasks(), limit, offset, _request_brief)


def list_stale_requests(ctx, *, limit=DEFAULT_LIMIT, offset=0) -> dict:
    return _page(stale.currently_stale_pending(), limit, offset, _request_brief)


def list_unassigned_requests(ctx, *, region=None, limit=DEFAULT_LIMIT, offset=0) -> dict:
    qs = HelpRequest.objects.filter(status="pending").select_related("client")
    if region in _REGION_LABELS:
        qs = qs.filter(region=region)
    return _page(qs.order_by("-is_urgent", "created_at"), limit, offset, _request_brief)


def find_requests_by_client(ctx, *, query: str, limit=DEFAULT_LIMIT, offset=0) -> dict:
    query = (query or "").strip()
    if len(query) < 2:
        return _err("invalid_args", note="Provide at least 2 characters.")
    qs = (
        HelpRequest.objects.select_related("client", "volunteer")
        .filter(client__username__icontains=query)
        .order_by("-created_at")
    )
    return _page(qs, limit, offset, _request_brief)


def list_emergencies(ctx, *, status=None, limit=DEFAULT_LIMIT, offset=0) -> dict:
    qs = EmergencyReport.objects.select_related("help_request", "volunteer")
    valid = {s for s, _ in EmergencyReport._meta.get_field("status").choices}
    if status in valid:
        qs = qs.filter(status=status)
    return _page(qs.order_by("-created_at"), limit, offset, _emergency_brief)


def get_emergency(ctx, *, report_id: int) -> dict:
    try:
        e = EmergencyReport.objects.select_related("help_request", "volunteer").get(pk=report_id)
    except (EmergencyReport.DoesNotExist, ValueError, TypeError):
        return _err("not_found")
    if not access.can_view_emergency(ctx.user, e):
        return _err("permission_denied")
    return _ok(report=_emergency_brief(e))


def _volunteer_queryset(region=None, availability=None):
    qs = (
        Users.objects.filter(is_volunteer=True, is_active=True)
        .select_related("profile")
        .annotate(
            active_task_count=Count(
                "volunteer_tasks", filter=Q(volunteer_tasks__status="active"), distinct=True
            )
        )
    )
    if region in _REGION_LABELS:
        qs = qs.filter(region=region)
    valid_av = {a for a, _ in Profile.AVAILABILITY_CHOICES}
    if availability in valid_av:
        qs = qs.filter(profile__availability_status=availability)
    return qs.order_by("-profile__rating", "username")


def list_volunteers(ctx, *, region=None, availability=None, limit=DEFAULT_LIMIT, offset=0) -> dict:
    return _page(_volunteer_queryset(region, availability), limit, offset, _volunteer_brief)


def list_available_volunteers(ctx, *, region=None, limit=DEFAULT_LIMIT, offset=0) -> dict:
    return _page(
        _volunteer_queryset(region, Profile.AVAILABILITY_AVAILABLE),
        limit, offset, _volunteer_brief,
    )


def get_volunteer_recommendations(ctx, *, request_id: int) -> dict:
    try:
        r = HelpRequest.objects.get(pk=request_id)
    except (HelpRequest.DoesNotExist, ValueError, TypeError):
        return _err("not_found")
    ranked = matching.recommend_volunteers(r, limit=5)
    return _ok(
        request_id=r.id,
        task_has_location=r.has_location,
        count=len(ranked),
        items=[
            {
                "volunteer_id": item["volunteer"].id,
                "username": item["volunteer"].username,
                "distance_km": item["distance_km"],
                "estimated_minutes": item["estimated_minutes"],
                "score": item["score"],
                "availability": item["availability"],
                "active_task_count": item["active_task_count"],
                "same_region": item["same_region"],
                "skill_match": item["skill_match"],
                "reasons": item["reasons"],
            }
            for item in ranked
        ],
    )


def list_pending_applications(ctx, *, limit=DEFAULT_LIMIT, offset=0) -> dict:
    qs = (
        VolunteerApplication.objects.filter(status=VolunteerApplication.STATUS_PENDING)
        .select_related("user")
        .order_by("-created_at")
    )
    return _page(qs, limit, offset, _application_brief)


def list_events(ctx, *, limit=DEFAULT_LIMIT, offset=0) -> dict:
    return _page(
        Event.objects.select_related("curator").order_by("-date"),
        limit, offset, _event_brief,
    )


def list_broadcasts(ctx, *, limit=DEFAULT_LIMIT, offset=0) -> dict:
    def _b(b: Broadcast):
        return {
            "id": b.id,
            "subject": b.subject,
            "region_display": _region_label(b.region) if b.region != "all" else "Все регионы",
            "sent_count": b.sent_count,
            "created_at": _iso(b.created_at),
            "sender_username": b.sender.username if b.sender_id else None,
        }

    return _page(Broadcast.objects.select_related("sender").order_by("-created_at"), limit, offset, _b)


def list_donations(ctx, *, status=None, limit=DEFAULT_LIMIT, offset=0) -> dict:
    qs = Donation.objects.select_related("product", "donor").order_by("-created_at")
    valid = {s for s, _ in Donation._meta.get_field("status").choices}
    if status in valid:
        qs = qs.filter(status=status)
    return _page(qs, limit, offset, _donation_brief)


def list_pet_reports(ctx, *, status=None, limit=DEFAULT_LIMIT, offset=0) -> dict:
    qs = PetReport.objects.order_by("-created_at")
    valid = {s for s, _ in PetReport._meta.get_field("status").choices}
    if status in valid:
        qs = qs.filter(status=status)
    return _page(qs, limit, offset, _pet_brief)


# --------------------------------------------------------------------------- #
# admin only
# --------------------------------------------------------------------------- #

def list_users(ctx, *, role=None, region=None, active=None, limit=DEFAULT_LIMIT, offset=0) -> dict:
    qs = Users.objects.select_related("profile").order_by("-date_joined")
    if role == "admin":
        qs = qs.filter(is_superuser=True)
    elif role == "curator":
        qs = qs.filter(is_curator=True, is_superuser=False)
    elif role == "volunteer":
        qs = qs.filter(is_volunteer=True)
    elif role == "client":
        qs = qs.filter(is_client=True)
    if region in _REGION_LABELS:
        qs = qs.filter(region=region)
    if isinstance(active, bool):
        qs = qs.filter(is_active=active)
    return _page(qs, limit, offset, _user_safe)


def get_user(ctx, *, user_id: int) -> dict:
    try:
        u = Users.objects.select_related("profile").get(pk=user_id)
    except (Users.DoesNotExist, ValueError, TypeError):
        return _err("not_found")
    data = _user_safe(u)
    data["completed_tasks"] = u.volunteer_tasks.filter(status="completed").count() if u.is_volunteer else None
    data["open_requests"] = u.client_requests.exclude(status__in=["completed", "cancelled"]).count() if u.is_client else None
    return _ok(user=data)


def users_by_role(ctx) -> dict:
    return _ok(counts=analytics.users_by_role())


def platform_totals(ctx) -> dict:
    return _ok(totals=analytics.platform_totals())
