from django.db import models

from accounts.models import REGION_CHOICES, Users


EVENT_REGION_CHOICES = REGION_CHOICES + [("all", "Все регионы")]


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
