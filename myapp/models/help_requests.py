from datetime import timedelta

from django.db import models
from django.utils import timezone

from accounts.models import REGION_CHOICES, Users


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
