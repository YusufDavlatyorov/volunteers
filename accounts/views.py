import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from django.views.decorators.http import require_POST

from . import google_oauth
from .forms import ForgotPasswordForm, GoogleSignupForm, LoginForm, ProfileForm, RegistrationForm, ResetPasswordForm, UserUpdateForm
from .models import Profile, REGION_CHOICES, Users, clear_rating_cache, hash_token, validate_file_size
from myapp.models import VolunteerApplication
from myapp.notifications import queue_email
from myapp.services.notify import notify
from myapp.services import achievements, activity, images, profile_location
from server.client_ip import client_ip

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
    # Behind nginx REMOTE_ADDR is the proxy's address for everyone —
    # the shared helper reads the trusted proxy headers (server/client_ip.py).
    return client_ip(request)


def _too_many_attempts(cache_key, limit):
    return cache.get(cache_key, 0) >= limit


def _register_attempt(cache_key, window_seconds):
    cache.set(cache_key, cache.get(cache_key, 0) + 1, window_seconds)


def _file_volunteer_application(user, motivation):
    """A "volunteer" sign-up (password or Google) files a pending application
    and tells the admins — the role itself is granted only on approval."""
    application, _ = VolunteerApplication.objects.get_or_create(
        user=user,
        defaults={"region": user.region, "reason": motivation or ""},
    )
    admins = Users.objects.filter(is_superuser=True, is_active=True)
    notify(
        admins, "application_submitted",
        params={"application_id": application.pk, "username": user.username,
                "kind": "new", "region": user.region},
        link=reverse("volunteer_application_detail", args=[application.pk]),
        dedupe_key=f"application_new:{application.pk}",
        extra={"region_name": user.get_region_display() or None},
    )
    return application


def register_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    # ?role=client|volunteer pre-selects the choice the visitor already made
    # ("Request help" / "Become a volunteer"); they can still switch it.
    role = request.GET.get("role")
    initial = {"role": role} if role in ("client", "volunteer") else None
    form = RegistrationForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        token = user.generate_email_verification_token()
        verify_url = request.build_absolute_uri(f"/confirm-email/{token}/")
        queue_email(  # background delivery: registration doesn't wait on SMTP
            user.email,
            "Подтвердите email - KhayrKhoh",
            f"Здравствуйте, {user.username}!\nПодтвердите email по ссылке:\n{verify_url}",
        )
        if form.cleaned_data["role"] == "volunteer":
            _file_volunteer_application(user, form.cleaned_data.get("motivation", ""))
            messages.success(request, "Аккаунт создан. Заявка на волонтерство отправлена на рассмотрение администратору.")
        else:
            messages.success(request, "Аккаунт создан. Проверьте email для подтверждения.")
        login(request, user)
        # Back to what they came for (e.g. the request form) — same-site only.
        # Someone who switched to "volunteer" can't use the client's request form.
        next_url = _safe_next(request)
        if next_url and next_url.startswith(reverse("create_request")) and form.cleaned_data["role"] != "client":
            return redirect("dashboard")
        return _after_login(request)
    return render(request, "accounts/register.html", {
        "form": form, "next": _safe_next(request), "google_enabled": google_oauth.is_enabled(),
    })


def _safe_next(request):
    """``?next=`` (kept in the form's POST to the same URL) when it points
    back to this site — never an open redirect — else None."""
    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    ):
        return next_url
    return None


def _after_login(request):
    """The safe ``?next=`` page, else the dashboard."""
    next_url = _safe_next(request)
    return redirect(next_url) if next_url else redirect("dashboard")


def _register_link(next_url):
    """The sign-up link offered on the login page: carries ``next`` along, and
    a visitor sent here by "Request help" gets "I need help" pre-selected."""
    if not next_url:
        return reverse("register")
    params = {"next": next_url}
    if next_url.startswith(reverse("create_request")):
        params["role"] = "client"
    return f"{reverse('register')}?{urlencode(params)}"


def login_view(request):
    if request.user.is_authenticated:
        return _after_login(request)
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
            form.add_error(None, ValidationError(
                "Слишком много попыток входа. Попробуйте снова через несколько минут.", code="login_throttled"))
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
                return _after_login(request)
            _register_attempt(ip_key, LOGIN_ATTEMPT_WINDOW_SECONDS)
            _register_attempt(user_key, LOGIN_ATTEMPT_WINDOW_SECONDS)
            form.add_error(None, ValidationError("Неверный username или пароль", code="login_failed"))
    next_url = _safe_next(request)
    return render(request, "accounts/login.html", {
        "form": form,
        "next": next_url,
        "register_url": _register_link(next_url),
        "wants_help": bool(next_url and next_url.startswith(reverse("create_request"))),
        "google_enabled": google_oauth.is_enabled(),
    })


# --- Continue with Google (accounts/google_oauth.py has the protocol) ---------

GOOGLE_CALLBACK_IP_LIMIT = 30
GOOGLE_CALLBACK_WINDOW_SECONDS = 15 * 60


def _google_redirect_uri(request):
    # Built from the request, so it is https://khayrkhoh.tj/… in production
    # (SECURE_PROXY_SSL_HEADER + ALLOWED_HOSTS) and http://127.0.0.1:8000/… in
    # dev; both must be listed as authorized redirect URIs in Google Console.
    return request.build_absolute_uri(reverse("google_callback"))


def _require_google():
    if not google_oauth.is_enabled():
        raise Http404


def _login_with_google(request, user):
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    # A Google sign-in is a deliberate "keep me signed in" — same lifetime as
    # the password login's "remember me".
    request.session.set_expiry(settings.SESSION_COOKIE_AGE)
    logger.info("google login: user=%s", user.pk)


def _after_google(next_url, new_role=None):
    """Where a Google sign-in lands — the same as the password login: the safe
    ``?next=`` kept since /auth/google/, else the dashboard. A new account that
    isn't a client can't use the client's request form (like register_view)."""
    if next_url and new_role and new_role != "client" and next_url.startswith(reverse("create_request")):
        return redirect("dashboard")
    return redirect(next_url) if next_url else redirect("dashboard")


def google_login_view(request):
    _require_google()
    if request.user.is_authenticated:
        return _after_login(request)
    # ?next= is checked now (same-site only) and kept server-side in the flow.
    flow = google_oauth.start_flow(request.session, next_url=_safe_next(request))
    return redirect(google_oauth.authorization_url(flow, _google_redirect_uri(request)))


def google_callback_view(request):
    _require_google()
    if request.user.is_authenticated:
        return redirect("dashboard")
    if request.GET.get("error"):
        # The user pressed "Cancel" on Google's screen (access_denied) or similar.
        request.session.pop(google_oauth.SESSION_FLOW_KEY, None)
        messages.info(request, "Вход через Google отменён")
        return redirect("login")

    ip_key = f"google_callback:ip:{_client_ip(request)}"
    if _too_many_attempts(ip_key, GOOGLE_CALLBACK_IP_LIMIT):
        logger.warning("google callback throttled: ip=%s", _client_ip(request))
        messages.error(request, "Слишком много попыток. Попробуйте снова через несколько минут.")
        return redirect("login")
    _register_attempt(ip_key, GOOGLE_CALLBACK_WINDOW_SECONDS)

    flow = google_oauth.pop_flow(request.session, request.GET.get("state"))
    code = request.GET.get("code")
    if not flow or not code:
        messages.error(request, "Сессия входа через Google истекла. Попробуйте ещё раз.")
        return redirect("login")
    id_token = google_oauth.exchange_code(code, flow["verifier"], _google_redirect_uri(request))
    identity = google_oauth.verify_id_token(id_token, flow["nonce"]) if id_token else None
    if identity is None:
        messages.error(request, "Не удалось войти через Google. Попробуйте ещё раз.")
        return redirect("login")

    user = Users.objects.filter(google_sub=identity.sub).first()
    if user is None and identity.email_verified:
        # An existing password account with the same (Google-verified) email:
        # link it. Whoever controls that mailbox could reset the password
        # anyway, so this grants nothing new.
        user = Users.objects.filter(email=identity.email).first()
        if user is not None:
            if user.google_sub:
                # Already tied to a *different* Google account — don't silently re-link.
                messages.error(request, "Этот email уже привязан к другому Google-аккаунту. Войдите по паролю.")
                return redirect("login")
            user.google_sub = identity.sub
            fields = ["google_sub"]
            if not user.is_email_verified:
                user.is_email_verified = True
                user.email_verification_token = None
                user.email_verification_token_created_at = None
                fields += ["is_email_verified", "email_verification_token",
                           "email_verification_token_created_at"]
            user.save(update_fields=fields)
            logger.info("google account linked: user=%s", user.pk)

    if user is not None:
        if not user.is_active:
            messages.error(request, "Аккаунт отключён. Обратитесь к администратору.")
            return redirect("login")
        _login_with_google(request, user)
        messages.success(request, f"Добро пожаловать, {user.username}!")
        return _after_google(flow.get("next"))

    if not identity.email_verified:
        messages.error(request, "Email этого Google-аккаунта не подтверждён. Зарегистрируйтесь обычным способом.")
        return redirect("register")
    request.session[google_oauth.SESSION_PENDING_KEY] = {**identity.to_session(), "next": flow.get("next")}
    return redirect("google_signup")


def google_signup_view(request):
    """Finish a new account after Google: pick a role, username and region."""
    _require_google()
    if request.user.is_authenticated:
        return redirect("dashboard")
    pending = request.session.get(google_oauth.SESSION_PENDING_KEY)
    identity = google_oauth.GoogleIdentity.from_session(pending)
    if identity is None:
        request.session.pop(google_oauth.SESSION_PENDING_KEY, None)
        messages.error(request, "Сессия входа через Google истекла. Попробуйте ещё раз.")
        return redirect("login")
    if Users.objects.filter(email=identity.email).exists() or Users.objects.filter(google_sub=identity.sub).exists():
        # Created meanwhile (another tab) — the normal Google login handles it now.
        request.session.pop(google_oauth.SESSION_PENDING_KEY, None)
        return redirect("google_login")

    initial = {"username": google_oauth.suggest_username(
        identity, lambda name: Users.objects.filter(username=name).exists())}
    form = GoogleSignupForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            with transaction.atomic():
                user = Users.objects.create_user(
                    username=data["username"],
                    email=identity.email,
                    password=None,  # unusable — "Forgot password?" can set one later
                    region=data["region"],
                    is_client=data["role"] == "client",
                    is_email_verified=True,
                    google_sub=identity.sub,
                )
                if identity.name:
                    Profile.objects.filter(user=user, full_name="").update(
                        full_name=identity.name[:Users.FULL_NAME_MAX_LENGTH])
                if data["role"] == "volunteer":
                    _file_volunteer_application(user, data.get("motivation", ""))
        except IntegrityError:
            # Lost a race on the username (or email/sub) unique index.
            form.add_error("username", ValidationError("Такой username уже занят", code="username_taken"))
        else:
            request.session.pop(google_oauth.SESSION_PENDING_KEY, None)
            _login_with_google(request, user)
            if data["role"] == "volunteer":
                messages.success(request, "Аккаунт создан. Заявка на волонтерство отправлена на рассмотрение администратору.")
            else:
                messages.success(request, "Аккаунт создан.")
            return _after_google(pending.get("next"), new_role=data["role"])
    return render(request, "accounts/google_signup.html", {"form": form, "google_email": identity.email})


@require_POST
def logout_view(request):
    logout(request)
    messages.info(request, "Вы вышли из системы")
    return redirect("login")


@login_required
def profile_view(request):
    """"Who I am": the user's card, activity calendar, badges, saved location,
    community card and (volunteers) review summary. Deliberately NOT the role
    dashboard — "what do I do now" is myapp.views.dashboard_view — so nothing
    from services.dashboard is computed here."""
    user = request.user
    profile, _ = Profile.objects.get_or_create(user=user)
    # Prime the reverse one-to-one cache so user.profile (services, templates)
    # reuses this row instead of querying it again.
    user.profile = profile
    context = {
        "profile": profile,
        "location_stale": user.is_volunteer and profile_location.is_stale(profile),
        # Volunteer community card (Telegram, milestone 5): approved volunteers + staff.
        "community_card": _community_card(user),
        # Badges + progress (feature 6) — display only, never a matching input.
        "achievement_items": achievements.progress_for(user) if user.is_volunteer else [],
        # Activity calendar (volunteers and clients only; None for staff).
        "activity": activity.calendar(user),
        "activity_empty_key": "act.empty_volunteer" if user.is_volunteer else "act.empty_client",
    }
    return render(request, "accounts/profile.html", context)


def _community_card(user):
    from myapp.services import community
    from myapp.services.access import is_staff_user

    if not community.configured() or not (user.is_volunteer or is_staff_user(user)):
        return None
    return {"linked": user.telegram_id is not None}


@login_required
def edit_profile_view(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)
    user_form = UserUpdateForm(request.POST or None, instance=request.user)
    profile_form = ProfileForm(request.POST or None, request.FILES or None, instance=profile)
    if (request.method == "POST" and profile_form.is_valid() and "image" in profile_form.changed_data
            and profile_form.cleaned_data.get("image")):
        # A new avatar: stored without EXIF/GPS under a random name (images.clean_original).
        clean, err = images.clean_original(profile_form.cleaned_data["image"])
        if err:
            profile_form.add_error("image", images.UPLOAD_ERROR_TEXT)
        else:
            profile_form.instance.image = clean
    if request.method == "POST" and user_form.is_valid() and profile_form.is_valid():
        try:
            with transaction.atomic():
                user_form.save()
        except IntegrityError:
            # Lost a race on the username (or email) unique index after the check.
            request.user.refresh_from_db()
            user_form.add_error("username", ValidationError("Такой username уже занят", code="username_taken"))
            return render(request, "accounts/edit_profile.html", {"user_form": user_form, "profile_form": profile_form})
        if user_form.username_changed:
            # The rating and the About leaders cache display names (full name
            # or username) — rebuild them with the new one. Everything else
            # (login, chats, reviews, Telegram binding) is keyed by the user id.
            clear_rating_cache()
            logger.info("user #%s changed their username", request.user.pk)
        profile = profile_form.save()
        images.queue_variants(profile)
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
            # Queued, not sent inline: the response no longer takes measurably
            # longer when the address exists (an account-enumeration side channel).
            queue_email(
                user.email,
                "Сброс пароля - KhayrKhoh",
                f"Здравствуйте, {user.username}!\nСсылка для сброса пароля:\n{reset_url}",
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
        # Tolerate "@Name" in .env: the template adds the "@" and builds t.me/<name>.
        "bot_username": (getattr(settings, "TELEGRAM_BOT_USERNAME", "") or "").strip().lstrip("@"),
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
    # No linked Telegram → no place in the volunteer community chats.
    from myapp.services import community

    community.revoke_safely(request.user, reason="telegram_unlinked")
    messages.success(request, "Telegram отвязан от аккаунта.")
    return redirect("telegram_link")


@login_required
def update_profile(request):
    if request.method != "POST":
        return JsonResponse({"success": False}, status=405)

    # profile.save() below does not run field validators, so the upload-size
    # cap (a disk-exhaustion DoS guard) is enforced explicitly and up front,
    # before any partial write — exactly as ProfileForm does for edit-profile.
    clean_image = None
    if "image" in request.FILES:
        try:
            validate_file_size(request.FILES["image"])
        except ValidationError as exc:
            return JsonResponse({"success": False, "error": exc.messages[0]}, status=400)
        # A real image, stored without EXIF/GPS under a random name.
        clean_image, err = images.clean_original(request.FILES["image"])
        if err:
            return JsonResponse({"success": False, "error": images.UPLOAD_ERROR_TEXT}, status=400)

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

    full_name = request.POST.get("full_name", profile.full_name).strip()
    if len(full_name) > Users.FULL_NAME_MAX_LENGTH:
        # `error_i18n` + `error_args`: the same err.max_length key the forms
        # use, so a client can show it in EN/RU/TJ (`error` stays Russian).
        return JsonResponse({
            "success": False,
            "error": f"Имя не длиннее {Users.FULL_NAME_MAX_LENGTH} символов.",
            "error_i18n": "err.max_length",
            "error_args": {"limit_value": Users.FULL_NAME_MAX_LENGTH, "show_value": len(full_name)},
        }, status=400)
    profile.full_name = full_name
    raw_age = request.POST.get("age")
    if raw_age:
        try:
            age = int(raw_age)
        except (TypeError, ValueError):
            return JsonResponse({"success": False, "error": "Некорректный возраст."}, status=400)
        if 0 < age <= 120:
            profile.age = age
    profile.bio = request.POST.get("bio", profile.bio)
    if clean_image is not None:
        profile.image = clean_image
    profile.save()
    images.queue_variants(profile)  # WebP thumb/medium in the background
    return JsonResponse({"success": True})


@login_required
@require_POST
def set_language_view(request):
    """Store the UI language the user picked in the i18n.js switcher
    (Profile.language), so server-rendered email/Telegram notifications use it.
    Called by the frontend with ``language=ru|tj|en``; JSON response."""
    language = (request.POST.get("language") or "").strip().lower()
    if language not in {code for code, _ in Profile.LANGUAGE_CHOICES}:
        return JsonResponse({"success": False, "error": "invalid_language"}, status=400)
    profile, _ = Profile.objects.get_or_create(user=request.user)
    if profile.language != language:
        Profile.objects.filter(pk=profile.pk).update(language=language)
    return JsonResponse({"success": True, "language": language})


# A one-tap browser fix is rarely better than a few hundred metres indoors;
# anything vaguer than this would mislead the matching distance.
PROFILE_LOCATION_MAX_ACCURACY_M = 2000


@login_required
@require_POST
def profile_location_view(request):
    """"Обновить моё местоположение": a volunteer saves their current device
    position as the profile point that matching uses (owner decision — live
    tracking never writes it). Validated WGS84; JSON response; ID-only log."""
    from myapp.services.geo import is_valid_coordinate

    if not request.user.is_volunteer:
        return JsonResponse({"success": False, "error": "forbidden"}, status=403)
    lat, lng = request.POST.get("lat"), request.POST.get("lng")
    if lat is None or lng is None or not is_valid_coordinate(lat, lng):
        return JsonResponse({"success": False, "error": "invalid_coordinates"}, status=400)
    try:
        accuracy = float(request.POST.get("accuracy") or 0)
    except ValueError:
        accuracy = 0
    if accuracy > PROFILE_LOCATION_MAX_ACCURACY_M:
        return JsonResponse({"success": False, "error": "inaccurate"}, status=400)
    profile, _ = Profile.objects.get_or_create(user=request.user)
    profile.set_location(round(float(lat), 6), round(float(lng), 6))
    Profile.objects.filter(pk=profile.pk).update(location_reminded_at=None)
    logger.info("profile location updated by user #%s", request.user.pk)
    return JsonResponse({"success": True, "updated_at": profile.location_updated_at.isoformat()})
