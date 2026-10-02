"""Live volunteer location during an active task (Feature Pack v2, feature 7).

Rules (myapp/services/tracking.py enforces them): points are accepted only from
the assigned volunteer of an *active* task after their consent; only the task's
client and staff (and the volunteer) see them; the trail is deleted when the
task ends and never kept longer than 24 h (`purge_tracking`). The latest
position also lives on the task (`HelpRequest.volunteer_last_*`) so readers
never scan the trail.
"""

from django.db import models

from accounts.models import Users

from .help_requests import HelpRequest

TRACKING_SOURCE_CHOICES = [("web", "Web"), ("telegram", "Telegram")]


class TaskLocationPoint(models.Model):
    task = models.ForeignKey(HelpRequest, on_delete=models.CASCADE, related_name="location_points")
    volunteer = models.ForeignKey(Users, on_delete=models.CASCADE, related_name="+")
    latitude = models.DecimalField(max_digits=9, decimal_places=6)
    longitude = models.DecimalField(max_digits=9, decimal_places=6)
    accuracy_m = models.FloatField(null=True, blank=True)
    speed_mps = models.FloatField(null=True, blank=True)
    heading = models.FloatField(null=True, blank=True)
    source = models.CharField(max_length=10, choices=TRACKING_SOURCE_CHOICES, default="web")
    recorded_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)  # purge_tracking

    class Meta:
        verbose_name = "Точка маршрута"
        verbose_name_plural = "Точки маршрута"
        ordering = ["recorded_at"]
        indexes = [models.Index(fields=["task", "-recorded_at"], name="trackpt_task_recorded_idx")]

    def __str__(self):
        return f"#{self.task_id} @ {self.recorded_at:%H:%M:%S}"
