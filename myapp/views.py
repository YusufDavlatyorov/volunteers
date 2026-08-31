import json
import os

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import Count, Max, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from accounts.models import Profile, REGION_CHOICES, Users
from .forms import (
    BroadcastForm,
    EventForm,
    HelpRequestFilterForm,
    HelpRequestForm,
    PhotoReportForm,
    TaskManagementFilterForm,
)
from .models import (
    Broadcast,
    Event,
    HELP_TYPE_CHOICES,
    HelpRequest,
    OVERDUE_THRESHOLD,
    PhotoReport,
    PRIORITY_CHOICES,
    STATUS_CHOICES,
    VolunteerApplication,
)
from .notifications import notify_users, volunteer_queryset_for_region
from .services import analytics, maps
from .services.geo import get_route, is_valid_coordinate
from .services.matching import location_freshness_label, recommend_volunteers


def role_required(*roles):
    def decorator(view_func):
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect("login")
            if request.user.is_superuser and "admin" in roles:
                return view_func(request, *args, **kwargs)
            if request.user.role not in roles:
                messages.error(request, "У вас нет доступа к этой странице")
                return redirect("profile")
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator


def about_view(request):
    stats = {
        "volunteers": Users.objects.filter(is_volunteer=True, is_active=True).count(),
        "clients": Users.objects.filter(is_client=True, is_active=True).count(),
        "completed": HelpRequest.objects.filter(status="completed").count(),
        "regions": Users.objects.exclude(region="").values("region").distinct().count(),
    }
    reports = PhotoReport.objects.select_related("author").all()[:6]
    return render(request, "myapp/about.html", {"stats": stats, "reports": reports})


@login_required
def dashboard_view(request):
    user = request.user
    if user.is_client:
        return redirect("create_request")
    if user.is_volunteer:
        return redirect("task_list")
    if VolunteerApplication.objects.filter(user=user).exists():
        return redirect("volunteer_application")
    return redirect("admin_panel")


@role_required("admin", "curator")
def admin_panel_view(request):
    requests_qs = HelpRequest.objects.select_related("client", "volunteer")
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

    now = timezone.now()
    context = {
        "filter_form": filter_form,
        "free_requests": requests_qs.filter(status="pending"),
        "busy_requests": requests_qs.filter(status="active"),
        "archive_count": HelpRequest.objects.filter(status="completed").count(),
        "upcoming_events": Event.objects.select_related("curator").filter(date__gte=now).order_by("date")[:5],
        "recent_broadcasts": Broadcast.objects.select_related("sender")[:5],
        "pending_volunteer_applications": VolunteerApplication.objects.filter(status=VolunteerApplication.STATUS_PENDING).count(),
        "total_requests": HelpRequest.objects.filter(status__in=["pending", "active"]).count(),
        "total_volunteers": Users.objects.filter(is_volunteer=True, is_active=True).count(),
        "total_clients": Users.objects.filter(is_client=True, is_active=True).count(),
        "total_events": Event.objects.count(),
        # CRM dashboard: real aggregate stats + a unified recent-activity feed,
        # both computed in myapp.services.analytics so this view stays thin.
        "stats": analytics.dashboard_stats(),
        "recent_activity": analytics.recent_activity(limit=8),
    }
    if request.user.is_superuser:
        context["recent_applications"] = VolunteerApplication.objects.select_related("user").order_by("-created_at")[:5]
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
        if availability_filter:
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

    context = {
        "people": people,
        "role": role,
        "region_filter": region_filter,
        "search_query": search_query,
        "availability_filter": availability_filter,
        "availability_choices": Profile.AVAILABILITY_CHOICES,
        "region_choices": REGION_CHOICES,
        "volunteer_count": Users.objects.filter(is_volunteer=True, is_active=True).count(),
        "client_count": Users.objects.filter(is_client=True, is_active=True).count(),
        "curator_count": Users.objects.filter(is_curator=True, is_active=True).count(),
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

    overdue_only = request.GET.get("overdue") == "1"
    if overdue_only:
        tasks = tasks.filter(status="active", accepted_at__lt=timezone.now() - OVERDUE_THRESHOLD)

    paginator = Paginator(tasks, 20)
    page_obj = paginator.get_page(request.GET.get("page"))

    querystring = request.GET.copy()
    querystring.pop("page", None)

    context = {
        "filter_form": filter_form,
        "page_obj": page_obj,
        "overdue_only": overdue_only,
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
    }
    return render(request, "myapp/crm_volunteer_detail.html", context)


@role_required("admin", "curator")
def event_list_view(request):
    now = timezone.now()
    events = Event.objects.select_related("curator")
    context = {
        "upcoming_events": events.filter(date__gte=now).order_by("date"),
        "past_events": events.filter(date__lt=now).order_by("-date"),
    }
    return render(request, "myapp/event_list.html", context)


@role_required("admin", "curator")
def broadcast_list_view(request):
    broadcasts = Broadcast.objects.select_related("sender")
    return render(request, "myapp/broadcast_list.html", {"broadcasts": broadcasts})


@role_required("volunteer", "admin", "curator")
def task_list_view(request):
    user = request.user
    tasks = HelpRequest.objects.filter(status="pending").select_related("client")
    if user.is_volunteer and user.region:
        tasks = tasks.filter(Q(region=user.region) | Q(region=""))

    filter_form = HelpRequestFilterForm(request.GET)
    if filter_form.is_valid():
        if filter_form.cleaned_data.get("help_type"):
            tasks = tasks.filter(help_type=filter_form.cleaned_data["help_type"])
        if filter_form.cleaned_data.get("region"):
            tasks = tasks.filter(region=filter_form.cleaned_data["region"])
        if filter_form.cleaned_data.get("priority"):
            tasks = tasks.filter(priority=filter_form.cleaned_data["priority"])
        if filter_form.cleaned_data.get("is_urgent"):
            tasks = tasks.filter(is_urgent=True)

    my_active = HelpRequest.objects.filter(volunteer=user, status="active") if user.is_volunteer else None
    return render(request, "myapp/task_list.html", {"tasks": tasks, "filter_form": filter_form, "my_active": my_active})


def _route_permission(user, task):
    """Who may see the volunteer<->client route for a task: the two parties
    involved plus staff — never an unrelated volunteer/client."""
    return user.is_superuser or user.is_curator or task.client_id == user.id or task.volunteer_id == user.id


def _can_view_task(user, task):
    """Who may open a task's detail page.

    Staff (admin/curator) always can. A client only ever sees their own
    requests. A volunteer sees a task they are already assigned to, or a
    still-pending task that would actually appear in their task_list (same
    region, or no region filter set) — never an arbitrary other client's
    pending request just because its status happens to be "pending", and
    never another volunteer's already-accepted task.
    """
    if user.is_superuser or user.is_curator:
        return True
    if user.is_client:
        return task.client_id == user.id
    if user.is_volunteer:
        if task.volunteer_id == user.id:
            return True
        if task.status == "pending":
            if not user.region:
                return True
            return not task.region or task.region == user.region
        return False
    return False


@login_required
def task_detail_view(request, pk):
    task = get_object_or_404(HelpRequest.objects.select_related("client", "volunteer"), pk=pk)
    user = request.user
    if not _can_view_task(user, task):
        messages.error(request, "У вас нет доступа к этой странице")
        return redirect("profile")
    show_route = task.status == "active" and bool(task.volunteer_id) and _route_permission(user, task)
    is_crm_staff = user.is_superuser or user.is_curator
    show_recommendations = task.status == "pending" and is_crm_staff
    # The route card already shows a map with the destination pin once a
    # volunteer is assigned; for every other state, a simple location-only
    # pin is still useful context and needs no extra endpoint (lat/lng are
    # already on the task).
    show_location_map = task.has_location and not show_route
    return render(
        request,
        "myapp/task_detail.html",
        {
            "task": task,
            "show_route": show_route,
            "show_recommendations": show_recommendations,
            "show_location_map": show_location_map,
            "show_history": is_crm_staff,
        },
    )


# accept_task_view's per-volunteer critical section. A HelpRequest-level DB
# constraint was deliberately ruled out here: CRM/admin flows and the
# matching algorithm's workload scoring (myapp.services.matching) legitimately
# model a volunteer holding more than one "active" row at once (see e.g.
# MatchingAlgorithmTests, CrmTasksViewTests) — the "one active task" rule is a
# self-service-accept-flow rule, not a fact about the data model as a whole,
# so it must not be baked into schema-level uniqueness.
ACCEPT_TASK_LOCK_TIMEOUT_SECONDS = 10


@role_required("volunteer")
@require_POST
def accept_task_view(request, pk):
    task = get_object_or_404(HelpRequest.objects.select_related("client"), pk=pk)

    # cache.add() only succeeds if the key is absent, which is atomic in
    # Django's cache backends — so of two concurrent accept_task_view calls
    # for the *same* volunteer (two different tasks, or the same one), only
    # one gets past this line at a time; the other is turned away immediately
    # instead of racing the "do I already have an active task" check below.
    lock_key = f"accept_task_lock:{request.user.pk}"
    if not cache.add(lock_key, "1", ACCEPT_TASK_LOCK_TIMEOUT_SECONDS):
        messages.warning(request, "Сначала завершите текущий запрос. Один волонтер работает с одним запросом.")
        return redirect("task_list")

    try:
        if HelpRequest.objects.filter(volunteer=request.user, status="active").exists():
            messages.warning(request, "Сначала завершите текущий запрос. Один волонтер работает с одним запросом.")
            return redirect("task_list")

        # Conditional UPDATE, not fetch-then-save: the WHERE clause is
        # evaluated by the database as part of one atomic statement, so if
        # two *different* volunteers click "accept" on this same task at the
        # same time, only the first UPDATE can still see status="pending" —
        # the second affects zero rows instead of silently overwriting the
        # first volunteer's acceptance.
        updated = HelpRequest.objects.filter(pk=task.pk, status="pending").update(
            volunteer=request.user, status="active", accepted_at=timezone.now()
        )
    finally:
        cache.delete(lock_key)

    if not updated:
        messages.warning(request, "Этот запрос уже принят другим волонтером.")
        return redirect("task_list")

    task.refresh_from_db()
    subject = "Запрос принят волонтером"
    message = (
        f"Запрос #{task.id} принят.\n"
        f"Клиент: {task.client.username}, телефон: {task.phone}\n"
        f"Волонтер: {request.user.username}\n"
        f"Адрес: {task.address}"
    )
    notify_users([task.client, request.user], subject, message)
    messages.success(request, "Вы приняли запрос. Клиент получил уведомление на email/Telegram.")
    return redirect("task_list")


@role_required("volunteer")
@require_POST
def complete_task_view(request, pk):
    task = get_object_or_404(HelpRequest, pk=pk, volunteer=request.user, status="active")
    task.complete()
    profile, _ = Profile.objects.get_or_create(user=request.user)
    profile.add_points(5 if task.is_urgent else 3)
    notify_users([task.client], "Запрос выполнен", f"Ваш запрос #{task.id} отмечен как выполненный. Спасибо!")
    messages.success(request, "Запрос завершен, рейтинг обновлен.")
    return redirect("task_list")


@role_required("client")
def create_request_view(request):
    if request.method == "POST":
        form = HelpRequestForm(request.POST)
        if form.is_valid():
            help_request = form.save(commit=False)
            help_request.client = request.user
            help_request.region = request.user.region
            # If the client didn't drop a pin, try to resolve the typed address
            # to a point so the request still shows on the operations map.
            # Best-effort: an un-geocodable address just means no coordinates.
            if not help_request.has_location and help_request.address:
                coords = maps.geocode(help_request.address, region=help_request.region)
                if coords:
                    help_request.latitude, help_request.longitude = coords
            help_request.save()
            volunteers = volunteer_queryset_for_region(request.user.region)
            notify_users(volunteers, "Новый запрос помощи", f"Новый запрос в регионе {request.user.get_region_display()}: {help_request.description[:180]}")
            messages.success(request, "Запрос создан. Волонтеры вашего региона получили уведомление.")
            return redirect("profile")
    else:
        form = HelpRequestForm()
    return render(request, "myapp/task_form.html", {"form": form, "title": "Создать запрос"})


@role_required("admin", "curator")
def completed_tasks_view(request):
    tasks = HelpRequest.objects.filter(status="completed").select_related("client", "volunteer")
    return render(request, "myapp/completed_tasks.html", {"tasks": tasks})


def rating_view(request):
    volunteers = Profile.objects.filter(user__is_volunteer=True, user__is_active=True).select_related("user").order_by("-rating", "user__username")[:30]
    by_region = volunteers.values("user__region").annotate(total=Count("id"))
    return render(request, "myapp/rating.html", {"volunteers": volunteers, "by_region": by_region})


@login_required
def ai_assistant_view(request):
    return render(request, "myapp/ai_assistant.html")


@login_required
def ai_chat_view(request):
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    user_message = data.get("message", "").strip()
    if not user_message:
        return JsonResponse({"error": "Напишите вопрос"}, status=400)

    if request.user.is_client:
        system_prompt = "Ты спокойный помощник для пожилого клиента. Отвечай просто, заботливо, не назначай лекарства, при опасных симптомах советуй врача или 103."
        fallback = "Понимаю. Если есть сильная боль, одышка, резкая слабость или падение, лучше сразу позвонить 103."
    else:
        system_prompt = "Ты помощник волонтера. Давай практичные советы по этике общения, безопасности, маршруту помощи и отчетности."
        fallback = "Совет волонтеру: заранее позвоните клиенту, уточните адрес и задачу, не берите деньги без подтверждения куратора."

    system_prompt += " Answer only in Tajik, Russian, or English. Use the same language as the user's message. If the message mixes languages, choose the clearest of these three languages."

    from dotenv import load_dotenv
    load_dotenv()

    api_key = getattr(settings, "GROQ_API_KEY", "") or os.getenv("GROQ_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")

    if not api_key:
        return JsonResponse({"reply": fallback})

    try:
        response = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": getattr(settings, "GROQ_MODEL", "llama-3.1-8b-instant"),
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                "temperature": 0.4,
                "max_completion_tokens": 500,
            },
            timeout=25,
        )
        response.raise_for_status()
        result = response.json()
        reply = result["choices"][0]["message"]["content"].strip()
        return JsonResponse({"reply": reply or fallback})
    except Exception as e:
        print(f"Groq error: {e}")
        return JsonResponse({"reply": fallback})


@role_required("admin", "curator")
def create_event_view(request):
    if request.method == "POST":
        form = EventForm(request.POST)
        if form.is_valid():
            event = form.save(commit=False)
            event.curator = request.user
            event.save()
            volunteers = volunteer_queryset_for_region(event.region)
            sent = notify_users(volunteers, f"Акция: {event.title}", f"{event.description}\nДата: {event.date:%d.%m.%Y %H:%M}")
            Event.objects.filter(pk=event.pk).update(notifications_sent=True)
            messages.success(request, f"Акция создана. Уведомлений отправлено: {sent}.")
            return redirect("admin_panel")
    else:
        form = EventForm()
    return render(request, "myapp/task_form.html", {"form": form, "title": "Новая акция"})


@role_required("admin", "curator")
def broadcast_view(request):
    if request.method == "POST":
        form = BroadcastForm(request.POST)
        if form.is_valid():
            broadcast = form.save(commit=False)
            broadcast.sender = request.user
            volunteers = volunteer_queryset_for_region(broadcast.region)
            broadcast.sent_count = notify_users(volunteers, broadcast.subject, broadcast.message)
            broadcast.save()
            messages.success(request, f"Сообщение отправлено. Получателей: {broadcast.sent_count}.")
            return redirect("admin_panel")
    else:
        form = BroadcastForm()
    return render(request, "myapp/task_form.html", {"form": form, "title": "Рассылка волонтерам"})


@login_required
def photo_reports_view(request):
    reports = PhotoReport.objects.select_related("author", "event", "help_request")
    can_create = request.user.is_superuser or request.user.is_curator or request.user.is_volunteer
    if request.method == "POST":
        if not can_create:
            messages.error(request, "Фотоотчеты может публиковать только волонтер, куратор или админ.")
            return redirect("photo_reports")
        form = PhotoReportForm(request.POST, request.FILES)
        if form.is_valid():
            report = form.save(commit=False)
            report.author = request.user
            if not report.region:
                report.region = request.user.region
            report.save()
            messages.success(request, "Фотоотчет добавлен.")
            return redirect("photo_reports")
    else:
        form = PhotoReportForm()
    return render(request, "myapp/photo_reports.html", {"reports": reports, "form": form, "can_create": can_create})


@login_required
def volunteer_application_view(request):
    application = VolunteerApplication.objects.filter(user=request.user).select_related("reviewed_by").first()
    if request.method == "POST":
        if not application or application.status != VolunteerApplication.STATUS_REJECTED:
            messages.error(request, "Повторную заявку можно подать только после отклонения.")
            return redirect("volunteer_application")
        application.reapply()
        admins = Users.objects.filter(is_superuser=True, is_active=True)
        notify_users(admins, "Повторная заявка волонтера", f"Пользователь {request.user.username} подал заявку на волонтерство повторно.")
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

    all_applications = VolunteerApplication.objects.all()
    context = {
        "applications": applications,
        "status_filter": status_filter,
        "region_filter": region_filter,
        "search_query": search_query,
        "region_choices": REGION_CHOICES,
        "pending_count": all_applications.filter(status=VolunteerApplication.STATUS_PENDING).count(),
        "approved_count": all_applications.filter(status=VolunteerApplication.STATUS_APPROVED).count(),
        "rejected_count": all_applications.filter(status=VolunteerApplication.STATUS_REJECTED).count(),
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
    if application.status == VolunteerApplication.STATUS_APPROVED:
        messages.info(request, "Заявка уже одобрена.")
        return redirect("volunteer_applications")
    application.approve(request.user)
    notify_users(
        [application.user],
        "Заявка на волонтерство одобрена",
        "Поздравляем! Ваша заявка на волонтерство одобрена. Теперь вам доступен список запросов.",
    )
    messages.success(request, f"Заявка пользователя {application.user.username} одобрена.")
    return redirect("volunteer_applications")


@role_required("admin")
@require_POST
def volunteer_application_reject_view(request, pk):
    application = get_object_or_404(VolunteerApplication.objects.select_related("user"), pk=pk)
    if application.status == VolunteerApplication.STATUS_REJECTED:
        messages.info(request, "Заявка уже отклонена.")
        return redirect("volunteer_applications")
    application.reject(request.user)
    notify_users(
        [application.user],
        "Заявка на волонтерство отклонена",
        "Ваша заявка на волонтерство отклонена администратором.",
    )
    messages.success(request, f"Заявка пользователя {application.user.username} отклонена.")
    return redirect("volunteer_applications")


@role_required("admin", "curator")
@require_POST
def check_overdue_view(request):
    overdue = list(
        HelpRequest.objects.filter(status="active", alarm_sent=False, accepted_at__lt=timezone.now() - OVERDUE_THRESHOLD)
    )
    curators = Users.objects.filter(Q(is_curator=True) | Q(is_superuser=True), is_active=True)
    for task in overdue:
        notify_users(curators, "Просроченный запрос", f"Запрос #{task.id} в работе больше 3 часов. Волонтер: {task.volunteer}")
        task.alarm_sent = True
        task.save(update_fields=["alarm_sent"])
    messages.info(request, f"Проверено. Просроченных запросов: {len(overdue)}.")
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


@login_required
def map_data_view(request):
    user = request.user
    points = []

    if user.is_superuser or user.is_curator:
        tasks = (
            HelpRequest.objects.filter(status__in=["pending", "active"])
            .exclude(latitude__isnull=True)
            .select_related("client", "volunteer")
        )
        for task in tasks:
            points.append(_task_point(task, task.volunteer.username if task.volunteer else ""))

        volunteers = Users.objects.filter(
            is_volunteer=True, is_active=True, profile__latitude__isnull=False
        ).select_related("profile")
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
            })
    elif user.is_volunteer:
        tasks = HelpRequest.objects.filter(status="pending").exclude(latitude__isnull=True).select_related("client")
        if user.region:
            tasks = tasks.filter(Q(region=user.region) | Q(region=""))
        for task in tasks:
            points.append(_task_point(task))

        my_active = HelpRequest.objects.filter(volunteer=user, status="active").exclude(latitude__isnull=True)
        for task in my_active:
            points.append(_task_point(task, "Моя задача"))
    elif user.is_client:
        tasks = HelpRequest.objects.filter(client=user).exclude(latitude__isnull=True)
        for task in tasks:
            points.append(_task_point(task))

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

    return JsonResponse({"points": points})


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
    task's location. Read-only; never assigns or changes anything."""
    task = get_object_or_404(
        HelpRequest.objects.select_related("client", "volunteer", "volunteer__profile"), pk=pk
    )
    if not _route_permission(request.user, task):
        return JsonResponse({"success": False, "reason": "forbidden"}, status=403)

    if not task.has_location:
        return JsonResponse({"success": False, "reason": "no_task_location"})

    volunteer = task.volunteer
    if not volunteer:
        return JsonResponse({"success": False, "reason": "no_volunteer_assigned"})

    origin_lat = origin_lng = None
    origin_source = "saved"
    # Only the assigned volunteer's own browser can supply a live GPS fix for
    # "origin" — a client or curator/admin viewing the same task always sees
    # the volunteer's last saved position, never their own location as origin.
    if request.user.id == volunteer.id:
        raw_lat, raw_lng = request.GET.get("lat"), request.GET.get("lng")
        if raw_lat is not None or raw_lng is not None:
            if raw_lat is None or raw_lng is None or not is_valid_coordinate(raw_lat, raw_lng):
                return JsonResponse({"success": False, "reason": "invalid_coordinates"}, status=400)
            origin_lat, origin_lng, origin_source = float(raw_lat), float(raw_lng), "live"

    if origin_lat is None:
        profile = getattr(volunteer, "profile", None)
        if not profile or not profile.has_location:
            return JsonResponse({"success": False, "reason": "no_volunteer_location"})
        origin_lat, origin_lng = float(profile.latitude), float(profile.longitude)

    destination = (float(task.latitude), float(task.longitude))
    result = get_route((origin_lat, origin_lng), destination)
    result["origin"] = {"lat": origin_lat, "lng": origin_lng, "source": origin_source}
    result["destination"] = {"lat": destination[0], "lng": destination[1]}
    return JsonResponse(result)


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
            "location_freshness": item["location_freshness"],
            "reasons": item["reasons"],
        }

    return JsonResponse({
        "task_has_location": task.has_location,
        "recommendations": [_serialize(item) for item in ranked],
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
    subject = "Рекомендованный запрос помощи"
    message = (
        f"Куратор рекомендует вам запрос #{task.id} ({task.get_help_type_display()}) "
        f"в регионе {task.get_region_display() or '—'}.\n"
        f"Это рекомендация, а не назначение — запрос остаётся свободным, пока вы сами его не примете."
    )
    notify_users([volunteer], subject, message)
    return JsonResponse({"success": True})
