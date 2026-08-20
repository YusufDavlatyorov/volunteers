import json
import os
from datetime import timedelta

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from accounts.models import Profile, Users
from .forms import BroadcastForm, EventForm, HelpRequestFilterForm, HelpRequestForm, PhotoReportForm
from .models import Broadcast, Event, HelpRequest, PhotoReport
from .notifications import notify_users, volunteer_queryset_for_region


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
        if filter_form.cleaned_data.get("is_urgent"):
            requests_qs = requests_qs.filter(is_urgent=True)

    context = {
        "filter_form": filter_form,
        "free_requests": requests_qs.filter(status="pending"),
        "busy_requests": requests_qs.filter(status="active"),
        "archive_count": HelpRequest.objects.filter(status="completed").count(),
        "broadcasts": Broadcast.objects.select_related("sender")[:5],
        "events": Event.objects.select_related("curator")[:5],
    }
    return render(request, "myapp/admin_panel.html", context)


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
        if filter_form.cleaned_data.get("is_urgent"):
            tasks = tasks.filter(is_urgent=True)

    my_active = HelpRequest.objects.filter(volunteer=user, status="active") if user.is_volunteer else None
    return render(request, "myapp/task_list.html", {"tasks": tasks, "filter_form": filter_form, "my_active": my_active})


@login_required
def task_detail_view(request, pk):
    task = get_object_or_404(HelpRequest.objects.select_related("client", "volunteer"), pk=pk)
    user = request.user
    allowed = user.is_superuser or user.is_curator or task.client == user or task.volunteer == user or task.status == "pending"
    if not allowed:
        messages.error(request, "Этот запрос уже закреплен за другим волонтером")
        return redirect("profile")
    return render(request, "myapp/task_detail.html", {"task": task})


@role_required("volunteer")
def accept_task_view(request, pk):
    task = get_object_or_404(HelpRequest, pk=pk, status="pending")
    active_exists = HelpRequest.objects.filter(volunteer=request.user, status="active").exists()
    if active_exists:
        messages.warning(request, "Сначала завершите текущий запрос. Один волонтер работает с одним запросом.")
        return redirect("task_list")

    task.accept(request.user)
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
    if request.method == "POST":
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
    return render(request, "myapp/photo_reports.html", {"reports": reports, "form": form})


@role_required("admin", "curator")
def check_overdue_view(request):
    overdue = list(
        HelpRequest.objects.filter(status="active", alarm_sent=False, accepted_at__lt=timezone.now() - timedelta(hours=3))
    )
    curators = Users.objects.filter(Q(is_curator=True) | Q(is_superuser=True), is_active=True)
    for task in overdue:
        notify_users(curators, "Просроченный запрос", f"Запрос #{task.id} в работе больше 3 часов. Волонтер: {task.volunteer}")
        task.alarm_sent = True
        task.save(update_fields=["alarm_sent"])
    messages.info(request, f"Проверено. Просроченных запросов: {len(overdue)}.")
    return redirect("admin_panel")
