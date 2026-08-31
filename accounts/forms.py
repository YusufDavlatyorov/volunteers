from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils import timezone

from myapp.models import HELP_TYPE_CHOICES
from .models import REGION_CHOICES, Profile, Users


class RegistrationForm(forms.ModelForm):
    ROLE_CHOICES = [
        ("volunteer", "Я волонтер"),
        ("client", "Мне нужна помощь"),
    ]

    role = forms.ChoiceField(choices=ROLE_CHOICES, widget=forms.RadioSelect)
    motivation = forms.CharField(
        required=False,
        label="Почему вы хотите стать волонтером",
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Необязательно, только для волонтеров"}),
    )
    password = forms.CharField(widget=forms.PasswordInput(attrs={"placeholder": "Минимум 8 символов"}))
    confirm_password = forms.CharField(widget=forms.PasswordInput(attrs={"placeholder": "Повторите пароль"}))

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("label_suffix", "")
        super().__init__(*args, **kwargs)

    class Meta:
        model = Users
        # telegram_id is not accepted here — a Telegram chat is bound only via
        # the verified code flow in accounts.views.telegram_link_view.
        fields = ["username", "email", "region"]
        widgets = {
            "username": forms.TextInput(attrs={"placeholder": "username"}),
            "email": forms.EmailInput(attrs={"placeholder": "you@example.com"}),
            "region": forms.Select(choices=[("", "Выберите регион")] + REGION_CHOICES),
        }

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        if len(username) < 4:
            raise forms.ValidationError("Username должен быть минимум 4 символа")
        if Users.objects.filter(username=username).exists():
            raise forms.ValidationError("Такой username уже занят")
        return username

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if Users.objects.filter(email=email).exists():
            raise forms.ValidationError("Такой email уже используется")
        return email

    def clean_password(self):
        password = self.cleaned_data.get("password")
        try:
            validate_password(password)
        except ValidationError as exc:
            raise forms.ValidationError(exc.messages)
        return password

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("password") != cleaned.get("confirm_password"):
            raise forms.ValidationError("Пароли не совпадают")
        return cleaned

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = user.email.lower()
        user.set_password(self.cleaned_data["password"])
        user.is_client = self.cleaned_data["role"] == "client"
        # is_volunteer is intentionally NOT set here: a "volunteer" role choice
        # only creates a pending VolunteerApplication (see accounts.views.register_view).
        # The role is granted only after admin approval.
        if commit:
            user.save()
        return user


class LoginForm(forms.Form):
    username = forms.CharField(widget=forms.TextInput(attrs={
        "placeholder": "Username",
        "autocomplete": "username",
        "autofocus": True,
        "aria-describedby": "id_username_error",
    }))
    password = forms.CharField(widget=forms.PasswordInput(attrs={
        "placeholder": "Password",
        "autocomplete": "current-password",
        "aria-describedby": "id_password_error",
    }))
    remember_me = forms.BooleanField(
        required=False,
        initial=True,
        widget=forms.CheckboxInput(attrs={"class": "checkbox-input"}),
    )


class ForgotPasswordForm(forms.Form):
    email = forms.EmailField(label="Email", widget=forms.EmailInput(attrs={"placeholder": "Ваш email"}))


class ResetPasswordForm(forms.Form):
    new_password = forms.CharField(label="Новый пароль", widget=forms.PasswordInput(attrs={"placeholder": "Новый пароль"}))
    confirm_password = forms.CharField(label="Повторите пароль", widget=forms.PasswordInput(attrs={"placeholder": "Повторите пароль"}))

    def __init__(self, *args, user=None, **kwargs):
        # The account being reset — passed so AUTH_PASSWORD_VALIDATORS'
        # UserAttributeSimilarityValidator can reject a password too close to
        # the username/email.
        self.user = user
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("new_password")
        if password != cleaned.get("confirm_password"):
            raise forms.ValidationError("Пароли не совпадают")
        if password:
            # Same policy as registration — the reset path must not be a way
            # around AUTH_PASSWORD_VALIDATORS (min length, common-password and
            # numeric-only blocklists, similarity to account attributes).
            try:
                validate_password(password, self.user)
            except ValidationError as exc:
                raise forms.ValidationError(exc.messages)
        return cleaned


class ProfileForm(forms.ModelForm):
    skills = forms.MultipleChoiceField(
        label="Навыки — чем могу помочь",
        choices=HELP_TYPE_CHOICES,
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Влияет на подбор задач в CRM.",
    )

    class Meta:
        model = Profile
        fields = ["full_name", "age", "image", "bio", "availability_status", "skills", "latitude", "longitude"]
        labels = {
            "full_name": "Полное имя",
            "age": "Возраст",
            "image": "Фото профиля",
            "bio": "О себе",
            "availability_status": "Статус доступности",
        }
        widgets = {
            "full_name": forms.TextInput(attrs={"placeholder": "Полное имя"}),
            "age": forms.NumberInput(attrs={"min": 1, "max": 120}),
            "bio": forms.Textarea(attrs={"rows": 4, "placeholder": "О себе, навыки, удобное время"}),
            "availability_status": forms.Select(attrs={"class": "control"}),
            "latitude": forms.HiddenInput(),
            "longitude": forms.HiddenInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.initial.setdefault("skills", self.instance.skills or [])

    def clean_skills(self):
        return list(self.cleaned_data.get("skills") or [])

    def clean(self):
        # DecimalField(max_digits=9) alone allows any value up to ~1000,
        # which is meaningless (and, on the map, misleading) for a real-world
        # WGS84 latitude/longitude — enforce the actual coordinate range here.
        cleaned = super().clean()
        lat, lng = cleaned.get("latitude"), cleaned.get("longitude")
        if (lat is None) != (lng is None):
            raise forms.ValidationError("Укажите широту и долготу вместе или не указывайте вовсе.")
        if lat is not None and lng is not None and not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise forms.ValidationError("Некорректные координаты.")
        return cleaned

    def save(self, commit=True):
        profile = super().save(commit=False)
        if "latitude" in self.changed_data or "longitude" in self.changed_data:
            profile.location_updated_at = timezone.now()
        if commit:
            profile.save()
        return profile


class UserUpdateForm(forms.ModelForm):
    class Meta:
        model = Users
        # telegram_id is managed separately via the verified Telegram-linking
        # flow (accounts.views.telegram_link_view), never edited free-form here.
        fields = ["email", "region"]
        labels = {
            "email": "Email",
            "region": "Регион",
        }
        widgets = {
            "email": forms.EmailInput(),
            "region": forms.Select(choices=[("", "Выберите регион")] + REGION_CHOICES),
        }

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if Users.objects.filter(email=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Email уже используется")
        return email
