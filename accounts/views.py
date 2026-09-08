import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import ForgotPasswordForm, LoginForm, ProfileForm, RegistrationForm, ResetPasswordForm, UserUpdateForm
from .models import Profile, REGION_CHOICES, Users, hash_token, validate_file_size
from myapp.models import VolunteerApplication
from myapp.notifications import notify_users
from myapp.services import dashboard

logger = logging.getLogger(__name__)


# Simple cache-based brute-force/abuse throttling (no new dependency: Django's
# default local-memory cache backend is active even with no CACHES setting).
# Two independent counters per action — by client IP and by the identifier
# being targeted (username / email) — so an attacker can't dodge the limit
# either by rotating IPs against one account or by spraying many accounts
# from one IP.
LOGIN_IP_ATTEMPT_LIMIT = 20
LOGIN_USER_ATTEMPT_LIMIT = 5
LOGIN_ATTEMPT_WINDOW_SECONDS = 15 * 60

PASSWORD_RESET_IP_ATTEMPT_LIMIT = 5
PASSWORD_RESET_WINDOW_SECONDS = 15 * 60


def _client_ip(request):
    return request.META.get("REMOTE_ADDR", "unknown")


def _too_many_attempts(cache_key, limit):
    return cache.get(cache_key, 0) >= limit


def _register_attempt(cache_key, window_seconds):
    cache.set(cache_key, cache.get(cache_key, 0) + 1, window_seconds)


def register_view(request):
    if request.user.is_authenticated:
        return redirect("profile")
    form = RegistrationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        token = user.generate_email_verification_token()
        verify_url = request.build_absolute_uri(f"/confirm-email/{token}/")
        send_mail(
            "Подтвердите email - Generation Connect",
            f"Здравствуйте, {user.username}!\nПодтвердите email по ссылке:\n{verify_url}",
            settings.DEFAULT_FROM_EMAIL,
            [user.email],
            fail_silently=True,
        )
        if form.cleaned_data["role"] == "volunteer":
            VolunteerApplication.objects.get_or_create(
                user=user,
                defaults={
                    "region": user.region,
                    "reason": form.cleaned_data.get("motivation", ""),
                },
            )
            admins = Users.objects.filter(is_superuser=True, is_active=True)
            notify_users(
                admins,
                "Новая заявка волонтера",
                f"Пользователь {user.username} подал заявку на волонтерство в регионе {user.get_region_display() or '—'}.",
            )
            messages.success(request, "Аккаунт создан. Заявка на волонтерство отправлена на рассмотрение администратору.")
        else:
            messages.success(request, "Аккаунт создан. Проверьте email для подтверждения.")
        login(request, user)
        return redirect("profile")
    return render(request, "accounts/register.html", {"form": form})


def login_view(request):
    if request.user.is_authenticated:
        return redirect("profile")
    form = LoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        username = form.cleaned_data["username"].strip()
        ip_key = f"login_attempts:ip:{_client_ip(request)}"
        user_key = f"login_attempts:user:{username.lower()}"
        if _too_many_attempts(ip_key, LOGIN_IP_ATTEMPT_LIMIT) or _too_many_attempts(user_key, LOGIN_USER_ATTEMPT_LIMIT):
            # Username (not a secret) + IP so an operator can spot a brute-force
            # sweep in the logs; the password is never touched.
            logger.warning(
                "login throttled: ip=%s username=%s", _client_ip(request), username.lower(),
            )
            form.add_error(None, "Слишком много попыток входа. Попробуйте снова через несколько минут.")
        else:
            user = authenticate(request, username=username, password=form.cleaned_data["password"])
            if user and user.is_active:
                cache.delete(user_key)
                login(request, user)
                if form.cleaned_data.get("remember_me"):
                    request.session.set_expiry(settings.SESSION_COOKIE_AGE)
                else:
                    request.session.set_expiry(0)
                messages.success(request, f"Добро пожаловать, {user.username}!")
                return redirect("profile")
            _register_attempt(ip_key, LOGIN_ATTEMPT_WINDOW_SECONDS)
            _register_attempt(user_key, LOGIN_ATTEMPT_WINDOW_SECONDS)
            form.add_error(None, "Неверный username или пароль")
    return render(request, "accounts/login.html", {"form": form})


@require_POST
def logout_view(request):
    logout(request)
    messages.info(request, "Вы вышли из системы")
    return redirect("login")


@login_required
def profile_view(request):
    """The one role-aware dashboard. All per-role aggregation lives in
    myapp.services.dashboard (scoped to request.user); this view stays thin."""
    profile, _ = Profile.objects.get_or_create(user=request.user)
    hour = timezone.localtime().hour
    greeting = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
    context = {
        "profile": profile,
        "greeting": greeting,
        "volunteer_application": VolunteerApplication.objects.filter(user=request.user).first(),
        **dashboard.for_user(request.user),
    }
    return render(request, "accounts/profile.html", context)


@login_required
def edit_profile_view(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)
    user_form = UserUpdateForm(request.POST or None, instance=request.user)
    profile_form = ProfileForm(request.POST or None, request.FILES or None, instance=profile)
    if request.method == "POST" and user_form.is_valid() and profile_form.is_valid():
        user_form.save()
        profile_form.save()
        messages.success(request, "Профиль обновлен")
        return redirect("profile")
    return render(request, "accounts/edit_profile.html", {"user_form": user_form, "profile_form": profile_form})


def forgot_password_view(request):
    form = ForgotPasswordForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        ip_key = f"password_reset_attempts:ip:{_client_ip(request)}"
        if _too_many_attempts(ip_key, PASSWORD_RESET_IP_ATTEMPT_LIMIT):
            logger.warning("password-reset throttled: ip=%s", _client_ip(request))
            messages.error(request, "Слишком много попыток. Попробуйте снова через несколько минут.")
            return redirect("forgot_password")
        _register_attempt(ip_key, PASSWORD_RESET_WINDOW_SECONDS)
        user = Users.objects.filter(email=form.cleaned_data["email"].lower()).first()
        if user:
            token = user.generate_reset_password_token()
            reset_url = request.build_absolute_uri(f"/reset-password/{token}/")
            send_mail(
                "Сброс пароля - Generation Connect",
                f"Здравствуйте, {user.username}!\nСсылка для сброса пароля:\n{reset_url}",
                settings.DEFAULT_FROM_EMAIL,
                [user.email],
                fail_silently=True,
            )
        messages.success(request, "Если email найден, ссылка отправлена.")
        return redirect("login")
    return render(request, "accounts/forgot_password.html", {"form": form})


def reset_password_confirm_view(request, token):
    user = Users.objects.filter(reset_password_token=hash_token(token)).first()
    if not user or not user.reset_password_token_is_valid():
        messages.error(request, "Ссылка недействительна или устарела")
        return redirect("forgot_password")
    form = ResetPasswordForm(request.POST or None, user=user)
    if request.method == "POST" and form.is_valid():
        user.set_password(form.cleaned_data["new_password"])
        user.clear_reset_password_token()
        user.save()
        messages.success(request, "Пароль изменен")
        return redirect("login")
    return render(request, "accounts/reset_password_confirm.html", {"form": form})


def confirm_email_view(request, token):
    user = Users.objects.filter(email_verification_token=hash_token(token)).first()
    if user and user.email_verification_token_is_valid():
        user.confirm_email()
        messages.success(request, "Email подтвержден")
        return redirect("profile")
    messages.error(request, "Ссылка недействительна или устарела")
    return redirect("login")


@login_required
def telegram_link_view(request):
    """Telegram-linking control panel for the logged-in user.

    A POST mints a fresh one-time code and renders it exactly once; a GET just
    shows the current link status. The code is redeemed by sending it to the
    bot from the target chat — see myapp/services/telegram_link.py. telegram_id
    is deliberately writable *only* through this verified round-trip (never via
    a plain profile form), so a stored binding always implies proof of control
    of both the account and the chat.
    """
    raw_code = None
    if request.method == "POST":
        raw_code = request.user.generate_telegram_link_token()
    request.user.refresh_from_db()
    return render(request, "accounts/telegram_link.html", {
        "raw_code": raw_code,
        "bot_username": getattr(settings, "TELEGRAM_BOT_USERNAME", ""),
        "is_linked": request.user.telegram_id is not None,
        "code_active": request.user.telegram_link_token_is_valid(),
    })


@login_required
@require_POST
def telegram_unlink_view(request):
    request.user.telegram_id = None
    request.user.telegram_link_token = None
    request.user.telegram_link_token_created_at = None
    request.user.save(update_fields=[
        "telegram_id", "telegram_link_token", "telegram_link_token_created_at",
    ])
    messages.success(request, "Telegram отвязан от аккаунта.")
    return redirect("telegram_link")


@login_required
def update_profile(request):
    if request.method != "POST":
        return JsonResponse({"success": False}, status=405)

    # profile.save() below does not run field validators, so the upload-size
    # cap (a disk-exhaustion DoS guard) is enforced explicitly and up front,
    # before any partial write — exactly as ProfileForm does for edit-profile.
    if "image" in request.FILES:
        try:
            validate_file_size(request.FILES["image"])
        except ValidationError as exc:
            return JsonResponse({"success": False, "error": exc.messages[0]}, status=400)

    profile, _ = Profile.objects.get_or_create(user=request.user)

    # Validate the same way the forms do — this endpoint has no ModelForm, so
    # without these checks an arbitrary `region` string (breaking region
    # filtering / matching for that user) or a non-numeric `age` (a 500 on
    # profile.save()) would go straight to the database.
    valid_regions = {value for value, _ in REGION_CHOICES}
    submitted_region = request.POST.get("region")
    if submitted_region is not None and (submitted_region in valid_regions or submitted_region == ""):
        request.user.region = submitted_region
    # telegram_id is intentionally NOT settable here: binding a Telegram chat
    # requires the verified code round-trip in telegram_link_view.
    request.user.save()

    profile.full_name = request.POST.get("full_name", profile.full_name)[:255]
    raw_age = request.POST.get("age")
    if raw_age:
        try:
            age = int(raw_age)
        except (TypeError, ValueError):
            return JsonResponse({"success": False, "error": "Некорректный возраст."}, status=400)
        if 0 < age <= 120:
            profile.age = age
    profile.bio = request.POST.get("bio", profile.bio)
    if "image" in request.FILES:
        profile.image = request.FILES["image"]
    profile.save()
    return JsonResponse({"success": True})
