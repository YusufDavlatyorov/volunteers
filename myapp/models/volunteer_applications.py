from django.db import models, transaction
from django.utils import timezone

from accounts.models import REGION_CHOICES, Users


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
