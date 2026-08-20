from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.http import JsonResponse
from django.shortcuts import redirect, render

from .forms import ForgotPasswordForm, LoginForm, ProfileForm, RegistrationForm, ResetPasswordForm, UserUpdateForm
from .models import Profile, Users


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
        login(request, user)
        messages.success(request, "Аккаунт создан. Проверьте email для подтверждения.")
        return redirect("profile")
    return render(request, "accounts/register.html", {"form": form})


def login_view(request):
    if request.user.is_authenticated:
        return redirect("profile")
    form = LoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = authenticate(request, username=form.cleaned_data["username"], password=form.cleaned_data["password"])
        if user and user.is_active:
            login(request, user)
            messages.success(request, f"Добро пожаловать, {user.username}!")
            return redirect("profile")
        messages.error(request, "Неверный username или пароль")
    return render(request, "accounts/login.html", {"form": form})


def logout_view(request):
    logout(request)
    messages.info(request, "Вы вышли из системы")
    return redirect("login")


@login_required
def profile_view(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)
    context = {"profile": profile}
    if request.user.is_volunteer:
        context["my_tasks"] = request.user.volunteer_tasks.all()[:5]
    if request.user.is_client:
        context["my_requests"] = request.user.client_requests.all()[:5]
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
    user = Users.objects.filter(reset_password_token=token).first()
    if not user or not user.reset_password_token_is_valid():
        messages.error(request, "Ссылка недействительна или устарела")
        return redirect("forgot_password")
    form = ResetPasswordForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user.set_password(form.cleaned_data["new_password"])
        user.clear_reset_password_token()
        user.save()
        messages.success(request, "Пароль изменен")
        return redirect("login")
    return render(request, "accounts/reset_password_confirm.html", {"form": form})


def confirm_email_view(request, token):
    user = Users.objects.filter(email_verification_token=token).first()
    if user and user.email_verification_token_is_valid():
        user.confirm_email()
        messages.success(request, "Email подтвержден")
        return redirect("profile")
    messages.error(request, "Ссылка недействительна или устарела")
    return redirect("login")


@login_required
def update_profile(request):
    if request.method != "POST":
        return JsonResponse({"success": False}, status=405)
    profile, _ = Profile.objects.get_or_create(user=request.user)
    request.user.region = request.POST.get("region", request.user.region)
    request.user.telegram_id = request.POST.get("telegram_id") or request.user.telegram_id
    request.user.save()
    profile.full_name = request.POST.get("full_name", profile.full_name)
    profile.age = request.POST.get("age") or profile.age
    profile.bio = request.POST.get("bio", profile.bio)
    if "image" in request.FILES:
        profile.image = request.FILES["image"]
    profile.save()
    return JsonResponse({"success": True})
