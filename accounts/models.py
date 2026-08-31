import hashlib
import secrets
from datetime import timedelta

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


MAX_UPLOAD_SIZE_MB = 5
MAX_UPLOAD_SIZE_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024


def validate_file_size(value):
    """Shared upload-size cap for avatar/photo-report images — an
    unrestricted ImageField accepts arbitrarily large files, which is an easy
    disk-exhaustion / slow-upload denial-of-service vector on MEDIA_ROOT."""
    if value.size > MAX_UPLOAD_SIZE_BYTES:
        raise ValidationError(f"Файл слишком большой. Максимальный размер: {MAX_UPLOAD_SIZE_MB} МБ.")


def hash_token(raw_token):
    """One-way hash for reset/verification tokens at rest.

    Only this hash is stored in the DB, so reading the database (a backup
    leak, a SQL-injection elsewhere, etc.) doesn't hand out working
    password-reset or email-confirmation links — the raw token that goes into
    the emailed URL never touches storage. SHA-256 (not a slow password
    hash) is appropriate here because the input is a high-entropy random
    token, not a low-entropy guessable secret like a password.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


# A Telegram-linking code is a one-time credential typed into the bot chat, so
# it must live long enough to switch apps and copy/paste but no longer.
TELEGRAM_LINK_TOKEN_TTL = timedelta(minutes=10)


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
    # Only the hash of the current Telegram-linking code is kept at rest, same
    # as the reset/verification tokens above. The raw code is shown once in the
    # web UI and then redeemed via the bot (see myapp/services/telegram_link.py).
    telegram_link_token = models.CharField(max_length=100, null=True, blank=True)
    telegram_link_token_created_at = models.DateTimeField(null=True, blank=True)
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
        raw_token = secrets.token_urlsafe(32)
        self.email_verification_token = hash_token(raw_token)
        self.email_verification_token_created_at = timezone.now()
        self.save(update_fields=["email_verification_token", "email_verification_token_created_at"])
        return raw_token

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
        raw_token = secrets.token_urlsafe(32)
        self.reset_password_token = hash_token(raw_token)
        self.reset_password_token_created_at = timezone.now()
        self.save(update_fields=["reset_password_token", "reset_password_token_created_at"])
        return raw_token

    def reset_password_token_is_valid(self):
        if not self.reset_password_token_created_at:
            return False
        return timezone.now() < self.reset_password_token_created_at + timedelta(hours=1)

    def clear_reset_password_token(self):
        self.reset_password_token = None
        self.reset_password_token_created_at = None
        self.save(update_fields=["reset_password_token", "reset_password_token_created_at"])

    def generate_telegram_link_token(self):
        """Mint a fresh one-time code for binding a Telegram chat to this
        account. Returns the raw code (shown once in the UI); only its hash is
        stored, and minting a new code invalidates any previous one."""
        raw_token = secrets.token_urlsafe(32)
        self.telegram_link_token = hash_token(raw_token)
        self.telegram_link_token_created_at = timezone.now()
        self.save(update_fields=["telegram_link_token", "telegram_link_token_created_at"])
        return raw_token

    def telegram_link_token_is_valid(self):
        if not self.telegram_link_token or not self.telegram_link_token_created_at:
            return False
        return timezone.now() < self.telegram_link_token_created_at + TELEGRAM_LINK_TOKEN_TTL

    def clear_telegram_link_token(self):
        self.telegram_link_token = None
        self.telegram_link_token_created_at = None
        self.save(update_fields=["telegram_link_token", "telegram_link_token_created_at"])


class Profile(models.Model):
    AVAILABILITY_AVAILABLE = "available"
    AVAILABILITY_BUSY = "busy"
    AVAILABILITY_OFFLINE = "offline"
    AVAILABILITY_CHOICES = [
        (AVAILABILITY_AVAILABLE, "Доступен"),
        (AVAILABILITY_BUSY, "Занят"),
        (AVAILABILITY_OFFLINE, "Не в сети"),
    ]

    user = models.OneToOneField(Users, on_delete=models.CASCADE, related_name="profile")
    full_name = models.CharField(max_length=255, blank=True)
    age = models.PositiveIntegerField(null=True, blank=True)
    image = models.ImageField(upload_to="avatars/", blank=True, validators=[validate_file_size])
    bio = models.TextField(blank=True)
    rating = models.PositiveIntegerField(default=0)

    # Current/home location (volunteers and clients). Populated via a map picker
    # on the edit-profile form or the browser geolocation API; both are optional
    # so existing accounts keep working without a location set.
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    location_updated_at = models.DateTimeField(null=True, blank=True)

    # Volunteer availability for task matching/dispatch. Meaningless for clients
    # but kept on the shared Profile model rather than a volunteer-only table to
    # avoid a second 1:1 model for what is otherwise identical "extra user data".
    availability_status = models.CharField(
        max_length=20, choices=AVAILABILITY_CHOICES, default=AVAILABILITY_AVAILABLE
    )
    # Help types a volunteer is comfortable with — feeds the matching score
    # (myapp.services.matching). A list of HELP_TYPE_CHOICES keys; empty = "no
    # info", treated neutrally by the scorer, never as a penalty.
    skills = models.JSONField(default=list, blank=True)

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

    def has_skill(self, help_type):
        return help_type in (self.skills or [])

    @property
    def has_location(self):
        return self.latitude is not None and self.longitude is not None

    def set_location(self, latitude, longitude):
        self.latitude = latitude
        self.longitude = longitude
        self.location_updated_at = timezone.now()
        self.save(update_fields=["latitude", "longitude", "location_updated_at", "updated_at"])
