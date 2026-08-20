import uuid
from datetime import timedelta

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone


REGION_CHOICES = [
    ("dushanbe", "Душанбе"),
    ("sogd", "Согд"),
    ("khatlon", "Хатлон"),
    ("gbao", "ГБАО"),
    ("rrp", "РРП"),
]


class UsersManager(BaseUserManager):
    def create_user(self, username, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Email обязателен")
        if not username:
            raise ValueError("Username обязателен")

        user = self.model(
            username=username.strip(),
            email=self.normalize_email(email),
            **extra_fields,
        )
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, username, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)
        extra_fields.setdefault("is_email_verified", True)
        return self.create_user(username, email, password, **extra_fields)


class Users(AbstractBaseUser, PermissionsMixin):
    ROLE_ADMIN = "admin"
    ROLE_CURATOR = "curator"
    ROLE_VOLUNTEER = "volunteer"
    ROLE_CLIENT = "client"

    username = models.CharField(max_length=150, unique=True)
    email = models.EmailField(unique=True)

    is_email_verified = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    is_curator = models.BooleanField(default=False)
    is_volunteer = models.BooleanField(default=False)
    is_client = models.BooleanField(default=False)

    telegram_id = models.BigIntegerField(unique=True, null=True, blank=True)
    region = models.CharField(max_length=100, choices=REGION_CHOICES, blank=True)

    email_verification_token = models.CharField(max_length=100, null=True, blank=True)
    email_verification_token_created_at = models.DateTimeField(null=True, blank=True)
    reset_password_token = models.CharField(max_length=100, null=True, blank=True)
    reset_password_token_created_at = models.DateTimeField(null=True, blank=True)
    date_joined = models.DateTimeField(auto_now_add=True)

    objects = UsersManager()

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS = ["email"]

    class Meta:
        verbose_name = "Пользователь"
        verbose_name_plural = "Пользователи"

    @property
    def role(self):
        if self.is_superuser:
            return self.ROLE_ADMIN
        if self.is_curator:
            return self.ROLE_CURATOR
        if self.is_volunteer:
            return self.ROLE_VOLUNTEER
        if self.is_client:
            return self.ROLE_CLIENT
        return "guest"

    @property
    def role_display(self):
        return {
            self.ROLE_ADMIN: "Админ",
            self.ROLE_CURATOR: "Куратор",
            self.ROLE_VOLUNTEER: "Волонтер",
            self.ROLE_CLIENT: "Клиент",
        }.get(self.role, "Гость")

    def clean(self):
        super().clean()
        roles = [self.is_curator, self.is_volunteer, self.is_client]
        if sum(bool(role) for role in roles) > 1:
            from django.core.exceptions import ValidationError

            raise ValidationError("У пользователя может быть только одна роль")

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.username} ({self.role_display})"

    def generate_email_verification_token(self):
        self.email_verification_token = str(uuid.uuid4())
        self.email_verification_token_created_at = timezone.now()
        self.save(update_fields=["email_verification_token", "email_verification_token_created_at"])
        return self.email_verification_token

    def email_verification_token_is_valid(self):
        if not self.email_verification_token_created_at:
            return False
        return timezone.now() < self.email_verification_token_created_at + timedelta(hours=24)

    def confirm_email(self):
        self.is_email_verified = True
        self.email_verification_token = None
        self.email_verification_token_created_at = None
        self.save()

    def generate_reset_password_token(self):
        self.reset_password_token = str(uuid.uuid4())
        self.reset_password_token_created_at = timezone.now()
        self.save(update_fields=["reset_password_token", "reset_password_token_created_at"])
        return self.reset_password_token

    def reset_password_token_is_valid(self):
        if not self.reset_password_token_created_at:
            return False
        return timezone.now() < self.reset_password_token_created_at + timedelta(hours=1)

    def clear_reset_password_token(self):
        self.reset_password_token = None
        self.reset_password_token_created_at = None
        self.save(update_fields=["reset_password_token", "reset_password_token_created_at"])


class Profile(models.Model):
    user = models.OneToOneField(Users, on_delete=models.CASCADE, related_name="profile")
    full_name = models.CharField(max_length=255, blank=True)
    age = models.PositiveIntegerField(null=True, blank=True)
    image = models.ImageField(upload_to="avatars/", blank=True)
    bio = models.TextField(blank=True)
    rating = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Профиль"
        verbose_name_plural = "Профили"

    def __str__(self):
        return f"Профиль {self.user.username}"

    def add_points(self, points=3):
        self.rating += points
        self.save(update_fields=["rating", "updated_at"])
