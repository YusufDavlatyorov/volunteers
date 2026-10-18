"""Photo reports — what a volunteer (or staff) publishes after helping.

An admin can also add one from the CRM (``/myapp/crm/photo-reports/new/``)
for any task: on behalf of a volunteer (the task's by default) or as
themselves; ``uploaded_by`` records who really uploaded it.

Every report is reviewed by a curator / admin in the CRM
(``/myapp/crm/photo-reports/``): ``pending → approved | rejected``. Only an
approved report reaches the public pages (landing page, the reports list), and
only an approved report on a task releases that task's reward (rating points +
achievements, ``services/photo_reports.credit_task``). A rejected report is
final — the volunteer uploads a new one.
"""

from django.db import models
from django.utils import timezone

from accounts.models import REGION_CHOICES, Users, validate_file_size

from .events import Event
from .help_requests import HelpRequest


PHOTO_REPORT_PENDING = "pending"
PHOTO_REPORT_APPROVED = "approved"
PHOTO_REPORT_REJECTED = "rejected"
PHOTO_REPORT_STATUS_CHOICES = [
    (PHOTO_REPORT_PENDING, "На проверке"),
    (PHOTO_REPORT_APPROVED, "Одобрен"),
    (PHOTO_REPORT_REJECTED, "Отклонён"),
]
REJECTION_REASON_MAX_LENGTH = 500


class PhotoReport(models.Model):
    PENDING = PHOTO_REPORT_PENDING
    APPROVED = PHOTO_REPORT_APPROVED
    REJECTED = PHOTO_REPORT_REJECTED

    # ``author`` is whom the report is for (the reward rule reads it);
    # ``uploaded_by`` is who actually uploaded it — the author themselves, or
    # an admin adding it from the CRM on a volunteer's behalf (or as
    # themselves when the task has no volunteer). Null on reports uploaded
    # before 2026-10-18 (migration 0029).
    author = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True, related_name="photo_reports")
    uploaded_by = models.ForeignKey(
        Users, on_delete=models.SET_NULL, null=True, blank=True, related_name="uploaded_photo_reports"
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to="reports/", validators=[validate_file_size])
    image_variants = models.JSONField(default=dict, blank=True)  # WebP thumb/medium (services/images.py)
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

    # CRM review. Reports that existed before the review was introduced were
    # marked approved by migration 0028; a new upload starts pending.
    status = models.CharField(
        max_length=20, choices=PHOTO_REPORT_STATUS_CHOICES, default=PHOTO_REPORT_PENDING, db_index=True
    )
    reviewed_by = models.ForeignKey(
        Users, on_delete=models.SET_NULL, null=True, blank=True, related_name="reviewed_photo_reports"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True, max_length=REJECTION_REASON_MAX_LENGTH)

    class Meta:
        verbose_name = "Фотоотчет"
        verbose_name_plural = "Фотоотчеты"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["-created_at"], name="photoreport_created_idx"),
            models.Index(fields=["status", "-created_at"], name="photoreport_status_idx"),
        ]

    def __str__(self):
        return self.title

    @property
    def added_by_admin(self):
        """The "added by an administrator" mark: uploaded by someone else than
        the author (only the admin CRM form can do that), or by an admin."""
        if self.uploaded_by_id is None:
            return False
        return self.uploaded_by_id != self.author_id or self.uploaded_by.is_superuser

    # --- review (curator / admin) ------------------------------------------

    def _review(self, target, actor, reason=""):
        """pending → target as one conditional UPDATE (the accept_task_view
        idiom): two reviewers can't both win, a reviewed report stays as it is.
        Raises ValueError when the report is no longer pending."""
        if self.status != PHOTO_REPORT_PENDING:
            raise ValueError(f"photo report #{self.pk}: {self.status} -> {target} is not allowed")
        now = timezone.now()
        updated = PhotoReport.objects.filter(pk=self.pk, status=PHOTO_REPORT_PENDING).update(
            status=target, reviewed_by=actor, reviewed_at=now, rejection_reason=reason,
        )
        if not updated:
            raise ValueError(f"photo report #{self.pk}: already reviewed")
        self.status, self.reviewed_by, self.reviewed_at, self.rejection_reason = target, actor, now, reason

    def approve(self, actor):
        self._review(PHOTO_REPORT_APPROVED, actor)

    def reject(self, actor, reason):
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("a rejection needs a reason")
        self._review(PHOTO_REPORT_REJECTED, actor, reason[:REJECTION_REASON_MAX_LENGTH])
