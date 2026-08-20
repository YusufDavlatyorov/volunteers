from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from .models import REGION_CHOICES, Profile, Users


class RegistrationForm(forms.ModelForm):
    ROLE_CHOICES = [
        ("volunteer", "Я волонтер"),
        ("client", "Мне нужна помощь"),
    ]

    role = forms.ChoiceField(choices=ROLE_CHOICES, widget=forms.RadioSelect)
    password = forms.CharField(widget=forms.PasswordInput(attrs={"placeholder": "Минимум 8 символов"}))
    confirm_password = forms.CharField(widget=forms.PasswordInput(attrs={"placeholder": "Повторите пароль"}))

    class Meta:
        model = Users
        fields = ["username", "email", "region", "telegram_id"]
        widgets = {
            "username": forms.TextInput(attrs={"placeholder": "username"}),
            "email": forms.EmailInput(attrs={"placeholder": "you@example.com"}),
            "region": forms.Select(choices=[("", "Выберите регион")] + REGION_CHOICES),
            "telegram_id": forms.NumberInput(attrs={"placeholder": "Telegram chat id, можно позже"}),
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
        user.is_volunteer = self.cleaned_data["role"] == "volunteer"
        user.is_client = self.cleaned_data["role"] == "client"
        if commit:
            user.save()
        return user


class LoginForm(forms.Form):
    username = forms.CharField(widget=forms.TextInput(attrs={"placeholder": "Username"}))
    password = forms.CharField(widget=forms.PasswordInput(attrs={"placeholder": "Пароль"}))


class ForgotPasswordForm(forms.Form):
    email = forms.EmailField(widget=forms.EmailInput(attrs={"placeholder": "Ваш email"}))


class ResetPasswordForm(forms.Form):
    new_password = forms.CharField(widget=forms.PasswordInput(attrs={"placeholder": "Новый пароль"}))
    confirm_password = forms.CharField(widget=forms.PasswordInput(attrs={"placeholder": "Повторите пароль"}))

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("new_password")
        if password != cleaned.get("confirm_password"):
            raise forms.ValidationError("Пароли не совпадают")
        if password and len(password) < 8:
            raise forms.ValidationError("Пароль слишком короткий")
        return cleaned


class ProfileForm(forms.ModelForm):
    class Meta:
        model = Profile
        fields = ["full_name", "age", "image", "bio"]
        widgets = {
            "full_name": forms.TextInput(attrs={"placeholder": "Полное имя"}),
            "age": forms.NumberInput(attrs={"min": 1, "max": 120}),
            "bio": forms.Textarea(attrs={"rows": 4, "placeholder": "О себе, навыки, удобное время"}),
        }


class UserUpdateForm(forms.ModelForm):
    class Meta:
        model = Users
        fields = ["email", "region", "telegram_id"]
        widgets = {
            "email": forms.EmailInput(),
            "region": forms.Select(choices=[("", "Выберите регион")] + REGION_CHOICES),
            "telegram_id": forms.NumberInput(attrs={"placeholder": "Telegram chat id"}),
        }

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if Users.objects.filter(email=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Email уже используется")
        return email
