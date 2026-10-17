import json
import logging
import mimetypes
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Max, Q
from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from accounts.models import ABOUT_COMMUNITY_CACHE_KEY, Profile, REGION_CHOICES, Users
from .forms import (
    BroadcastForm,
    DonationForm,
    DonationGoodsForm,
    EventForm,
    HelpRequestFilterForm,
    HelpRequestForm,
    PetReportForm,
    PhotoReportForm,
    TaskManagementFilterForm,
)
from .models import (
    Broadcast,
    Donation,
    DonationPhoto,
    MAX_DONATION_PHOTOS,
    DONATION_STATUS_CHOICES,
    EmergencyReport,
    Event,
    HELP_TYPE_CHOICES,
    HelpRequest,
    PET_REPORT_TYPE_CHOICES,
    PET_SPECIES_CHOICES,
    PetReport,
    PhotoReport,
    PRIORITY_CHOICES,
    STATUS_CHOICES,
    VolunteerApplication,
    WORK_STAGE_CHOICES,
)
from .models.emergency import STATUS_CHOICES as EMERGENCY_STATUS_CHOICES
from .notifications import staff_recipients, volunteer_queryset_for_region
from .realtime import publish
from .services import achievements, activity, cancellation, chat, community, feedback, network, presence, rating, task_actions, tracking
from .services.notify import notify
from .services import access, analytics, dashboard, dispatch, donations, emergency, images, maps, overdue, pets, photo_reports, profile_location, stale
from .services.ai import build_user_context, confirm_action, run_conversation
from .services.ai import actions as ai_actions
from .services.ai.prompts import detect_lang
from .services.geo import is_valid_coordinate
from .services.matching import location_freshness_label, recommend_volunteers

logger = logging.getLogger(__name__)

# Page sizes for the list views that grow without bound at production scale
# (10k+ users, 100k+ historical requests). Named so the regression tests can
# shrink them instead of creating hundreds of rows.
ARCHIVE_PAGE_SIZE = 30
PEOPLE_PAGE_SIZE = 30
APPLICATIONS_PAGE_SIZE = 25
PHOTO_REPORTS_PAGE_SIZE = 24


def role_required(*roles):
    def decorator(view_func):
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                # Like @login_required: come back here after signing in (a
                # visitor's "Request help" must land on the form, not the
                # dashboard). Only for GET — a POST URL can't be re-opened.
                if request.method == "GET":
                    return redirect_to_login(request.get_full_path())
                return redirect("login")
            if request.user.is_superuser and "admin" in roles:
                return view_func(request, *args, **kwargs)
            if request.user.role not in roles:
                messages.error(request, "У вас нет доступа к этой странице")
                return redirect("dashboard")
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator


def home_view(request):
    # The landing page: logged-out visitors (and search engines) get the
    # About content at "/"; signed-in users go straight to their dashboard,
    # as "/" (then the login form) always did.
    if request.user.is_authenticated:
        return redirect("dashboard")
    return about_view(request)


ABOUT_STATS_CACHE_KEY = "about_stats:v1"
ABOUT_STATS_TTL = 300  # seconds — public landing-page numbers, fine to be a few minutes old


def _about_stats():
    counts = Users.objects.filter(is_active=True).aggregate(
        volunteers=Count("id", filter=Q(is_volunteer=True)),
        clients=Count("id", filter=Q(is_client=True)),
    )
    return {
        **counts,
        "completed": HelpRequest.objects.filter(status="completed").count(),
        "regions": Users.objects.exclude(region="").values("region").distinct().count(),
    }


ABOUT_LEADERS = 4


def _about_leaders():
    """The rating's own top ``ABOUT_LEADERS`` (services/rating.py — the same
    ranking and places as /rating/, no second calculation): volunteers with at
    least one completed request."""
    return rating.top(ABOUT_LEADERS)


def _about_community():
    # Leaders + featured testimonials: 1 query (2 when the "all" ranking is
    # cold too). Cleared with the rating (clear_rating_cache) and by a
    # review's landing flag; otherwise as fresh as the stats.
    return {"leaders": _about_leaders(), "testimonials": feedback.landing_testimonials()}


def _request_help_url(request, help_type=""):
    """The request form (pre-selected help type); anonymous visitors go
    through the login page and come back to it (``?next=``)."""
    url = reverse("create_request")
    if help_type:
        url += "?" + urlencode({"help_type": help_type})
    if request.user.is_authenticated:
        return url
    return reverse("login") + "?" + urlencode({"next": url})


def about_view(request):
    # The most-visited page (also "/"): its aggregates come from the shared
    # cache (DB cache in production) instead of four COUNTs per hit.
    stats = cache.get_or_set(ABOUT_STATS_CACHE_KEY, _about_stats, ABOUT_STATS_TTL)
    community = cache.get_or_set(ABOUT_COMMUNITY_CACHE_KEY, _about_community, ABOUT_STATS_TTL)
    reports = photo_reports.public_reports().select_related("author")[:6]  # approved only
    # Every real help type (HELP_TYPE_CHOICES), so a new one shows up here too.
    help_types = [
        {"key": key, "label": label, "url": _request_help_url(request, key)}
        for key, label in HELP_TYPE_CHOICES
    ]
    return render(
        request,
        "myapp/about.html",
        {
            "stats": stats,
            "reports": reports,
            "leaders": community["leaders"],
            "testimonials": community["testimonials"],
            "help_types": help_types,
            "request_help_url": _request_help_url(request),
            # Per-region volunteer counts only for approved volunteers / staff
            # (services/network.py decides; nobody else's context has them).
            "network": network.for_user(request.user),
            "network_login_url": reverse("login") + "?" + urlencode({"next": reverse("about") + "#network"}),
        },
    )


@login_required
def dashboard_view(request):
    """"What do I do now": the one role-aware dashboard at /dashboard/. All
    per-role aggregation lives in services.dashboard (scoped to request.user);
    this view stays thin. "Who I am" (card, calendar, badges, saved location)
    is the profile page, accounts.views.profile_view."""
    user = request.user
    profile, _ = Profile.objects.get_or_create(user=user)
    # Prime the reverse one-to-one cache so user.profile (dashboard services,
    # templates) reuses this row instead of querying it again.
    user.profile = profile
    hour = timezone.localtime().hour
    context = {
        "profile": profile,
        "greeting": "morning" if hour < 12 else "afternoon" if hour < 18 else "evening",
        "volunteer_application": VolunteerApplication.objects.filter(user=user).first(),
        # Only a reminder banner here; the location card itself is on the profile.
        "location_stale": user.is_volunteer and profile_location.is_stale(profile),
        **dashboard.for_user(user),
    }
    return render(request, "myapp/dashboard.html", context)


@role_required("admin", "curator")
def admin_panel_view(request):
    """The staff control panel (/myapp/panel/): KPI bands, compact cards and
    one request table with free/busy tabs (?tab=), the HelpRequestFilterForm
    filters and 20-row pages — rows, counters and candidates come from
    services.dashboard.panel_requests. Every link keeps tab + filters."""
    requests_qs = HelpRequest.objects.all()
    filter_form = HelpRequestFilterForm(request.GET)
    if filter_form.is_valid():
        if filter_form.cleaned_data.get("help_type"):
            requests_qs = requests_qs.filter(help_type=filter_form.cleaned_data["help_type"])
        if filter_form.cleaned_data.get("region"):
            requests_qs = requests_qs.filter(region=filter_form.cleaned_data["region"])
        if filter_form.cleaned_data.get("priority"):
            requests_qs = requests_qs.filter(priority=filter_form.cleaned_data["priority"])
        if filter_form.cleaned_data.get("is_urgent"):
            requests_qs = requests_qs.filter(is_urgent=True)
    table = dashboard.panel_requests(requests_qs, request.GET.get("tab"), request.GET.get("page"))
    # The active filters alone (no tab / page), for tab links, page links and
    # the "open in task management" link.
    filters = request.GET.copy()
    for key in list(filters):
        if key not in filter_form.fields or not filters.get(key):
            filters.pop(key)
    filter_query = filters.urlencode()
    limit = dashboard.CARD_LIST_LIMIT
    context = {
        "filter_form": filter_form,
        "filter_query": filter_query,
        "filters_active": bool(filter_query),
        "page_query": f"tab={table['tab']}" + (f"&{filter_query}" if filter_query else ""),
        **table,
        "upcoming_events": Event.objects.filter(date__gte=timezone.now()).order_by("date")[:limit],
        "recent_broadcasts": Broadcast.objects.all()[:limit],
        # CRM dashboard: real aggregate stats + a unified recent-activity feed,
        # both computed in myapp.services.analytics so this view stays thin.
        "stats": analytics.dashboard_stats(),
        "recent_activity": analytics.recent_activity(limit=8),
        "open_emergencies": emergency.open_reports()[:limit],
        "photo_reports_pending": photo_reports.pending_count(),  # the CRM menu badge
    }
    if request.user.is_superuser:
        context["recent_applications"] = VolunteerApplication.objects.select_related("user").order_by("-created_at")[:limit]
    return render(request, "myapp/admin_panel.html", context)


PEOPLE_ROLE_FIELDS = {
    "volunteer": "is_volunteer",
    "client": "is_client",
    "curator": "is_curator",
}


@role_required("admin", "curator")
def people_list_view(request, role):
    if role not in PEOPLE_ROLE_FIELDS:
        raise Http404
    if role == "curator" and not request.user.is_superuser:
        messages.error(request, "У вас нет доступа к этой странице")
        return redirect("admin_panel")

    region_filter = request.GET.get("region", "")
    search_query = request.GET.get("q", "").strip()
    availability_filter = request.GET.get("availability", "") if role == "volunteer" else ""

    people = Users.objects.filter(**{PEOPLE_ROLE_FIELDS[role]: True}, is_active=True).select_related("profile").order_by("-date_joined")
    if region_filter:
        people = people.filter(region=region_filter)
    if search_query:
        people = people.filter(Q(username__icontains=search_query) | Q(email__icontains=search_query))

    if role == "volunteer":
        if availability_filter == "away":
            # Do-not-disturb in effect right now (an expired end date counts as off).
            people = people.exclude(presence.not_away_q())
        elif availability_filter:
            people = people.filter(profile__availability_status=availability_filter)
        # Per-row task counts via annotation, not a query per row (avoids N+1).
        people = people.annotate(
            completed_task_count=Count("volunteer_tasks", filter=Q(volunteer_tasks__status="completed"), distinct=True),
            active_task_count=Count("volunteer_tasks", filter=Q(volunteer_tasks__status="active"), distinct=True),
        )
    elif role == "client":
        people = people.annotate(
            request_count=Count("client_requests", distinct=True),
            completed_request_count=Count("client_requests", filter=Q(client_requests__status="completed"), distinct=True),
        )

    # The directory grows with the platform (10k+ volunteers at scale) — paginate.
    page_obj = Paginator(people, PEOPLE_PAGE_SIZE).get_page(request.GET.get("page"))
    querystring = request.GET.copy()
    querystring.pop("page", None)

    context = {
        # `people` stays the iteration variable in the template; it is the current
        # page (iterable), `page_obj` drives the pager.
        "people": page_obj,
        "page_obj": page_obj,
        "querystring": querystring.urlencode(),
        "role": role,
        "region_filter": region_filter,
        "search_query": search_query,
        "availability_filter": availability_filter,
        "availability_choices": Profile.AVAILABILITY_CHOICES,
        "region_choices": REGION_CHOICES,
        # One aggregate query for the three tab counts.
        **Users.objects.filter(is_active=True).aggregate(
            volunteer_count=Count("id", filter=Q(is_volunteer=True)),
            client_count=Count("id", filter=Q(is_client=True)),
            curator_count=Count("id", filter=Q(is_curator=True)),
        ),
    }
    return render(request, "myapp/people_list.html", context)


@role_required("admin", "curator")
def crm_tasks_view(request):
    """CRM task management: the full filterable, searchable, paginated task
    list — a superset of the free/busy grids on the dashboard itself."""
    filter_form = TaskManagementFilterForm(request.GET)
    tasks = HelpRequest.objects.select_related("client", "volunteer").order_by("-created_at")

    if filter_form.is_valid():
        cleaned = filter_form.cleaned_data
        if cleaned.get("status"):
            tasks = tasks.filter(status=cleaned["status"])
        if cleaned.get("help_type"):
            tasks = tasks.filter(help_type=cleaned["help_type"])
        if cleaned.get("region"):
            tasks = tasks.filter(region=cleaned["region"])
        if cleaned.get("priority"):
            tasks = tasks.filter(priority=cleaned["priority"])
        if cleaned.get("is_urgent"):
            tasks = tasks.filter(is_urgent=True)
        if cleaned.get("volunteer"):
            tasks = tasks.filter(volunteer__username__icontains=cleaned["volunteer"])
        if cleaned.get("date_from"):
            tasks = tasks.filter(created_at__date__gte=cleaned["date_from"])
        if cleaned.get("date_to"):
            tasks = tasks.filter(created_at__date__lte=cleaned["date_to"])
        if cleaned.get("q"):
            query = cleaned["q"]
            tasks = tasks.filter(
                Q(description__icontains=query)
                | Q(address__icontains=query)
                | Q(phone__icontains=query)
                | Q(client__username__icontains=query)
            )

    # The "overdue"/"stale" predicates live in one place each (services.overdue /
    # services.stale) so this CRM filter can't drift from the sweep, the
    # dashboard queue or the analytics count.
    overdue_only = request.GET.get("overdue") == "1"
    if overdue_only:
        tasks = tasks.filter(overdue.currently_overdue_q())

    stale_only = request.GET.get("stale") == "1"
    if stale_only:
        tasks = tasks.filter(stale.currently_stale_pending_q())

    paginator = Paginator(tasks, 20)
    page_obj = paginator.get_page(request.GET.get("page"))

    querystring = request.GET.copy()
    querystring.pop("page", None)

    context = {
        "filter_form": filter_form,
        "page_obj": page_obj,
        "overdue_only": overdue_only,
        "stale_only": stale_only,
        "querystring": querystring.urlencode(),
        "stats": analytics.dashboard_stats(),
    }
    return render(request, "myapp/crm_tasks.html", context)


@role_required("admin", "curator")
def crm_volunteer_detail_view(request, pk):
    """Volunteer profile as seen from the CRM: availability, workload,
    rating, location freshness, last activity, and their task history."""
    volunteer = get_object_or_404(
        Users.objects.select_related("profile").filter(is_volunteer=True), pk=pk
    )
    profile = volunteer.profile
    tasks = HelpRequest.objects.filter(volunteer=volunteer).select_related("client").order_by("-created_at")
    stats = tasks.aggregate(
        completed=Count("id", filter=Q(status="completed")),
        active=Count("id", filter=Q(status="active")),
        last_accepted=Max("accepted_at"),
        last_completed=Max("completed_at"),
    )
    activity_candidates = [profile.location_updated_at, stats["last_accepted"], stats["last_completed"]]
    last_activity = max((moment for moment in activity_candidates if moment), default=None)

    context = {
        "volunteer": volunteer,
        "profile": profile,
        "recent_tasks": tasks[:10],
        "completed_count": stats["completed"],
        "active_count": stats["active"],
        "location_freshness": location_freshness_label(profile.location_updated_at) if profile.has_location else None,
        "last_activity": last_activity,
        "achievement_items": achievements.progress_for(volunteer),
        # The volunteer's activity calendar (myapp/services/activity.py), staff-only here.
        "activity": activity.calendar(volunteer),
        "activity_empty_key": "act.empty_other",
    }
    context["mentor_awarded"] = any(i["code"] == "mentor" and i["earned"] for i in context["achievement_items"])
    return render(request, "myapp/crm_volunteer_detail.html", context)


EVENT_LIST_PAST_LIMIT = 50
BROADCAST_LIST_LIMIT = 50


@role_required("admin", "curator")
def event_list_view(request):
    now = timezone.now()
    events = Event.objects.select_related("curator")
    context = {
        "upcoming_events": events.filter(date__gte=now).order_by("date"),
        # Past events accumulate forever — the recent ones are all that's useful.
        "past_events": events.filter(date__lt=now).order_by("-date")[:EVENT_LIST_PAST_LIMIT],
    }
    return render(request, "myapp/event_list.html", context)


@role_required("admin", "curator")
def broadcast_list_view(request):
    broadcasts = Broadcast.objects.select_related("sender")[:BROADCAST_LIST_LIMIT]
    return render(request, "myapp/broadcast_list.html", {"broadcasts": broadcasts})


@role_required("volunteer", "admin", "curator")
def task_list_view(request):
    user = request.user
    tasks = HelpRequest.objects.filter(status="pending").select_related("client")
    if user.is_volunteer and user.region:
        tasks = tasks.filter(Q(region=user.region) | Q(region=""))

    filter_form = HelpRequestFilterForm(request.GET)
    if user.is_volunteer and user.region:
        # The feed is already this volunteer's region (above) — a region
        # filter could only empty it, so it isn't offered.
        filter_form.fields.pop("region")
    if filter_form.is_valid():
        if filter_form.cleaned_data.get("help_type"):
            tasks = tasks.filter(help_type=filter_form.cleaned_data["help_type"])
        if filter_form.cleaned_data.get("region"):
            tasks = tasks.filter(region=filter_form.cleaned_data["region"])
        if filter_form.cleaned_data.get("priority"):
            tasks = tasks.filter(priority=filter_form.cleaned_data["priority"])
        if filter_form.cleaned_data.get("is_urgent"):
            tasks = tasks.filter(is_urgent=True)

    my_active = list(HelpRequest.objects.filter(volunteer=user, status="active")) if user.is_volunteer else None
    # One query for every active task's unread chat count (feature 8).
    chat_unread = chat.unread_counts(user, [t.pk for t in my_active]) if my_active else {}
    return render(request, "myapp/task_list.html", {
        "tasks": tasks, "filter_form": filter_form, "my_active": my_active, "chat_unread": chat_unread,
    })


def _route_permission(user, task):
    """Who may see the volunteer<->client route for a task — delegates to
    ``access.can_view_route`` (staff always; the assigned volunteer and the
    task's client only while it is active). ``task_route_view`` still answers
    a task *party* outside that window with a graceful 200 reason, as before."""
    return access.can_view_route(user, task)


def _can_view_task(user, task):
    """Who may open a task's detail page — delegates to the shared rule in
    ``myapp/services/access.py::can_view_task`` (also used by the WebSocket
    ``task_<id>`` subscribe and the AI tools), so HTTP and WS can't drift."""
    return access.can_view_task(user, task)


@login_required
def task_detail_view(request, pk):
    task = get_object_or_404(HelpRequest.objects.select_related("client", "volunteer"), pk=pk)
    user = request.user
    if not _can_view_task(user, task):
        messages.error(request, "У вас нет доступа к этой странице")
        return redirect("dashboard")
    show_route = task.status == "active" and bool(task.volunteer_id) and _route_permission(user, task)
    is_crm_staff = user.is_superuser or user.is_curator
    show_recommendations = task.status == "pending" and is_crm_staff
    # The route card already shows a map with the destination pin once a
    # volunteer is assigned; for every other state, a simple location-only
    # pin is still useful context and needs no extra endpoint (lat/lng are
    # already on the task).
    show_location_map = task.has_location and not show_route

    work_stage_steps = next_stage = None
    if task.status == "active":
        labels = dict(WORK_STAGE_CHOICES)
        order = HelpRequest.WORK_STAGE_ORDER
        current_idx = order.index(task.work_stage)
        work_stage_steps = [
            {
                "key": key,
                "label": labels[key],
                "state": "done" if i < current_idx else "current" if i == current_idx else "upcoming",
            }
            for i, key in enumerate(order)
        ]
        if current_idx + 1 < len(order):
            next_stage = {"key": order[current_idx + 1], "label": labels[order[current_idx + 1]]}

    active_emergency = (
        task.emergency_reports.filter(status__in=EmergencyReport.OPEN_STATUSES)
        .order_by("-created_at")
        .first()
    )

    # Chat entry point (feature 8): parties + staff, only while readable.
    chat_readable = access.can_read_chat(user, task)
    chat_unread = chat.unread_counts(user, [task.pk]).get(task.pk, 0) if chat_readable else 0

    return render(
        request,
        "myapp/task_detail.html",
        {
            "task": task,
            "chat_readable": chat_readable,
            "chat_unread": chat_unread,
            "show_client_phone": access.can_see_client_phone(user, task),
            "show_route": show_route,
            "show_recommendations": show_recommendations,
            "show_location_map": show_location_map,
            "show_history": is_crm_staff,
            "work_stage_steps": work_stage_steps,
            "next_stage": next_stage,
            "can_advance_stage": task.status == "active" and (user == task.volunteer or is_crm_staff),
            "can_report_emergency": user == task.volunteer and task.status == "active",
            "active_emergency": active_emergency,
            "can_cancel": cancellation.can_cancel(user, task),
            "cancel_reason_required": is_crm_staff,
        },
    )


CANCEL_MESSAGES = {
    cancellation.FORBIDDEN: "Вы не можете отменить этот запрос.",
    cancellation.NOT_OPEN: "Запрос уже закрыт или принят волонтёром.",
    cancellation.REASON_REQUIRED: "Укажите причину отмены.",
}


@login_required
@require_POST
def task_cancel_view(request, pk):
    """Cancel a help request: the client while pending (reason optional), staff
    from any open state (reason required). Rules + side effects live in
    services/cancellation.py — the one cancel path."""
    task = get_object_or_404(HelpRequest.objects.select_related("client", "volunteer"), pk=pk)
    if not access.can_view_task(request.user, task):
        raise Http404
    outcome = cancellation.cancel_request(task, request.user, request.POST.get("reason", ""))
    if outcome.ok:
        messages.success(request, "Запрос отменён. Участникам отправлено уведомление.")
    else:
        messages.error(request, CANCEL_MESSAGES[outcome.code])
    return redirect("task_detail", pk=task.pk)


# After a client creates a request. The first only when the region ping
# actually queued a delivery; otherwise say what really happens next (the
# request waits in the open list / curator dashboard).
REQUEST_CREATED_NOTIFIED = (
    "Запрос отправлен. Мы сообщили о нём волонтёрам вашего региона. "
    "Когда волонтёр примет запрос, вы получите уведомление."
)
REQUEST_CREATED_NOBODY = (
    "Запрос отправлен. Сейчас никого из волонтёров региона не удалось оповестить — "
    "запрос виден волонтёрам и координаторам, мы сообщим, когда его примут."
)
_ACCEPT_BUSY_MESSAGE = "Сначала завершите текущий запрос. Один волонтер работает с одним запросом."


@role_required("volunteer")
@require_POST
def accept_task_view(request, pk):
    """Self-accept — the rules (lock, one active task, conditional UPDATE) live
    in services/task_actions.accept, shared with the Telegram bot."""
    task = get_object_or_404(HelpRequest.objects.select_related("client"), pk=pk)
    outcome = task_actions.accept(request.user, task)
    if outcome.code == task_actions.BUSY:
        messages.warning(request, _ACCEPT_BUSY_MESSAGE)
    elif outcome.code == task_actions.TAKEN:
        messages.warning(request, "Этот запрос уже принят другим волонтером.")
    else:
        messages.success(request, "Вы приняли запрос. Клиенту отправлено уведомление.")
    return redirect("task_list")


@role_required("volunteer")
@require_POST
def complete_task_view(request, pk):
    task = get_object_or_404(HelpRequest, pk=pk, volunteer=request.user, status="active")
    # services/task_actions.complete: conditional UPDATE — a double-click can't
    # complete twice or award the rating points twice.
    outcome = task_actions.complete(request.user, task)
    if not outcome.ok:
        messages.info(request, "Этот запрос уже завершён.")
        return redirect("task_list")
    if outcome.reward_pending:
        # Points + badges wait for an approved photo report (services/photo_reports).
        messages.success(request, "Запрос завершён. Загрузите фотоотчёт — баллы начислятся после его проверки.")
        return redirect(reverse("photo_reports") + f"?task={task.pk}#report-form")
    messages.success(request, "Запрос завершен, рейтинг обновлен.")
    return redirect("task_list")


@login_required
@require_POST
def task_advance_stage_view(request, pk):
    """Move an active task's work_stage forward. The assigned volunteer drives
    their own progress; curator/admin can correct it. The transition + client
    notification live in services.ai.actions.advance_work_stage_for, shared with
    the AI ``advance_work_stage`` action."""
    task = get_object_or_404(HelpRequest.objects.select_related("client", "volunteer"), pk=pk)
    user = request.user
    if not (user == task.volunteer or user.is_superuser or user.is_curator):
        messages.error(request, "У вас нет доступа к этой странице")
        return redirect("dashboard")

    try:
        ai_actions.advance_work_stage_for(user, task, request.POST.get("stage", ""))
    except ValueError:
        messages.warning(request, "Не удалось изменить этап задачи.")
        return redirect("task_detail", pk=pk)

    messages.success(request, f"Этап обновлён: {task.get_work_stage_display()}.")
    return redirect("task_detail", pk=pk)


@role_required("client")
def create_request_view(request):
    if request.method == "POST":
        form = HelpRequestForm(request.POST)
        if form.is_valid():
            # Region = client's, best-effort geocode, volunteer ping (DND
            # excluded) — services/task_actions.create_request, shared with the bot.
            created = task_actions.create_request(request.user, form)
            if created.volunteers_notified:
                messages.success(request, REQUEST_CREATED_NOTIFIED)
            else:
                messages.success(request, REQUEST_CREATED_NOBODY)
            return redirect("dashboard")
    else:
        # ?help_type=<key> pre-selects the type (the About page's category cards).
        help_type = request.GET.get("help_type")
        form = HelpRequestForm(initial={"help_type": help_type} if help_type in dict(HELP_TYPE_CHOICES) else None)
    return render(request, "myapp/task_form.html", {
        "form": form, "title": "Создать запрос", "title_i18n": "form.title_create_request",
        "submit_label": "Отправить запрос", "submit_i18n": "form.send_request",
        # The request takes the client's own region (task_actions.create_request).
        "request_region": request.user.get_region_display() if request.user.region else "",
        "request_region_key": request.user.region,
    })


@role_required("admin", "curator")
def completed_tasks_view(request):
    tasks = HelpRequest.objects.filter(status="completed").select_related("client", "volunteer")
    # The archive grows without bound (100k+ historical rows at scale) — paginate
    # rather than render the whole table.
    page_obj = Paginator(tasks, ARCHIVE_PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "myapp/completed_tasks.html", {"page_obj": page_obj})


def rating_view(request):
    """Public volunteer rating with places, all time (owner 2026-10-14; the
    ranking, its cache and the rules live in services/rating.py): the podium
    (places 1–3) on the first page, then the table from the next place, paged.
    No search / filter / sort any more — old ``?q=`` / ``?region=`` /
    ``?sort=`` / ``?period=`` links are simply ignored. Viewer-specific bits
    ("You", the profile button) are decided here, never cached."""
    data = rating.cached()
    rows = data["rows"]
    podium = rating.podium(rows)
    # The table continues after the podium, so its paging starts at place 4.
    page_obj = Paginator(rows[len(podium):], rating.PAGE_SIZE).get_page(request.GET.get("page"))
    leader = rows[0]["completed"] if rows else 0
    page_rows = [
        dict(row, progress=round(100 * row["completed"] / leader) if leader else 0) for row in page_obj
    ]
    shown_podium = podium if page_obj.number == 1 else []
    # Up to 3 earned badges per person (one query) — the badge only, no counts.
    badges = achievements.top_badges([row["user_id"] for row in [*shown_podium, *page_rows]])
    age = timezone.now() - data["computed_at"]
    return render(request, "myapp/rating.html", {
        "updated_minutes": max(0, int(age.total_seconds() // 60)),
        "podium": shown_podium,
        "has_rows": bool(rows),
        "page_obj": page_obj,
        "page_rows": page_rows,
        # "Showing 4–23 of 81": overall places, not positions within the table.
        "shown_from": page_rows[0]["rank"] if page_rows else 0,
        "shown_to": page_rows[-1]["rank"] if page_rows else 0,
        "shown_total": len(rows),
        "page_range": page_obj.paginator.get_elided_page_range(page_obj.number, on_each_side=1, on_ends=1),
        "badges": badges,
        "viewer_id": request.user.pk if request.user.is_authenticated else None,
        "staff_viewer": access.is_staff_user(request.user),
    })


# Role-aware starter prompts shown on the empty chat screen. Kept server-side so
# a client/volunteer never sees admin/curator suggestions.
AI_SUGGESTION_KEYS = {
    "admin": ["ai.suggest_admin_1", "ai.suggest_admin_2", "ai.suggest_admin_3", "ai.suggest_admin_4"],
    "curator": ["ai.suggest_curator_1", "ai.suggest_curator_2", "ai.suggest_curator_3", "ai.suggest_curator_4"],
    "volunteer": ["ai.suggest_vol_1", "ai.suggest_vol_2", "ai.suggest_vol_3", "ai.suggest_vol_4"],
    "client": ["ai.suggest_client_1", "ai.suggest_client_2", "ai.suggest_client_3", "ai.suggest_client_4"],
}


@login_required
def ai_assistant_view(request):
    role = request.user.role
    return render(request, "myapp/ai_assistant.html", {
        "ai_role": role,
        "ai_mode_key": {
            "admin": "ai.mode_admin", "curator": "ai.mode_curator",
            "volunteer": "ai.mode_volunteer", "client": "ai.mode_client",
        }.get(role, "ai.mode_client"),
        "ai_suggestion_keys": AI_SUGGESTION_KEYS.get(role, AI_SUGGESTION_KEYS["client"]),
    })


# Per-user ceiling on calls to the (billed, network-bound) Groq endpoint, so one
# logged-in account can't run up cost or saturate the upstream. Process-local
# LocMemCache like the login/reset throttles — a soft guard, not a hard quota.
AI_CHAT_RATE_LIMIT = 30
AI_CHAT_RATE_WINDOW_SECONDS = 5 * 60
AI_CHAT_MAX_MESSAGE_CHARS = 2000


@login_required
@require_POST
def ai_chat_view(request):
    """Thin transport for the role-based assistant (myapp/services/ai).

    Accepts JSON ``{message, history, lang}`` for a normal turn, or
    ``{confirm, history, lang}`` to run a previously-staged action. All the
    orchestration, permission and safety logic lives in the service package.
    """
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    confirm_id = (data.get("confirm") or "").strip()
    user_message = (data.get("message") or "").strip()[:AI_CHAT_MAX_MESSAGE_CHARS]
    history = data.get("history")
    lang_hint = data.get("lang") if data.get("lang") in ("ru", "tj", "en") else None

    if not confirm_id and not user_message:
        return JsonResponse({"error": "Напишите вопрос"}, status=400)

    rate_key = f"ai_chat_rate:{request.user.pk}"
    if cache.get(rate_key, 0) >= AI_CHAT_RATE_LIMIT:
        logger.info("ai: rate limit hit for user #%s", request.user.pk)
        return JsonResponse(
            {"error": "Слишком много запросов к ассистенту. Попробуйте через несколько минут."},
            status=429,
        )
    cache.set(rate_key, cache.get(rate_key, 0) + 1, AI_CHAT_RATE_WINDOW_SECONDS)

    ctx = build_user_context(request.user)
    lang = detect_lang(user_message, lang_hint)

    if confirm_id:
        reply = confirm_action(ctx, confirm_id, lang=lang)
    else:
        reply = run_conversation(ctx, user_message, history=history, lang=lang)

    payload = {"reply": reply.text}
    if reply.pending_action:
        payload["pending_action"] = {
            "id": reply.pending_action["id"],
            "summary": reply.pending_action["summary"],
        }
    if reply.action_result is not None:
        payload["action_ok"] = bool(reply.action_result.get("success"))
    return JsonResponse(payload)


@role_required("admin", "curator")
def create_event_view(request):
    if request.method == "POST":
        form = EventForm(request.POST)
        if form.is_valid():
            event = form.save(commit=False)
            event.curator = request.user
            event.save()
            volunteers = volunteer_queryset_for_region(event.region)
            local_date = timezone.localtime(event.date) if timezone.is_aware(event.date) else event.date
            sent = notify(
                volunteers, "event_new",
                params={"event_id": event.pk, "title": event.title,
                        "date": f"{local_date:%d.%m.%Y}", "time": f"{local_date:%H:%M}"},
                link=reverse("event_list"),
                dedupe_key=f"event:{event.pk}",
                extra={"description": event.description, "date": f"{event.date:%d.%m.%Y %H:%M}"},
            )
            Event.objects.filter(pk=event.pk).update(notifications_sent=True)
            community.mirror_event_safely(event)  # pinned post in the volunteer community (if configured)
            messages.success(request, f"Акция создана. Уведомлений отправлено: {sent}.")
            return redirect("admin_panel")
    else:
        form = EventForm()
    return render(request, "myapp/task_form.html", {"form": form, "title": "Новая акция", "title_i18n": "form.title_new_event"})


@role_required("admin", "curator")
def broadcast_view(request):
    if request.method == "POST":
        form = BroadcastForm(request.POST)
        if form.is_valid():
            broadcast = task_actions.send_broadcast(
                request.user, region=form.cleaned_data["region"],
                subject=form.cleaned_data["subject"], message=form.cleaned_data["message"],
            )
            messages.success(request, f"Сообщение отправлено. Получателей: {broadcast.sent_count}.")
            return redirect("admin_panel")
    else:
        form = BroadcastForm()
    return render(request, "myapp/task_form.html", {"form": form, "title": "Рассылка волонтерам", "title_i18n": "form.title_broadcast"})


@login_required
def photo_reports_view(request):
    # Approved reports + the viewer's own (with their review status); the
    # rest waits for the CRM review (myapp/views_photo_reports.py).
    reports_qs = photo_reports.visible_to(request.user).select_related("author", "event", "help_request")
    can_create = request.user.is_superuser or request.user.is_curator or request.user.is_volunteer
    if request.method == "POST":
        if not can_create:
            messages.error(request, "Фотоотчеты может публиковать только волонтер, куратор или админ.")
            return redirect("photo_reports")
        form = PhotoReportForm(request.POST, request.FILES, user=request.user)
        if form.is_valid():
            report = form.save(commit=False)
            report.author = request.user
            if not report.region:
                report.region = request.user.region
            clean, err = images.clean_original(form.cleaned_data["image"])
            if err:
                form.add_error("image", images.UPLOAD_ERROR_TEXT)
            else:
                report.image = clean
                report.save()
                images.queue_variants(report)
                messages.success(request, "Фотоотчёт отправлен на проверку координатору.")
                return redirect("photo_reports")
    else:
        # ?task=<id> (the "upload a report" links after completing / a rejection)
        # pre-selects the task — only one the form's scoped queryset offers.
        task_id = request.GET.get("task", "")
        initial = {"help_request": int(task_id)} if task_id.isdigit() else {}
        form = PhotoReportForm(user=request.user, initial=initial)
    # Visible to every authenticated user and grows without bound — paginate.
    page_obj = Paginator(reports_qs, PHOTO_REPORTS_PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "myapp/photo_reports.html", {
        "reports": page_obj, "page_obj": page_obj, "form": form, "can_create": can_create,
    })


@login_required
def volunteer_application_view(request):
    application = VolunteerApplication.objects.filter(user=request.user).select_related("reviewed_by").first()
    if request.method == "POST":
        if not application or application.status != VolunteerApplication.STATUS_REJECTED:
            messages.error(request, "Повторную заявку можно подать только после отклонения.")
            return redirect("volunteer_application")
        application.reapply()
        admins = Users.objects.filter(is_superuser=True, is_active=True)
        notify(
            admins, "application_submitted", variant="again",
            params={"application_id": application.pk, "username": request.user.username, "kind": "again"},
            link=reverse("volunteer_application_detail", args=[application.pk]),
        )
        messages.success(request, "Заявка отправлена повторно.")
        return redirect("volunteer_application")
    return render(request, "myapp/volunteer_application.html", {"application": application})


@role_required("admin")
def volunteer_applications_view(request):
    status_filter = request.GET.get("status", VolunteerApplication.STATUS_PENDING)
    region_filter = request.GET.get("region", "")
    search_query = request.GET.get("q", "").strip()

    applications = VolunteerApplication.objects.select_related("user", "reviewed_by")
    valid_statuses = {VolunteerApplication.STATUS_PENDING, VolunteerApplication.STATUS_APPROVED, VolunteerApplication.STATUS_REJECTED}
    if status_filter not in valid_statuses:
        status_filter = "all"
    else:
        applications = applications.filter(status=status_filter)
    if region_filter:
        applications = applications.filter(region=region_filter)
    if search_query:
        applications = applications.filter(Q(user__username__icontains=search_query) | Q(user__email__icontains=search_query))

    # `approved` / `all` grow with every volunteer ever onboarded — paginate.
    page_obj = Paginator(applications, APPLICATIONS_PAGE_SIZE).get_page(request.GET.get("page"))
    querystring = request.GET.copy()
    querystring.pop("page", None)

    # One aggregate query for the three tab counts.
    counts = VolunteerApplication.objects.aggregate(
        pending_count=Count("id", filter=Q(status=VolunteerApplication.STATUS_PENDING)),
        approved_count=Count("id", filter=Q(status=VolunteerApplication.STATUS_APPROVED)),
        rejected_count=Count("id", filter=Q(status=VolunteerApplication.STATUS_REJECTED)),
    )
    context = {
        "applications": page_obj,
        "page_obj": page_obj,
        "querystring": querystring.urlencode(),
        "status_filter": status_filter,
        "region_filter": region_filter,
        "search_query": search_query,
        "region_choices": REGION_CHOICES,
        **counts,
    }
    return render(request, "myapp/volunteer_applications.html", context)


@role_required("admin")
def volunteer_application_detail_view(request, pk):
    application = get_object_or_404(VolunteerApplication.objects.select_related("user", "reviewed_by"), pk=pk)
    return render(request, "myapp/volunteer_application_detail.html", {"application": application})


@role_required("admin")
@require_POST
def volunteer_application_approve_view(request, pk):
    application = get_object_or_404(VolunteerApplication.objects.select_related("user"), pk=pk)
    if not task_actions.approve_application(request.user, application).ok:
        messages.info(request, "Заявка уже одобрена.")
        return redirect("volunteer_applications")
    messages.success(request, f"Заявка пользователя {application.user.username} одобрена.")
    return redirect("volunteer_applications")


@role_required("admin")
@require_POST
def volunteer_application_reject_view(request, pk):
    application = get_object_or_404(VolunteerApplication.objects.select_related("user"), pk=pk)
    if not task_actions.reject_application(request.user, application).ok:
        messages.info(request, "Заявка уже отклонена.")
        return redirect("volunteer_applications")
    messages.success(request, f"Заявка пользователя {application.user.username} отклонена.")
    return redirect("volunteer_applications")


@role_required("admin", "curator")
@require_POST
def check_overdue_view(request):
    """Manual trigger for the overdue sweep (the same one the check_overdue_tasks
    cron command runs). Idempotent — alerts each overdue task once."""
    alerted = overdue.sweep_overdue_tasks()
    messages.info(request, f"Проверено. Просроченных запросов: {len(alerted)}.")
    return redirect("admin_panel")


@login_required
def map_view(request):
    is_staff = request.user.is_superuser or request.user.is_curator
    my_active_task_id = None
    if request.user.is_volunteer:
        active = HelpRequest.objects.filter(volunteer=request.user, status="active").values_list("id", flat=True).first()
        my_active_task_id = active
    return render(request, "myapp/map.html", {
        "is_ops_staff": is_staff,
        "my_active_task_id": my_active_task_id,
        "help_type_choices": HELP_TYPE_CHOICES,
        "region_choices": REGION_CHOICES,
        "priority_choices": PRIORITY_CHOICES,
        "status_choices": STATUS_CHOICES,
    })


def _task_point(task, subtitle_extra=""):
    # `color`/`glyph` are a legacy fallback; the ops map derives its own marker
    # style from kind + priority + is_overdue + status.
    color = "--danger" if task.is_urgent else ("--accent" if task.status == "pending" else "--primary")
    subtitle = task.get_region_display() or ""
    if subtitle_extra:
        subtitle = f"{subtitle} · {subtitle_extra}" if subtitle else subtitle_extra
    return {
        "id": task.pk,
        "kind": "task",
        "lat": float(task.latitude),
        "lng": float(task.longitude),
        "title": task.get_help_type_display(),
        "subtitle": subtitle,
        "status": task.status,
        "priority": task.priority,
        "help_type": task.help_type,
        "region": task.region,
        "is_overdue": task.is_overdue,
        "color": color,
        "glyph": "!" if task.is_urgent else "",
        "url": reverse("task_detail", args=[task.pk]),
    }


# Per-category ceilings for the operations map JSON. The querysets are already
# scoped (by status / ownership / region), so these are not a security boundary —
# they stop one request from loading and serialising an unbounded result set on a
# large deployment (a memory/CPU DoS that would hit every legitimate viewer too).
# Each category is ordered so the cap keeps the most operationally relevant rows;
# generous enough that a normal region never reaches them.
MAP_TASK_LIMIT = 500
MAP_VOLUNTEER_LIMIT = 500
MAP_EMERGENCY_LIMIT = 200
# Live volunteer positions (feature 7) in the staff map JSON — initial load
# only; the staff-only `map.marker` WS event keeps them fresh afterwards.
MAP_LIVE_LIMIT = 200


@login_required
def map_data_view(request):
    user = request.user
    points = []
    extra = {}

    if user.is_superuser or user.is_curator:
        # Staff branch only: current live volunteer positions (task id +
        # coordinates + timestamp, no identity), bounded like every category.
        extra["live"] = tracking.live_markers(limit=MAP_LIVE_LIMIT)
        tasks = (
            HelpRequest.objects.filter(status__in=["pending", "active"])
            .exclude(latitude__isnull=True)
            .select_related("client", "volunteer")
            .order_by("-is_urgent", "-created_at")[:MAP_TASK_LIMIT]
        )
        for task in tasks:
            points.append(_task_point(task, task.volunteer.username if task.volunteer else ""))

        volunteers = (
            Users.objects.filter(
                is_volunteer=True, is_active=True, profile__latitude__isnull=False
            )
            .select_related("profile")
            .order_by("-profile__location_updated_at")[:MAP_VOLUNTEER_LIMIT]
        )
        availability_colors = {
            "available": "--ok",
            "busy": "--accent",
            "offline": "--muted",
        }
        for volunteer in volunteers:
            profile = volunteer.profile
            points.append({
                "id": volunteer.pk,
                "kind": "volunteer",
                "lat": float(profile.latitude),
                "lng": float(profile.longitude),
                "title": volunteer.username,
                "subtitle": f"{volunteer.get_region_display() or '—'} · {profile.get_availability_status_display()}",
                "status": profile.availability_status,
                "region": volunteer.region,
                "color": availability_colors.get(profile.availability_status, "--muted"),
                "glyph": "V",
                # Do-not-disturb flag for the popup (staff-only branch; no dates/contacts).
                "away": profile.is_away_now,
            })

        emergencies = (
            EmergencyReport.objects.filter(
                status__in=EmergencyReport.OPEN_STATUSES, latitude__isnull=False
            )
            .select_related("help_request", "volunteer")
            .order_by("-created_at")[:MAP_EMERGENCY_LIMIT]
        )
        for report in emergencies:
            points.append({
                "id": report.pk,
                "kind": "emergency",
                "lat": float(report.latitude),
                "lng": float(report.longitude),
                "title": "Сигнал опасности",
                "subtitle": f"{report.volunteer.username} · запрос #{report.help_request_id}",
                "status": report.status,
                "region": report.region,
                "color": "--danger",
                "glyph": "!",
                "url": reverse("emergency_detail", args=[report.pk]),
            })
    elif user.is_volunteer:
        tasks = HelpRequest.objects.filter(status="pending").exclude(latitude__isnull=True).select_related("client")
        if user.region:
            tasks = tasks.filter(Q(region=user.region) | Q(region=""))
        for task in tasks.order_by("-is_urgent", "-created_at")[:MAP_TASK_LIMIT]:
            points.append(_task_point(task))

        my_active = HelpRequest.objects.filter(volunteer=user, status="active").exclude(latitude__isnull=True)
        for task in my_active:
            points.append(_task_point(task, "Моя задача"))

        # Active SOS of the volunteer's own region (pulsing marker, kept live by
        # the ``sos.marker`` WS event). Point + status only: no reporter name,
        # no task link — except on the volunteer's own report.
        if user.region:
            sos = (
                EmergencyReport.objects.filter(
                    status__in=EmergencyReport.OPEN_STATUSES, region=user.region, latitude__isnull=False,
                )
                .order_by("-created_at")[:MAP_EMERGENCY_LIMIT]
            )
            for report in sos:
                points.append({
                    "id": report.pk,
                    "kind": "emergency",
                    "lat": float(report.latitude),
                    "lng": float(report.longitude),
                    "title": "Сигнал опасности",
                    "subtitle": report.get_region_display(),
                    "status": report.status,
                    "region": report.region,
                    "color": "--danger",
                    "glyph": "!",
                    "url": reverse("emergency_detail", args=[report.pk]) if report.volunteer_id == user.pk else "",
                })
    elif user.is_client:
        tasks = (
            HelpRequest.objects.filter(client=user)
            .exclude(latitude__isnull=True)
            .order_by("-created_at")[:MAP_TASK_LIMIT]
        )
        for task in tasks:
            points.append(_task_point(task))

    # Lost & Found pet markers — public-safe (no reporter identity or contact
    # detail; see services.pets.public_point), shown to every authenticated role.
    points.extend(pets.open_board_points())

    profile = getattr(user, "profile", None)
    if profile and profile.has_location:
        points.append({
            "kind": "me",
            "lat": float(profile.latitude),
            "lng": float(profile.longitude),
            "title": "Я",
            "subtitle": "",
            "color": "--primary-2",
            "glyph": "•",
        })

    return JsonResponse({"points": points, **extra})


@login_required
@require_POST
def update_location_view(request):
    try:
        data = json.loads(request.body or "{}")
        latitude, longitude = data["latitude"], data["longitude"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return JsonResponse({"error": "latitude/longitude required"}, status=400)

    if not is_valid_coordinate(latitude, longitude):
        return JsonResponse({"error": "invalid latitude/longitude"}, status=400)

    profile, _ = Profile.objects.get_or_create(user=request.user)
    profile.set_location(float(latitude), float(longitude))
    return JsonResponse({
        "success": True,
        "location_updated_at": profile.location_updated_at.isoformat(),
    })


@login_required
@require_GET
def task_route_view(request, pk):
    """Volunteer -> client navigation data for one task: an OSRM route (or a
    graceful fallback) between the volunteer's current position and the
    task's location, with turn-by-turn steps, ETA and navigator deep links
    (``myapp/services/routing.py``). ``?profile=driving|walking`` and
    ``?preference=fastest|shortest`` (default: fastest by car, shortest on
    foot); up to 3 alternative routes in ``routes``.
    Read-only; never assigns or changes anything.

    Origin, first available: ``live`` — the assigned volunteer's own browser
    GPS (``lat``/``lng``; nobody else may supply an origin); ``tracking`` —
    their latest live-tracking point (feature 7); ``saved`` — their profile
    location."""
    from .services import routing, tracking

    task = get_object_or_404(
        HelpRequest.objects.select_related("client", "volunteer", "volunteer__profile"), pk=pk
    )
    user = request.user
    if not _route_permission(user, task):
        # A party of the task outside the active window gets the same graceful
        # "nothing to draw" answer as before feature 7 — not a route.
        if user.id in (task.client_id, task.volunteer_id):
            if not task.has_location:
                reason = "no_task_location"
            elif not task.volunteer_id:
                reason = "no_volunteer_assigned"
            else:
                reason = "task_not_active"
            return JsonResponse({"success": False, "reason": reason})
        return JsonResponse({"success": False, "reason": "forbidden"}, status=403)

    if not task.has_location:
        return JsonResponse({"success": False, "reason": "no_task_location"})

    volunteer = task.volunteer
    if not volunteer:
        return JsonResponse({"success": False, "reason": "no_volunteer_assigned"})

    profile_name = routing.normalize_profile(request.GET.get("profile"))
    origin_lat = origin_lng = None
    origin_source = "saved"
    # Only the assigned volunteer's own browser can supply a live GPS fix for
    # "origin" — a client or curator/admin viewing the same task always sees
    # the volunteer's tracked / saved position, never their own location.
    if user.id == volunteer.id:
        raw_lat, raw_lng = request.GET.get("lat"), request.GET.get("lng")
        if raw_lat is not None or raw_lng is not None:
            if raw_lat is None or raw_lng is None or not is_valid_coordinate(raw_lat, raw_lng):
                return JsonResponse({"success": False, "reason": "invalid_coordinates"}, status=400)
            origin_lat, origin_lng, origin_source = float(raw_lat), float(raw_lng), "live"

    profile = getattr(volunteer, "profile", None)
    if origin_lat is None:
        live = tracking.latest(task)
        saved_at = getattr(profile, "location_updated_at", None) if profile and profile.has_location else None
        if live and not (saved_at and saved_at > task.volunteer_last_at):
            origin_lat, origin_lng, origin_source = live["lat"], live["lng"], "tracking"

    if origin_lat is None:
        if not profile or not profile.has_location:
            return JsonResponse({"success": False, "reason": "no_volunteer_location"})
        origin_lat, origin_lng = float(profile.latitude), float(profile.longitude)

    destination = (float(task.latitude), float(task.longitude))
    result = dict(routing.route(
        (origin_lat, origin_lng), destination, profile=profile_name, steps=True,
        preference=request.GET.get("preference"), alternatives=True,
    ))
    result.setdefault("profile", profile_name)
    result["origin"] = {"lat": origin_lat, "lng": origin_lng, "source": origin_source}
    result["destination"] = {"lat": destination[0], "lng": destination[1]}
    return JsonResponse(result)


def _away_last_day_iso(volunteer):
    """Inclusive last do-not-disturb day as ISO date (None = no end date)."""
    from .templatetags.realtime_ui import away_last_day

    last_day = away_last_day(volunteer.profile.away_until)
    return last_day.isoformat() if last_day else None


@role_required("admin", "curator")
def task_recommendations_view(request, pk):
    """Smart Volunteer Matching — a ranked, explainable shortlist of nearby
    available volunteers for a pending task. Read-only: recommends, never
    assigns. Admin/curator only — this walks the volunteer directory, which
    a client or ordinary volunteer must not be able to query."""
    task = get_object_or_404(HelpRequest, pk=pk)
    ranked = recommend_volunteers(task, limit=5)

    def _serialize(item):
        volunteer = item["volunteer"]
        profile = volunteer.profile
        return {
            "volunteer_id": volunteer.id,
            "username": volunteer.username,
            "full_name": profile.full_name or volunteer.username,
            "region": volunteer.get_region_display() or "",
            "distance_km": item["distance_km"],
            "estimated_minutes": item["estimated_minutes"],
            "score": item["score"],
            "availability": item["availability"],
            "availability_display": profile.get_availability_status_display(),
            "active_task_count": item["active_task_count"],
            "same_region": item["same_region"],
            "skill_match": item["skill_match"],
            "location_freshness": item["location_freshness"],
            "reasons": item["reasons"],
        }

    away = [
        {
            "volunteer_id": volunteer.id,
            "full_name": volunteer.profile.full_name or volunteer.username,
            "region": volunteer.get_region_display() or "",
            "distance_km": distance_km,
            "away_until": _away_last_day_iso(volunteer),
        }
        for volunteer, distance_km in dispatch.away_volunteers_for(task)
    ]
    return JsonResponse({
        "task_has_location": task.has_location,
        "recommendations": [_serialize(item) for item in ranked],
        # Do-not-disturb volunteers — never ranked; assignable only with an
        # explicit confirmation (task_assign_volunteer_view, confirm_away=1).
        "away_volunteers": away,
    })


@role_required("admin", "curator")
@require_POST
def task_notify_volunteer_view(request, pk, volunteer_id):
    """Sends a targeted notification about a pending task to one recommended
    volunteer. This is explicitly NOT an assignment — the task stays pending
    until the volunteer accepts it themselves through the normal flow."""
    task = get_object_or_404(HelpRequest, pk=pk)
    if task.status != "pending":
        return JsonResponse({"success": False, "reason": "task_not_pending"})

    volunteer = get_object_or_404(Users, pk=volunteer_id, is_volunteer=True, is_active=True)
    # Message + send live in services.ai.actions, shared with the AI
    # ``notify_volunteer_about_task`` action.
    ai_actions.notify_volunteer_recommendation(request.user, task, volunteer)
    return JsonResponse({"success": True})


@role_required("admin", "curator")
@require_POST
def task_assign_volunteer_view(request, pk, volunteer_id):
    """Curator/admin assigns a pending task directly to a volunteer (the
    accept-recommendation action). Uses the same conditional UPDATE guard as
    accept_task_view so a curator assign and a volunteer self-accept can't both
    win."""
    task = get_object_or_404(HelpRequest.objects.select_related("client"), pk=pk)
    volunteer = get_object_or_404(
        Users.objects.select_related("profile"), pk=volunteer_id, is_volunteer=True, is_active=True
    )
    # Owner rule: a do-not-disturb volunteer may be assigned by hand only after
    # the curator explicitly confirmed the warning in the dispatch card.
    outcome = task_actions.assign(
        request.user, task, volunteer, confirm_away=request.POST.get("confirm_away") == "1"
    )
    if outcome.code == task_actions.AWAY:
        return JsonResponse({
            "success": False, "reason": "volunteer_away", "away_until": _away_last_day_iso(volunteer),
        }, status=409)
    if not outcome.ok:
        return JsonResponse({"success": False, "reason": "task_not_pending"})
    return JsonResponse({"success": True})


# ============================================================================
# Emergency / SOS
# ============================================================================

EMERGENCY_REPORT_LOCK_SECONDS = 30


def _can_view_emergency(user, report):
    """Staff see every report; a volunteer sees only their own (shared rule:
    ``services/access.py::can_view_emergency``)."""
    return access.can_view_emergency(user, report)


@role_required("volunteer")
@require_POST
def emergency_report_view(request, task_pk):
    """A volunteer raises an SOS on the active task assigned to them."""
    task = get_object_or_404(
        HelpRequest.objects.select_related("client"),
        pk=task_pk, volunteer=request.user, status="active",
    )

    # Short per-(volunteer, task) cooldown: a rapid second submit is bounced to
    # the existing report instead of hitting the create path again. It expires on
    # its own — the service-level dedup is the real duplicate guard.
    lock_key = f"emergency_report_lock:{request.user.pk}:{task.pk}"
    if not cache.add(lock_key, "1", EMERGENCY_REPORT_LOCK_SECONDS):
        existing = (
            EmergencyReport.objects.filter(
                help_request=task, volunteer=request.user, status__in=EmergencyReport.OPEN_STATUSES
            )
            .order_by("-created_at")
            .first()
        )
        if existing:
            messages.info(request, "У вас уже есть открытый сигнал по этому запросу.")
            return redirect("emergency_detail", pk=existing.pk)
        messages.info(request, "Сигнал уже обрабатывается…")
        return redirect("task_detail", pk=task.pk)

    report, created = emergency.report_emergency(
        volunteer=request.user,
        help_request=task,
        reason=request.POST.get("reason", ""),
        latitude=request.POST.get("latitude") or None,
        longitude=request.POST.get("longitude") or None,
    )
    if created:
        messages.success(request, "Сигнал отправлен. Куратор и администратор уведомлены.")
    else:
        messages.info(request, "У вас уже есть открытый сигнал по этому запросу.")
    return redirect("emergency_detail", pk=report.pk)


@role_required("admin", "curator")
def emergency_list_view(request):
    """CRM: every SOS report — filterable by status tab, region and free text."""
    status_filter = request.GET.get("status", "")
    region_filter = request.GET.get("region", "")
    search_query = request.GET.get("q", "").strip()

    reports = EmergencyReport.objects.select_related(
        "help_request", "help_request__client", "volunteer"
    )
    valid_statuses = {value for value, _ in EMERGENCY_STATUS_CHOICES}
    if status_filter in valid_statuses:
        reports = reports.filter(status=status_filter)
    if region_filter:
        reports = reports.filter(region=region_filter)
    if search_query:
        reports = reports.filter(
            Q(volunteer__username__icontains=search_query)
            | Q(help_request__client__username__icontains=search_query)
            | Q(reason__icontains=search_query)
        )

    paginator = Paginator(reports, 20)
    page_obj = paginator.get_page(request.GET.get("page"))
    querystring = request.GET.copy()
    querystring.pop("page", None)

    counts = EmergencyReport.objects.aggregate(
        total=Count("id"),
        open=Count("id", filter=Q(status=EmergencyReport.STATUS_OPEN)),
        acknowledged=Count("id", filter=Q(status=EmergencyReport.STATUS_ACKNOWLEDGED)),
        resolved=Count("id", filter=Q(status=EmergencyReport.STATUS_RESOLVED)),
        cancelled=Count("id", filter=Q(status=EmergencyReport.STATUS_CANCELLED)),
    )

    return render(request, "myapp/emergency_list.html", {
        "page_obj": page_obj,
        "status_filter": status_filter,
        "region_filter": region_filter,
        "search_query": search_query,
        "querystring": querystring.urlencode(),
        "counts": counts,
        "region_choices": REGION_CHOICES,
        "status_choices": EMERGENCY_STATUS_CHOICES,
    })


@login_required
def emergency_detail_view(request, pk):
    report = get_object_or_404(
        EmergencyReport.objects.select_related(
            "help_request", "help_request__client", "help_request__volunteer",
            "volunteer", "acknowledged_by", "resolved_by", "cancelled_by",
        ),
        pk=pk,
    )
    if not _can_view_emergency(request.user, report):
        messages.error(request, "У вас нет доступа к этой странице")
        return redirect("dashboard")

    is_staff = request.user.is_superuser or request.user.is_curator
    return render(request, "myapp/emergency_detail.html", {
        "report": report,
        "is_staff": is_staff,
        "can_act": is_staff and report.is_open,
        "show_location_map": report.has_location,
    })


_EMERGENCY_ACTIONS = {
    "acknowledge": lambda report, actor, note: emergency.acknowledge(report, actor=actor),
    "resolve": lambda report, actor, note: emergency.resolve(report, actor=actor, note=note),
    "cancel": lambda report, actor, note: emergency.cancel(report, actor=actor, note=note),
}


@role_required("admin", "curator")
@require_POST
def emergency_update_view(request, pk):
    """Curator/admin acts on a report: acknowledge / resolve / cancel, or
    re-alert staff when the first notification did not get through."""
    report = get_object_or_404(
        EmergencyReport.objects.select_related("volunteer", "help_request"), pk=pk
    )
    action = request.POST.get("action", "")

    if action == "realert":
        if not report.is_open:
            messages.warning(request, "Сигнал уже закрыт.")
        else:
            emergency.realert_staff(report)
            messages.success(request, "Уведомление отправлено повторно.")
        return redirect("emergency_detail", pk=pk)

    handler = _EMERGENCY_ACTIONS.get(action)
    if handler is None:
        messages.warning(request, "Неизвестное действие.")
        return redirect("emergency_detail", pk=pk)
    try:
        handler(report, request.user, request.POST.get("note", "").strip()[:2000])
    except ValueError:
        messages.warning(request, "Это действие недоступно для текущего статуса сигнала.")
        return redirect("emergency_detail", pk=pk)
    messages.success(request, "Статус сигнала обновлён.")
    return redirect("emergency_detail", pk=pk)


# ============================================================================
# In-kind donations — material assistance (Stage 6 increment C, reworked Stage 9)
# ============================================================================
# A Product catalogue of *needed items* + a Donation *offer* of physical goods.
# NO payment provider, no money. An offer is created `pending`; curators/admins
# review it (approve / cancel), a volunteer is optionally assigned to collect or
# deliver, and the goods move ready -> received -> distributed. All logic lives
# in services.donations; views stay thin.


def _is_donation_staff(user):
    return user.is_superuser or user.is_curator


def _can_view_donation(offer, user):
    """Donor, the assigned volunteer, and staff (curator + admin) can view an
    offer. It carries no financial data — curators coordinate distribution."""
    return (
        _is_donation_staff(user)
        or offer.donor_id == user.id
        or offer.assigned_volunteer_id == user.id
    )


def _notify_new_donation(offer):
    """Curators + admins hear about a new offer (in-app, email, Telegram)."""
    notify(
        staff_recipients(), "donation_new",
        params={"donation_id": offer.id, "item_label": offer.item_label,
                "quantity": offer.quantity, "donor_label": offer.donor_label},
        link=reverse("donation_detail", args=[offer.id]),
        dedupe_key=f"donation_new:{offer.id}",
    )


@login_required
def donate_goods_view(request):
    """"Donate goods" — the login/register-style page. Makes an ordinary
    ``Donation`` (the CRM queue, dashboards and analytics see it as any other
    offer), with photos, condition and a contact phone."""
    if request.method == "POST":
        form = DonationGoodsForm(request.POST, request.FILES)
        if form.is_valid():
            cd = form.cleaned_data
            try:
                offer = donations.create_offer(
                    donor=request.user,
                    item_name=cd["item_name"],
                    category=cd["category"],
                    quantity=cd["quantity"],
                    description=cd.get("description", ""),
                    condition=cd["condition"],
                    fulfilment=cd["fulfilment"],
                    location=cd.get("location", ""),
                    region=cd["region"],
                    contact_phone=cd["contact_phone"],
                    photos=cd.get("photos") or [],
                )
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                logger.info("donation #%s created by user #%s (goods page, photos=%s)",
                            offer.pk, request.user.pk, len(cd.get("photos") or []))
                _notify_new_donation(offer)
                messages.success(request, "Спасибо! Пожертвование отправлено — координатор свяжется с вами.")
                return redirect("profile")
    else:
        form = DonationGoodsForm(initial={
            "region": request.user.region,
            "contact_phone": donations.last_contact_phone(request.user),
        })
    return render(request, "myapp/donate_goods.html", {"form": form, "max_photos": MAX_DONATION_PHOTOS})


@login_required
@require_GET
def donation_photo_view(request, pk):
    """A donation photo, only for those who may see the offer (donor, assigned
    volunteer, staff). Production hands the file to nginx (X-Accel-Redirect to
    the `internal` /protected-media/; /media/donations/ itself is refused)."""
    photo = get_object_or_404(DonationPhoto.objects.select_related("donation"), pk=pk)
    if not _can_view_donation(photo.donation, request.user):
        raise Http404
    path = photo.image.name
    content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    if getattr(settings, "MEDIA_X_ACCEL", False):
        response = HttpResponse(content_type=content_type)
        response["X-Accel-Redirect"] = "/protected-media/" + path
    else:
        storage = photo.image.storage
        if not storage.exists(path):
            raise Http404
        response = FileResponse(storage.open(path, "rb"), content_type=content_type)
    response["Cache-Control"] = "private, max-age=3600"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
def donate_view(request):
    """Offer physical goods — pick a needed catalogue item or describe your own."""
    if request.method == "POST":
        form = DonationForm(request.POST)
        if form.is_valid():
            cd = form.cleaned_data
            try:
                offer = donations.create_offer(
                    donor=request.user,
                    donor_type=cd.get("donor_type") or "individual",
                    organization_name=cd.get("organization_name", ""),
                    product=cd.get("product"),
                    item_name=cd.get("item_name", ""),
                    category=cd.get("category") or "other",
                    quantity=cd.get("quantity") or 1,
                    unit=cd.get("unit", ""),
                    description=cd.get("description", ""),
                    fulfilment=cd.get("fulfilment") or "pickup",
                    location=cd.get("location", ""),
                    region=cd.get("region", ""),
                    message=cd.get("message", ""),
                )
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                _notify_new_donation(offer)
                messages.success(
                    request,
                    "Спасибо! Предложение записано и ожидает проверки координатором.",
                )
                return redirect("donation_detail", pk=offer.id)
    else:
        form = DonationForm()

    return render(request, "myapp/donate.html", {
        "form": form,
        "products": donations.active_products(),
        "recent_donations": donations.donations_for(request.user)[:5],
    })


@login_required
def my_donations_view(request):
    return render(request, "myapp/my_donations.html", {
        "donations": donations.donations_for(request.user),
    })


@login_required
def my_assigned_donations_view(request):
    """A volunteer's list of offers they have been asked to collect or deliver."""
    return render(request, "myapp/my_assigned_donations.html", {
        "donations": donations.assigned_to(request.user),
    })


@login_required
def donation_detail_view(request, pk):
    offer = get_object_or_404(
        Donation.objects.select_related("donor", "product", "reviewed_by", "assigned_volunteer"),
        pk=pk,
    )
    if not _can_view_donation(offer, request.user):
        messages.error(request, "У вас нет доступа к этой странице")
        return redirect("dashboard")
    is_staff = _is_donation_staff(request.user)
    # A curator is a national coordinator — the assign list is every active
    # volunteer, not the offer's region (region is shown in the option label so
    # the dispatcher can still pick a local one).
    assignable = None
    if is_staff and offer.status in ("approved", "ready"):
        assignable = Users.objects.filter(is_volunteer=True, is_active=True).order_by(
            "region", "username"
        )
    return render(request, "myapp/donation_detail.html", {
        "donation": offer,
        "is_staff": is_staff,
        "is_assigned_volunteer": offer.assigned_volunteer_id == request.user.id,
        "assignable_volunteers": assignable,
    })


@role_required("admin", "curator")
def donations_admin_view(request):
    """Staff (curator + admin) offer ledger — list, status tabs, operational
    summary. Carries no money."""
    status_filter = request.GET.get("status", "")
    qs = donations.all_offers()
    valid = {value for value, _ in DONATION_STATUS_CHOICES}
    if status_filter in valid:
        qs = qs.filter(status=status_filter)

    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page"))
    querystring = request.GET.copy()
    querystring.pop("page", None)

    return render(request, "myapp/donations_admin.html", {
        "page_obj": page_obj,
        "status_filter": status_filter,
        "status_choices": DONATION_STATUS_CHOICES,
        "querystring": querystring.urlencode(),
        "summary": donations.offer_summary(),
    })


_DONATION_ACTIONS = {
    "approve": donations.approve_offer,
    "ready": donations.mark_offer_ready,
    "received": donations.mark_offer_received,
    "distributed": donations.mark_offer_distributed,
    "cancel": donations.cancel_offer,
}
# The assigned volunteer may only advance the physical-handover steps.
_VOLUNTEER_ACTIONS = {"received", "distributed"}


@login_required
@require_POST
def donation_update_view(request, pk):
    """Advance an offer: approve / ready / received / distributed / cancel, or
    assign a volunteer. Staff (curator + admin) may do everything; the assigned
    volunteer may only mark goods received / distributed. Transitions are model
    methods; illegal moves raise ValueError."""
    offer = get_object_or_404(
        Donation.objects.select_related("donor", "assigned_volunteer"), pk=pk
    )
    action = request.POST.get("action", "")
    is_staff = _is_donation_staff(request.user)
    is_assigned = offer.assigned_volunteer_id == request.user.id

    if not is_staff and not (is_assigned and action in _VOLUNTEER_ACTIONS):
        messages.error(request, "У вас нет доступа к этому действию.")
        return redirect("donation_detail", pk=pk)

    if action == "assign":
        if not is_staff:
            messages.error(request, "У вас нет доступа к этому действию.")
            return redirect("donation_detail", pk=pk)
        volunteer = None
        vol_id = request.POST.get("volunteer_id", "")
        if vol_id:
            volunteer = Users.objects.filter(pk=vol_id, is_volunteer=True, is_active=True).first()
            if volunteer is None:
                messages.warning(request, "Волонтёр не найден.")
                return redirect("donation_detail", pk=pk)
        try:
            donations.assign_offer_volunteer(offer, volunteer=volunteer, actor=request.user)
        except ValueError:
            messages.warning(request, "Сейчас нельзя назначить волонтёра для этого предложения.")
            return redirect("donation_detail", pk=pk)
        if volunteer:
            notify(
                [volunteer], "donation_assigned",
                params={"donation_id": offer.id, "item_label": offer.item_label, "quantity": offer.quantity},
                link=reverse("donation_detail", args=[offer.id]),
            )
        messages.success(request, "Волонтёр обновлён.")
        return redirect("donation_detail", pk=pk)

    handler = _DONATION_ACTIONS.get(action)
    if handler is None:
        messages.warning(request, "Неизвестное действие.")
        return redirect("donation_detail", pk=pk)
    try:
        handler(offer, actor=request.user)
    except ValueError:
        messages.warning(request, "Это действие недоступно для текущего статуса предложения.")
        return redirect("donation_detail", pk=pk)

    if offer.donor_id:
        notify(
            [offer.donor], "donation_status", variant=offer.status,
            params={"donation_id": offer.id, "status": offer.status},
            link=reverse("donation_detail", args=[offer.id]),
            extra={"status_name": offer.get_status_display()},
        )
    messages.success(request, "Статус предложения обновлён.")
    return redirect("donation_detail", pk=pk)


# ============================================================================
# Lost & Found pets (Stage 6, increment D)
# ============================================================================
# A small community board for reuniting lost animals with their people. The
# board and the map show only safe fields (species, area, an approximate pin);
# the reporter's identity and contact phone are visible to the reporter and to
# staff only. No money, no task/emergency coupling. Business logic (creation,
# notifications, safe serialisation, deterministic match hints) lives in
# services.pets; views stay thin.


def _is_pet_staff(user):
    return user.is_superuser or user.is_curator


def _can_manage_pet(user, report):
    """The reporter or staff — may see contact details, possible matches and the
    status controls. Everyone else sees the safe public view only."""
    return _is_pet_staff(user) or report.reporter_id == user.id


@login_required
def pet_list_view(request):
    """The Lost & Found board: every open/matched report, newest first,
    filterable by type / species / region. Safe fields only — no reporter
    identity or contact detail is rendered here."""
    report_type = request.GET.get("type", "")
    species = request.GET.get("species", "")
    region = request.GET.get("region", "")
    if report_type not in dict(PET_REPORT_TYPE_CHOICES):
        report_type = ""
    if species not in dict(PET_SPECIES_CHOICES):
        species = ""
    if region not in dict(REGION_CHOICES):
        region = ""

    reports = pets.board_reports(report_type=report_type, species=species, region=region)
    paginator = Paginator(reports, 24)
    page_obj = paginator.get_page(request.GET.get("page"))
    querystring = request.GET.copy()
    querystring.pop("page", None)

    counts = PetReport.objects.filter(status__in=PetReport.OPEN_STATUSES).aggregate(
        total=Count("id"),
        lost=Count("id", filter=Q(report_type=PetReport.REPORT_LOST)),
        found=Count("id", filter=Q(report_type=PetReport.REPORT_FOUND)),
    )

    return render(request, "myapp/pet_list.html", {
        "page_obj": page_obj,
        "cards": [pets.public_card(r) for r in page_obj],
        "counts": counts,
        "type_filter": report_type,
        "species_filter": species,
        "region_filter": region,
        "type_choices": PET_REPORT_TYPE_CHOICES,
        "species_choices": PET_SPECIES_CHOICES,
        "region_choices": REGION_CHOICES,
        "querystring": querystring.urlencode(),
    })


@login_required
def pet_report_create_view(request):
    if request.method == "POST":
        form = PetReportForm(request.POST, request.FILES)
        image = None
        if form.is_valid() and form.cleaned_data.get("image"):
            image, err = images.clean_original(form.cleaned_data["image"])
            if err:
                form.add_error("image", images.UPLOAD_ERROR_TEXT)
        if form.is_valid():
            cd = form.cleaned_data
            report = pets.create_pet_report(
                reporter=request.user,
                report_type=cd["report_type"],
                description=cd["description"],
                species=cd["species"],
                pet_name=cd.get("pet_name", ""),
                breed=cd.get("breed", ""),
                region=cd.get("region", ""),
                latitude=cd.get("latitude"),
                longitude=cd.get("longitude"),
                contact_phone=cd.get("contact_phone", ""),
                image=image,
            )
            images.queue_variants(report)
            messages.success(request, "Объявление опубликовано. Координаторы уведомлены.")
            return redirect("pet_report_detail", pk=report.pk)
    else:
        form = PetReportForm()
    return render(request, "myapp/pet_form.html", {"form": form, "title": "Новое объявление о животном", "title_i18n": "form.title_new_pet"})


@login_required
def my_pet_reports_view(request):
    return render(request, "myapp/my_pet_reports.html", {
        "reports": pets.reports_for(request.user),
    })


@login_required
def pet_report_detail_view(request, pk):
    report = get_object_or_404(
        PetReport.objects.select_related("reporter", "reviewed_by"), pk=pk
    )
    can_manage = _can_manage_pet(request.user, report)
    # A resolved/closed report is off the public board — only its reporter and
    # staff can still open it directly.
    if not report.is_open and not can_manage:
        raise Http404

    is_staff = _is_pet_staff(request.user)
    is_owner = report.reporter_id == request.user.id
    return render(request, "myapp/pet_detail.html", {
        "report": report,
        "can_manage": can_manage,
        "is_staff": is_staff,
        "is_owner": is_owner,
        "can_edit": is_owner and report.is_open,
        "possible_matches": pets.possible_matches(report) if can_manage else [],
        "show_location_map": report.has_location,
    })


@login_required
def pet_report_edit_view(request, pk):
    report = get_object_or_404(PetReport, pk=pk)
    if report.reporter_id != request.user.id:
        messages.error(request, "У вас нет доступа к этой странице")
        return redirect("dashboard")
    if not report.is_open:
        messages.warning(request, "Закрытое объявление изменить нельзя.")
        return redirect("pet_report_detail", pk=pk)

    if request.method == "POST":
        form = PetReportForm(request.POST, request.FILES, instance=report)
        if form.is_valid() and "image" in form.changed_data and form.cleaned_data.get("image"):
            clean, err = images.clean_original(form.cleaned_data["image"])
            if err:
                form.add_error("image", images.UPLOAD_ERROR_TEXT)
            else:
                form.instance.image = clean
        if form.is_valid():
            report = form.save()
            images.queue_variants(report)
            messages.success(request, "Объявление обновлено.")
            return redirect("pet_report_detail", pk=pk)
    else:
        form = PetReportForm(instance=report)
    return render(request, "myapp/pet_form.html", {
        "form": form, "title": "Изменить объявление", "title_i18n": "form.title_edit_pet", "editing": True, "report": report,
    })


@login_required
@require_POST
def pet_report_delete_view(request, pk):
    report = get_object_or_404(PetReport, pk=pk)
    # The reporter, or an admin (superuser) cleaning up abuse. Curators moderate
    # via status changes, not deletion.
    if report.reporter_id != request.user.id and not request.user.is_superuser:
        messages.error(request, "У вас нет доступа к этой странице")
        return redirect("dashboard")
    report.delete()
    messages.success(request, "Объявление удалено.")
    return redirect("my_pet_reports")


@login_required
@require_POST
def pet_report_status_view(request, pk):
    """Advance a report's status. The reporter may resolve/close their own
    report; staff may also mark it matched or reopen it. Illegal moves are a
    no-op with a warning (the model raises ValueError)."""
    report = get_object_or_404(PetReport.objects.select_related("reporter"), pk=pk)
    is_staff = _is_pet_staff(request.user)
    if not (is_staff or report.reporter_id == request.user.id):
        messages.error(request, "У вас нет доступа к этой странице")
        return redirect("dashboard")

    action = request.POST.get("action", "")
    if action not in pets.ACTIONS:
        messages.warning(request, "Неизвестное действие.")
        return redirect("pet_report_detail", pk=pk)
    if action in pets.STAFF_ONLY_ACTIONS and not is_staff:
        messages.error(request, "Это действие доступно только координаторам.")
        return redirect("pet_report_detail", pk=pk)

    try:
        pets.apply_action(report, action, actor=request.user)
    except ValueError:
        messages.warning(request, "Это действие недоступно для текущего статуса объявления.")
        return redirect("pet_report_detail", pk=pk)
    messages.success(request, "Статус объявления обновлён.")
    return redirect("pet_report_detail", pk=pk)
