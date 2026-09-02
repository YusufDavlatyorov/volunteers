from django.db import models

from accounts.models import REGION_CHOICES, Users, validate_file_size

from .events import Event
from .help_requests import HelpRequest


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
