"""In-app notifications (Feature Pack v2, feature 11).

``Notification`` is one row per recipient per event; its visible text is NOT
stored — only i18n keys (``title_key`` / ``body_key``) plus ``params``, which
the browser renders through ``static/js/i18n.js`` in the viewer's current
language. ``params`` is published over the WebSocket, so it must never carry a
phone number or an email address (``myapp/services/notify.py`` strips them).

``NotificationPreference`` stores *overrides only*: the defaults per type live
in code (``myapp/services/notify.py::NOTIFICATION_TYPES``). Creation and
fan-out go through ``notify()`` — never through model signals.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q


class Notification(models.Model):
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    # A NOTIFICATION_TYPES key. Deliberately no `choices=`: adding a type in
    # code must not need a migration.
    type = models.CharField(max_length=40)
    title_key = models.CharField(max_length=80)
    body_key = models.CharField(max_length=80)
    params = models.JSONField(default=dict, blank=True)
    # Internal relative URL ("/myapp/tasks/12/") or "".
    link = models.CharField(max_length=300, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    # Idempotency for retried sends (e.g. "sos:<report_id>"); unique per
    # recipient when set.
    dedupe_key = models.CharField(max_length=120, null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["recipient", "read_at", "-created_at"], name="notif_recipient_unread_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["recipient", "dedupe_key"],
                condition=Q(dedupe_key__isnull=False),
                name="notif_unique_recipient_dedupe",
            ),
        ]

    def __str__(self):
        return f"Notification #{self.pk} {self.type} → user #{self.recipient_id}"

    @property
    def is_read(self):
        return self.read_at is not None


class NotificationPreference(models.Model):
    """One user's override for one (type, channel) pair. Absent row = the
    type's default. ``in_app`` is never stored (it cannot be disabled)."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notification_preferences"
    )
    type = models.CharField(max_length=40)
    channel = models.CharField(max_length=20)
    enabled = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "type", "channel"], name="notifpref_unique_user_type_channel"),
        ]

    def __str__(self):
        return f"user #{self.user_id} {self.type}/{self.channel}={'on' if self.enabled else 'off'}"
