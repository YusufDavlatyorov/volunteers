"""Client <-> volunteer chat, one per HelpRequest (Feature Pack v2, feature 8).

Rules live in myapp/services/chat.py (the one write path): the client and the
assigned volunteer write while the task is ``active``; staff read always and
post as "Куратор" (``is_staff_post``); read-only once the task ends; archived
(hidden from lists, kept for staff) after ``CHAT_ARCHIVE_DAYS``. Every
delivery is re-checked against the reader's rights (WS delivery guards).
"""

from django.core.validators import MaxLengthValidator
from django.db import models
from django.db.models import Q

from accounts.models import Users, validate_file_size

from .help_requests import HelpRequest

CHAT_MESSAGE_MAX_LENGTH = 1000
CHAT_KIND_TEXT = "text"
CHAT_KIND_IMAGE = "image"
CHAT_KIND_SYSTEM = "system"
CHAT_KIND_CHOICES = [(CHAT_KIND_TEXT, "Текст"), (CHAT_KIND_IMAGE, "Фото"), (CHAT_KIND_SYSTEM, "Системное")]


class ChatMessage(models.Model):
    task = models.ForeignKey(HelpRequest, on_delete=models.CASCADE, related_name="chat_messages")
    # Null for system messages ("chat closed", "request cancelled").
    sender = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    kind = models.CharField(max_length=10, choices=CHAT_KIND_CHOICES, default=CHAT_KIND_TEXT)
    body = models.TextField(blank=True, validators=[MaxLengthValidator(CHAT_MESSAGE_MAX_LENGTH)])
    image = models.ImageField(upload_to="chat/%Y/%m/", blank=True, validators=[validate_file_size])
    # {"thumb": "<storage path>", ...} — WebP variants made in the background.
    image_variants = models.JSONField(default=dict, blank=True)
    is_staff_post = models.BooleanField(default=False)  # shown as "Куратор"
    # Client-generated id: a retried send (WS reconnect, double tap) is stored once.
    client_msg_id = models.CharField(max_length=64, blank=True)
    hidden_at = models.DateTimeField(null=True, blank=True)  # staff moderation
    hidden_by = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Сообщение чата"
        verbose_name_plural = "Сообщения чата"
        ordering = ["id"]
        indexes = [models.Index(fields=["task", "id"], name="chatmsg_task_id_idx")]
        constraints = [
            models.UniqueConstraint(
                fields=["task", "sender", "client_msg_id"],
                condition=~Q(client_msg_id=""),
                name="uniq_chatmsg_client_msg_id",
            )
        ]

    def __str__(self):
        return f"#{self.task_id}/{self.pk}"


class ChatReadState(models.Model):
    """Per (task, user) read cursor — unread badges and read receipts."""

    task = models.ForeignKey(HelpRequest, on_delete=models.CASCADE, related_name="+")
    user = models.ForeignKey(Users, on_delete=models.CASCADE, related_name="+")
    last_read_id = models.PositiveBigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["task", "user"], name="uniq_chat_read_state")]


class ChatReport(models.Model):
    """A party reports a message to staff. One report per (message, reporter)."""

    message = models.ForeignKey(ChatMessage, on_delete=models.CASCADE, related_name="reports")
    reporter = models.ForeignKey(Users, on_delete=models.CASCADE, related_name="+")
    reason = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    handled_at = models.DateTimeField(null=True, blank=True)
    handled_by = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        verbose_name = "Жалоба на сообщение"
        verbose_name_plural = "Жалобы на сообщения"
        constraints = [models.UniqueConstraint(fields=["message", "reporter"], name="uniq_chat_report")]
