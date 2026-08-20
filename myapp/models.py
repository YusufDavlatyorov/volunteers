from django.db import models
from django.utils import timezone

from accounts.models import REGION_CHOICES, Users


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

EVENT_REGION_CHOICES = REGION_CHOICES + [("all", "Все регионы")]


class HelpRequest(models.Model):
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
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    is_urgent = models.BooleanField(default=False)
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
        self.accepted_at = timezone.now()
        self.save()

    def complete(self):
        self.status = "completed"
        self.completed_at = timezone.now()
        self.save()


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


class PhotoReport(models.Model):
    author = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True, related_name="photo_reports")
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to="reports/")
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
