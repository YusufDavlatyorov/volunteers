from datetime import timedelta

from django.db import models, transaction
from django.utils import timezone

from accounts.models import REGION_CHOICES, Users, validate_file_size


# Single source of truth for the "task in progress too long" threshold, reused by
# check_overdue_view, the check_overdue_tasks management command, and this model's
# is_overdue property so the number never drifts between them.
OVERDUE_THRESHOLD = timedelta(hours=3)


HELP_TYPE_CHOICES = [
    ("medical", "Медицинская помощь"),
    ("grocery", "Продукты и покупки"),
    ("transport", "Транспорт"),
    ("household", "Домашние дела"),
    ("emotional", "Общение и поддержка"),
    ("documents", "Документы"),
    ("other", "Другое"),
]

STATUS_CHOICES = [
    ("pending", "Свободный"),
    ("active", "В работе"),
    ("completed", "Завершен"),
    ("cancelled", "Отменен"),
]

WORK_STAGE_ASSIGNED = "assigned"
WORK_STAGE_EN_ROUTE = "en_route"
WORK_STAGE_ARRIVED = "arrived"
WORK_STAGE_IN_PROGRESS = "in_progress"
WORK_STAGE_CHOICES = [
    (WORK_STAGE_ASSIGNED, "Назначен"),
    (WORK_STAGE_EN_ROUTE, "В пути"),
    (WORK_STAGE_ARRIVED, "На месте"),
    (WORK_STAGE_IN_PROGRESS, "Выполняется"),
]
# Forward-only order for advance_work_stage(). Only meaningful while status == "active".
WORK_STAGE_ORDER = [WORK_STAGE_ASSIGNED, WORK_STAGE_EN_ROUTE, WORK_STAGE_ARRIVED, WORK_STAGE_IN_PROGRESS]

PRIORITY_NORMAL = "normal"
PRIORITY_HIGH = "high"
PRIORITY_EMERGENCY = "emergency"
PRIORITY_CHOICES = [
    (PRIORITY_NORMAL, "Обычный"),
    (PRIORITY_HIGH, "Высокий"),
    (PRIORITY_EMERGENCY, "Экстренный"),
]

EVENT_REGION_CHOICES = REGION_CHOICES + [("all", "Все регионы")]


class HelpRequest(models.Model):
    PRIORITY_NORMAL = PRIORITY_NORMAL
    PRIORITY_HIGH = PRIORITY_HIGH
    PRIORITY_EMERGENCY = PRIORITY_EMERGENCY
    WORK_STAGE_ASSIGNED = WORK_STAGE_ASSIGNED
    WORK_STAGE_EN_ROUTE = WORK_STAGE_EN_ROUTE
    WORK_STAGE_ARRIVED = WORK_STAGE_ARRIVED
    WORK_STAGE_IN_PROGRESS = WORK_STAGE_IN_PROGRESS
    WORK_STAGE_ORDER = WORK_STAGE_ORDER

    client = models.ForeignKey(
        Users,
        on_delete=models.CASCADE,
        related_name="client_requests",
        limit_choices_to={"is_client": True},
    )
    volunteer = models.ForeignKey(
        Users,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="volunteer_tasks",
        limit_choices_to={"is_volunteer": True},
    )
    help_type = models.CharField(max_length=50, choices=HELP_TYPE_CHOICES)
    description = models.TextField()
    address = models.CharField(max_length=255)
    phone = models.CharField(max_length=30)
    region = models.CharField(max_length=100, choices=REGION_CHOICES, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    # `is_urgent` is the legacy boolean, kept in sync from `priority` by
    # HelpRequestForm so existing `.filter(is_urgent=...)` call sites keep working.
    # `priority` is the graded value new code (map markers, dispatch, emergency
    # queue) reads.
    is_urgent = models.BooleanField(default=False)
    priority = models.CharField(
        max_length=20, choices=PRIORITY_CHOICES, default=PRIORITY_NORMAL, db_index=True
    )
    # The volunteer's on-the-ground progress. Only meaningful while status == "active";
    # the coarse lifecycle (status) is unchanged. Advanced via HelpRequest.advance_work_stage().
    work_stage = models.CharField(
        max_length=20, choices=WORK_STAGE_CHOICES, default=WORK_STAGE_ASSIGNED
    )
    alarm_sent = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Запрос помощи"
        verbose_name_plural = "Запросы помощи"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.get_help_type_display()} для {self.client.username}"

    def accept(self, volunteer):
        self.volunteer = volunteer
        self.status = "active"
        self.work_stage = WORK_STAGE_ASSIGNED
        self.accepted_at = timezone.now()
        self.save()

    def complete(self):
        self.status = "completed"
        self.completed_at = timezone.now()
        self.save()

    def advance_work_stage(self, target):
        """Move the volunteer's progress forward to `target`. Forward-only, and
        only while the task is active. Raises ValueError otherwise."""
        if self.status != "active":
            raise ValueError("work_stage only applies to an active task")
        order = WORK_STAGE_ORDER
        if target not in order:
            raise ValueError(f"unknown work stage: {target!r}")
        if order.index(target) <= order.index(self.work_stage):
            raise ValueError("work stage can only move forward")
        self.work_stage = target
        self.save(update_fields=["work_stage", "updated_at"])

    @property
    def has_location(self):
        return self.latitude is not None and self.longitude is not None

    @property
    def is_overdue(self):
        return self.status == "active" and bool(self.accepted_at) and timezone.now() - self.accepted_at > OVERDUE_THRESHOLD


class Event(models.Model):
    curator = models.ForeignKey(
        Users,
        on_delete=models.CASCADE,
        related_name="events",
        limit_choices_to={"is_curator": True},
    )
    title = models.CharField(max_length=255)
    description = models.TextField()
    region = models.CharField(max_length=100, choices=EVENT_REGION_CHOICES, default="all")
    date = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    notifications_sent = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Акция"
        verbose_name_plural = "Акции"
        ordering = ["-date"]

    def __str__(self):
        return self.title


class Broadcast(models.Model):
    sender = models.ForeignKey(Users, on_delete=models.CASCADE, related_name="broadcasts")
    subject = models.CharField(max_length=255)
    message = models.TextField()
    region = models.CharField(max_length=100, choices=EVENT_REGION_CHOICES, default="all")
    created_at = models.DateTimeField(auto_now_add=True)
    sent_count = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Рассылка"
        verbose_name_plural = "Рассылки"
        ordering = ["-created_at"]

    def __str__(self):
        return self.subject


class VolunteerApplication(models.Model):
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_PENDING, "На рассмотрении"),
        (STATUS_APPROVED, "Одобрена"),
        (STATUS_REJECTED, "Отклонена"),
    ]

    user = models.OneToOneField(Users, on_delete=models.CASCADE, related_name="volunteer_application")
    region = models.CharField(max_length=100, choices=REGION_CHOICES, blank=True)
    reason = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        Users,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_volunteer_applications",
    )

    class Meta:
        verbose_name = "Заявка волонтера"
        verbose_name_plural = "Заявки волонтеров"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user.username} — {self.get_status_display()}"

    def approve(self, reviewer):
        with transaction.atomic():
            user = Users.objects.get(pk=self.user_id)
            user.is_volunteer = True
            user.is_client = False
            user.save()
            self.status = self.STATUS_APPROVED
            self.reviewed_by = reviewer
            self.reviewed_at = timezone.now()
            self.save(update_fields=["status", "reviewed_by", "reviewed_at"])

    def reject(self, reviewer):
        with transaction.atomic():
            user = Users.objects.get(pk=self.user_id)
            user.is_volunteer = False
            user.save()
            self.status = self.STATUS_REJECTED
            self.reviewed_by = reviewer
            self.reviewed_at = timezone.now()
            self.save(update_fields=["status", "reviewed_by", "reviewed_at"])

    def reapply(self):
        self.status = self.STATUS_PENDING
        self.reviewed_by = None
        self.reviewed_at = None
        # auto_now_add only stamps created_at on the initial INSERT, so it is
        # safe to set it manually here to reflect the new submission time.
        self.created_at = timezone.now()
        self.save(update_fields=["status", "reviewed_by", "reviewed_at", "created_at"])


class PhotoReport(models.Model):
    author = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True, related_name="photo_reports")
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to="reports/", validators=[validate_file_size])
    region = models.CharField(max_length=100, choices=REGION_CHOICES, blank=True)
    event = models.ForeignKey(Event, on_delete=models.SET_NULL, null=True, blank=True, related_name="photo_reports")
    help_request = models.ForeignKey(
        HelpRequest,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="photo_reports",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Фотоотчет"
        verbose_name_plural = "Фотоотчеты"
        ordering = ["-created_at"]

    def __str__(self):
        return self.title
