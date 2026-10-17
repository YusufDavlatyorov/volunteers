from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils import timezone

from myapp.models import HELP_TYPE_CHOICES
from .form_i18n import localize
from .models import REGION_CHOICES, Profile, Users

# i18n.js keys for the region <select> every account form shares.
REGION_FIELD_I18N = {"label": "field.region", "choices": "region.", "empty": "choice.select_region"}


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

    # i18n.js keys (accounts/form_i18n.py); the labels live in register.html.
    I18N = {
        "region": REGION_FIELD_I18N,
        "motivation": {"ph": "form.optional_volunteers_ph"},
        "password": {"ph": "form.password_min_ph"},
        "confirm_password": {"ph": "form.password_repeat"},
    }

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("label_suffix", "")
        super().__init__(*args, **kwargs)
        # The display limit, not the (wider, legacy-safe) DB column: also sets
        # the input's maxlength so the browser stops at 30.
        self.fields["username"].max_length = Users.USERNAME_MAX_LENGTH
        self.fields["username"].widget.attrs["maxlength"] = Users.USERNAME_MAX_LENGTH
        localize(self, self.I18N)

    class Meta:
        model = Users
        # telegram_id is not accepted here — a Telegram chat is bound only via
        # the verified code flow in accounts.views.telegram_link_view.
        fields = ["username", "email", "region"]
        widgets = {
            "username": forms.TextInput(attrs={"placeholder": "username", "maxlength": Users.USERNAME_MAX_LENGTH}),
            "email": forms.EmailInput(attrs={"placeholder": "you@example.com"}),
            "region": forms.Select(choices=[("", "Выберите регион")] + REGION_CHOICES),
        }

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        if len(username) < 4:
            raise forms.ValidationError("Username должен быть минимум 4 символа", code="username_too_short")
        if Users.objects.filter(username=username).exists():
            raise forms.ValidationError("Такой username уже занят", code="username_taken")
        return username

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if Users.objects.filter(email=email).exists():
            raise forms.ValidationError("Такой email уже используется", code="email_taken")
        return email

    def clean_password(self):
        password = self.cleaned_data.get("password")
        try:
            validate_password(password)
        except ValidationError as exc:
            # Re-raise the validators' own ValidationErrors (not just their
            # English .messages) so each keeps its code/params — the
            # error_i18n_attrs tag maps those to i18n.js keys.
            raise forms.ValidationError(exc.error_list)
        return password

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("password") != cleaned.get("confirm_password"):
            raise forms.ValidationError("Пароли не совпадают", code="password_mismatch")
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


class GoogleSignupForm(forms.Form):
    """The rest of a sign-up after "Continue with Google": the email comes from
    Google (verified) and there is no password, so only role / username /
    region (+ motivation for volunteers) are asked. Same role semantics as
    RegistrationForm — "volunteer" only files a VolunteerApplication."""

    role = forms.ChoiceField(choices=RegistrationForm.ROLE_CHOICES, widget=forms.RadioSelect)
    username = forms.CharField(max_length=Users.USERNAME_MAX_LENGTH, widget=forms.TextInput(
        attrs={"placeholder": "username", "maxlength": Users.USERNAME_MAX_LENGTH}))
    region = forms.ChoiceField(
        choices=[("", "Выберите регион")] + REGION_CHOICES,
        widget=forms.Select(choices=[("", "Выберите регион")] + REGION_CHOICES),
    )
    motivation = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Необязательно, только для волонтеров"}),
    )

    I18N = {
        "region": REGION_FIELD_I18N,
        "motivation": {"ph": "form.optional_volunteers_ph"},
    }

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("label_suffix", "")
        super().__init__(*args, **kwargs)
        localize(self, self.I18N)

    clean_username = RegistrationForm.clean_username


class LoginForm(forms.Form):
    username = forms.CharField(widget=forms.TextInput(attrs={
        "placeholder": "Username",
        "data-i18n-ph": "field.username",
        "autocomplete": "username",
        "autofocus": True,
        "aria-describedby": "id_username_error",
    }))
    password = forms.CharField(widget=forms.PasswordInput(attrs={
        "placeholder": "Password",
        "data-i18n-ph": "field.password",
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        localize(self, {"email": {"label": "field.email", "ph": "form.email_ph"}})


class ResetPasswordForm(forms.Form):
    new_password = forms.CharField(label="Новый пароль", widget=forms.PasswordInput(attrs={"placeholder": "Новый пароль"}))
    confirm_password = forms.CharField(label="Повторите пароль", widget=forms.PasswordInput(attrs={"placeholder": "Повторите пароль"}))

    def __init__(self, *args, user=None, **kwargs):
        # The account being reset — passed so AUTH_PASSWORD_VALIDATORS'
        # UserAttributeSimilarityValidator can reject a password too close to
        # the username/email.
        self.user = user
        super().__init__(*args, **kwargs)
        localize(self, {
            "new_password": {"label": "form.new_password", "ph": "form.new_password"},
            "confirm_password": {"label": "form.password_repeat", "ph": "form.password_repeat"},
        })

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("new_password")
        if password != cleaned.get("confirm_password"):
            raise forms.ValidationError("Пароли не совпадают", code="password_mismatch")
        if password:
            # Same policy as registration — the reset path must not be a way
            # around AUTH_PASSWORD_VALIDATORS (min length, common-password and
            # numeric-only blocklists, similarity to account attributes).
            try:
                validate_password(password, self.user)
            except ValidationError as exc:
                raise forms.ValidationError(exc.error_list)
        return cleaned


class ProfileForm(forms.ModelForm):
    skills = forms.MultipleChoiceField(
        label="Навыки — чем могу помочь",
        choices=HELP_TYPE_CHOICES,
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Влияет на подбор задач в CRM.",
    )
    # Do-not-disturb (volunteers only). `availability_status` is NOT on this
    # form anymore — it is system-computed (myapp/services/presence.py).
    is_away = forms.BooleanField(
        label="Не беспокоить / в отпуске",
        required=False,
        help_text="Пока включено, вам не предлагают новые задачи и не присылают о них уведомления.",
    )
    # A calendar date — "away through that whole day"; stored as the start of
    # the next local day (presence._away_until_from_date).
    away_until = forms.DateField(
        label="До какой даты (включительно)",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        help_text="Необязательно. После этой даты режим выключится сам.",
    )
    # Volunteer community (milestone 5): may the weekly "thank you" post name me?
    community_shoutouts = forms.BooleanField(
        label="Упоминать меня в сообществе волонтёров",
        required=False,
        help_text="В еженедельном «Спасибо недели» в Telegram-группе волонтёров — рядом с новыми значками.",
    )
    # i18n.js keys (accounts/form_i18n.py), picked up by partials/_field.html.
    # localize() skips the volunteer-only fields a non-volunteer's form drops.
    I18N = {
        "full_name": {"label": "form.full_name", "ph": "form.full_name"},
        "age": {"label": "form.age"},
        "image": {"label": "form.profile_photo"},
        "bio": {"label": "form.bio", "ph": "form.bio_ph"},
        "skills": {"label": "form.skills", "help": "form.skills_help", "choices": "help."},
        "is_away": {"label": "profile.away_label", "help": "profile.away_help"},
        "away_until": {"label": "profile.away_until_label", "help": "profile.away_until_help"},
        "community_shoutouts": {"label": "profile.shoutouts_label", "help": "profile.shoutouts_help"},
    }

    class Meta:
        model = Profile
        fields = ["full_name", "age", "image", "bio", "skills", "is_away", "away_until", "community_shoutouts",
                  "latitude", "longitude"]
        labels = {
            "full_name": "Полное имя",
            "age": "Возраст",
            "image": "Фото профиля",
            "bio": "О себе",
        }
        widgets = {
            "full_name": forms.TextInput(attrs={"placeholder": "Полное имя", "maxlength": Users.FULL_NAME_MAX_LENGTH}),
            "age": forms.NumberInput(attrs={"min": 1, "max": 120}),
            "bio": forms.Textarea(attrs={"rows": 4, "placeholder": "О себе, навыки, удобное время"}),
            "latitude": forms.HiddenInput(),
            "longitude": forms.HiddenInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.initial.setdefault("skills", self.instance.skills or [])
        self.fields["full_name"].widget.attrs["maxlength"] = Users.FULL_NAME_MAX_LENGTH
        self._original_away = (self.instance.is_away, self.instance.away_until)
        if not self._is_volunteer():
            # Do-not-disturb only means something for volunteers (it removes
            # them from matching); for anyone else the fields are neither shown
            # nor accepted.
            del self.fields["is_away"]
            del self.fields["away_until"]
            del self.fields["community_shoutouts"]
        else:
            if self.instance.away_until:
                # Stored as "start of the day after" → show the inclusive last day.
                last_day = timezone.localtime(self.instance.away_until) - timezone.timedelta(microseconds=1)
                self.initial["away_until"] = last_day.date()
            self.fields["away_until"].widget.attrs["min"] = timezone.localdate().isoformat()
        localize(self, self.I18N)

    def _is_volunteer(self):
        user_id = getattr(self.instance, "user_id", None)
        return bool(user_id) and bool(getattr(self.instance.user, "is_volunteer", False))

    def clean_skills(self):
        return list(self.cleaned_data.get("skills") or [])

    def clean_away_until(self):
        from myapp.services.presence import _away_until_from_date

        value = self.cleaned_data.get("away_until")
        if value is None or not self.cleaned_data.get("is_away"):
            return None  # no end date, or switching do-not-disturb off resets it
        until = _away_until_from_date(value)
        if until <= timezone.now() or value < timezone.localdate():
            raise forms.ValidationError("Дата не может быть в прошлом.", code="away_until_past")
        return until

    def clean(self):
        # DecimalField(max_digits=9) alone allows any value up to ~1000,
        # which is meaningless (and, on the map, misleading) for a real-world
        # WGS84 latitude/longitude — enforce the actual coordinate range here.
        cleaned = super().clean()
        lat, lng = cleaned.get("latitude"), cleaned.get("longitude")
        if (lat is None) != (lng is None):
            raise forms.ValidationError("Укажите широту и долготу вместе или не указывайте вовсе.", code="coords_pair")
        if lat is not None and lng is not None and not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise forms.ValidationError("Некорректные координаты.", code="coords_invalid")
        if "is_away" in self.fields and not cleaned.get("is_away"):
            cleaned["away_until"] = None  # switching it off resets the end date
        return cleaned

    def save(self, commit=True):
        profile = super().save(commit=False)
        if "latitude" in self.changed_data or "longitude" in self.changed_data:
            profile.location_updated_at = timezone.now()
        if commit:
            profile.save()
            if "is_away" in self.fields and (profile.is_away, profile.away_until) != self._original_away:
                from myapp.services import presence

                # Staff see the do-not-disturb flag live (availability.changed).
                presence.publish_availability(profile)
        return profile


class UserUpdateForm(forms.ModelForm):
    class Meta:
        model = Users
        # telegram_id is managed separately via the verified Telegram-linking
        # flow (accounts.views.telegram_link_view), never edited free-form here.
        fields = ["username", "email", "region"]
        labels = {
            "username": "Имя пользователя",
            "email": "Email",
            "region": "Регион",
        }
        help_texts = {"username": "Используется для входа. После смены входите с новым именем."}
        widgets = {
            "username": forms.TextInput(attrs={"maxlength": Users.USERNAME_MAX_LENGTH, "autocomplete": "username"}),
            "email": forms.EmailInput(),
            "region": forms.Select(choices=[("", "Выберите регион")] + REGION_CHOICES),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The same limit as registration (RegistrationForm), not the wider column.
        self.fields["username"].max_length = Users.USERNAME_MAX_LENGTH
        self.fields["username"].widget.attrs["maxlength"] = Users.USERNAME_MAX_LENGTH
        # Not posted at all (older clients) = unchanged; posted empty = "required".
        self.fields["username"].required = False
        self.fields["username"].widget.attrs["required"] = True
        localize(self, {
            "username": {"label": "field.username", "help": "edit.username_hint"},
            "email": {"label": "field.email"},
            "region": REGION_FIELD_I18N,
        })

    def clean_username(self):
        """The registration rules (RegistrationForm.clean_username: trimmed,
        4–30 characters), but unique **ignoring case** among other accounts.
        An unchanged username passes as it is — a legacy name outside today's
        rules must not block saving the email / region."""
        if "username" not in self.data:
            return self.initial["username"]
        username = (self.cleaned_data.get("username") or "").strip()
        if username == self.initial["username"]:
            return username
        if not username:
            raise forms.ValidationError(self.fields["username"].error_messages["required"], code="required")
        if len(username) < 4:
            raise forms.ValidationError("Username должен быть минимум 4 символа", code="username_too_short")
        if Users.objects.filter(username__iexact=username).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Такой username уже занят", code="username_taken")
        return username

    def _get_validation_exclusions(self):
        # An unchanged username skips the model checks too (its 30-character
        # validator would reject a legacy longer name the user didn't touch).
        exclude = super()._get_validation_exclusions()
        if not self.username_changed:
            exclude.add("username")
        return exclude

    @property
    def username_changed(self):
        cleaned = getattr(self, "cleaned_data", {})
        return "username" in cleaned and cleaned["username"] != self.initial["username"]

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if Users.objects.filter(email=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Email уже используется", code="email_taken")
        return email
