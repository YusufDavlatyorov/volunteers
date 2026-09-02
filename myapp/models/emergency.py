"""Volunteer-raised SOS / danger reports.

A volunteer working an *active* HelpRequest can report that something is wrong
or unsafe. The report is always tied to one HelpRequest (and, through it, one
client). Domain state transitions are model methods here; the cross-cutting
work — deduplication, location resolution, notification fan-out — lives in
``myapp.services.emergency``.
"""

from django.db import models
from django.utils import timezone

from accounts.models import REGION_CHOICES, Users

from .help_requests import HelpRequest

STATUS_OPEN = "open"
STATUS_ACKNOWLEDGED = "acknowledged"
STATUS_RESOLVED = "resolved"
STATUS_CANCELLED = "cancelled"
STATUS_CHOICES = [
    (STATUS_OPEN, "Открыт"),
    (STATUS_ACKNOWLEDGED, "Принят"),
    (STATUS_RESOLVED, "Решён"),
    (STATUS_CANCELLED, "Отменён"),
]

# Statuses that still need staff attention.
OPEN_STATUSES = (STATUS_OPEN, STATUS_ACKNOWLEDGED)

# Forward-only. resolved / cancelled are terminal.
ALLOWED_TRANSITIONS = {
    STATUS_OPEN: {STATUS_ACKNOWLEDGED, STATUS_RESOLVED, STATUS_CANCELLED},
    STATUS_ACKNOWLEDGED: {STATUS_RESOLVED, STATUS_CANCELLED},
    STATUS_RESOLVED: set(),
    STATUS_CANCELLED: set(),
}


class EmergencyReport(models.Model):
    STATUS_OPEN = STATUS_OPEN
    STATUS_ACKNOWLEDGED = STATUS_ACKNOWLEDGED
    STATUS_RESOLVED = STATUS_RESOLVED
    STATUS_CANCELLED = STATUS_CANCELLED
    OPEN_STATUSES = OPEN_STATUSES

    help_request = models.ForeignKey(
        HelpRequest, on_delete=models.CASCADE, related_name="emergency_reports"
    )
    volunteer = models.ForeignKey(
        Users,
        on_delete=models.CASCADE,
        related_name="emergency_reports",
        limit_choices_to={"is_volunteer": True},
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN, db_index=True)
    reason = models.TextField(blank=True)
    # Snapshot of the reporting context at creation time, so CRM region filtering
    # is stable even if the volunteer or task later changes region.
    region = models.CharField(max_length=100, choices=REGION_CHOICES, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(
        Users, on_delete=models.SET_NULL, null=True, blank=True, related_name="emergencies_acknowledged"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        Users, on_delete=models.SET_NULL, null=True, blank=True, related_name="emergencies_resolved"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        Users, on_delete=models.SET_NULL, null=True, blank=True, related_name="emergencies_cancelled"
    )
    resolution_note = models.TextField(blank=True)
    # Idempotency guard for the staff fan-out — claimed with a conditional
    # UPDATE ... WHERE notified_at IS NULL, the same pattern as HelpRequest.alarm_sent.
    notified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Сигнал опасности"
        verbose_name_plural = "Сигналы опасности"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "-created_at"])]

    def __str__(self):
        return f"SOS #{self.pk} — {self.get_status_display()} (запрос #{self.help_request_id})"

    @property
    def client(self):
        return self.help_request.client

    @property
    def is_open(self):
        return self.status in OPEN_STATUSES

    @property
    def has_location(self):
        return self.latitude is not None and self.longitude is not None

    def can_transition_to(self, target):
        return target in ALLOWED_TRANSITIONS.get(self.status, set())

    def _apply_transition(self, target, actor, at_field, by_field, note):
        if not self.can_transition_to(target):
            raise ValueError(f"emergency #{self.pk}: {self.status} -> {target} is not allowed")
        self.status = target
        setattr(self, at_field, timezone.now())
        setattr(self, by_field, actor)
        fields = ["status", at_field, by_field, "updated_at"]
        if note:
            self.resolution_note = note
            fields.append("resolution_note")
        self.save(update_fields=fields)

    def acknowledge(self, actor):
        self._apply_transition(STATUS_ACKNOWLEDGED, actor, "acknowledged_at", "acknowledged_by", "")

    def resolve(self, actor, note=""):
        self._apply_transition(STATUS_RESOLVED, actor, "resolved_at", "resolved_by", note)

    def cancel(self, actor, note=""):
        self._apply_transition(STATUS_CANCELLED, actor, "cancelled_at", "cancelled_by", note)
