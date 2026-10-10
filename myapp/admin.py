from django.contrib import admin, messages
from django.urls import reverse

from .models import (
    Broadcast,
    Donation,
    EmergencyReport,
    Event,
    HelpRequest,
    PetReport,
    PhotoReport,
    Product,
    VolunteerApplication,
)
from .services.images import CleanImageAdminMixin
from .services.notify import notify


@admin.register(EmergencyReport)
class EmergencyReportAdmin(admin.ModelAdmin):
    list_display = ("id", "status", "volunteer", "help_request", "region", "created_at")
    list_filter = ("status", "region", "created_at")
    search_fields = ("volunteer__username", "help_request__client__username", "reason")
    readonly_fields = (
        "help_request", "volunteer", "reason", "region", "latitude", "longitude",
        "created_at", "updated_at", "notified_at",
        "acknowledged_at", "acknowledged_by", "resolved_at", "resolved_by",
        "cancelled_at", "cancelled_by",
    )

    def has_add_permission(self, request):
        # Reports are only ever created by a volunteer through the SOS action.
        return False


@admin.register(HelpRequest)
class HelpRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "client", "volunteer", "help_type", "status", "region", "is_urgent", "created_at")
    list_filter = ("status", "help_type", "region", "is_urgent")
    search_fields = ("client__username", "volunteer__username", "address", "phone")
    readonly_fields = ("created_at", "updated_at", "accepted_at", "completed_at")


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("title", "curator", "region", "date", "notifications_sent")
    list_filter = ("region", "notifications_sent", "date")
    search_fields = ("title", "curator__username")


@admin.register(Broadcast)
class BroadcastAdmin(admin.ModelAdmin):
    list_display = ("subject", "sender", "region", "sent_count", "created_at")
    list_filter = ("region", "created_at")
    search_fields = ("subject", "message")


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "unit", "is_active", "updated_at")
    list_filter = ("is_active", "category")
    search_fields = ("name", "description")
    readonly_fields = ("created_at", "updated_at")


@admin.register(Donation)
class DonationAdmin(admin.ModelAdmin):
    list_display = (
        "id", "donor", "donor_type", "category", "quantity", "status",
        "assigned_volunteer", "created_at",
    )
    list_filter = ("status", "category", "donor_type", "region", "created_at")
    search_fields = ("donor__username", "donor__email", "organization_name", "item_name", "message")
    # A coordination record: the Django admin is read-only. Status changes go
    # through the app (services.donations, which enforces the transition rules);
    # offers are only ever created by the donate flow.
    readonly_fields = tuple(f.name for f in Donation._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PetReport)
class PetReportAdmin(CleanImageAdminMixin, admin.ModelAdmin):
    list_display = ("id", "report_type", "species", "pet_name", "status", "region", "reporter", "created_at")
    list_filter = ("report_type", "species", "status", "region", "created_at")
    search_fields = ("pet_name", "breed", "description", "reporter__username")
    # Status changes go through the app (services.pets / model methods enforce
    # the transition rules); a raw admin edit would bypass them. Reports are only
    # ever created through the board.
    readonly_fields = (
        "reporter", "status", "reviewed_by",
        "created_at", "updated_at", "matched_at", "resolved_at", "closed_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(PhotoReport)
class PhotoReportAdmin(CleanImageAdminMixin, admin.ModelAdmin):
    list_display = ("title", "author", "region", "created_at")
    list_filter = ("region", "created_at")
    search_fields = ("title", "description")


@admin.register(VolunteerApplication)
class VolunteerApplicationAdmin(admin.ModelAdmin):
    list_display = ("user", "region", "status", "created_at", "reviewed_at", "reviewed_by")
    list_filter = ("status", "region")
    search_fields = ("user__username", "user__email")
    readonly_fields = ("created_at",)
    actions = ("approve_applications", "reject_applications")

    def has_add_permission(self, request):
        # Applications are only created through the registration/reapply flow.
        return False

    def get_readonly_fields(self, request, obj=None):
        base = list(self.readonly_fields)
        if not request.user.is_superuser:
            base += ["status", "reviewed_by", "reviewed_at"]
        return base

    def get_actions(self, request):
        # Only admins/superusers may grant or deny the volunteer role, even if
        # a curator has been given "change" permission on this model.
        actions = super().get_actions(request)
        if not request.user.is_superuser:
            actions.pop("approve_applications", None)
            actions.pop("reject_applications", None)
        return actions

    @admin.action(description="Approve selected applications")
    def approve_applications(self, request, queryset):
        if not request.user.is_superuser:
            self.message_user(request, "Только администратор может одобрять заявки.", level=messages.ERROR)
            return
        approved = 0
        for application in queryset.exclude(status=VolunteerApplication.STATUS_APPROVED).select_related("user"):
            application.approve(request.user)
            notify([application.user], "application_approved", link=reverse("task_list"))
            approved += 1
        self.message_user(request, f"Одобрено заявок: {approved}.")

    @admin.action(description="Reject selected applications")
    def reject_applications(self, request, queryset):
        if not request.user.is_superuser:
            self.message_user(request, "Только администратор может отклонять заявки.", level=messages.ERROR)
            return
        rejected = 0
        for application in queryset.exclude(status=VolunteerApplication.STATUS_REJECTED).select_related("user"):
            application.reject(request.user)
            notify([application.user], "application_rejected", link=reverse("volunteer_application"))
            rejected += 1
        self.message_user(request, f"Отклонено заявок: {rejected}.")


# --------------------------------------------------------------------------- #
# Client reviews (Feature Pack v2, feature 9)
# --------------------------------------------------------------------------- #
from .models import TaskReview  # noqa: E402
from .services import feedback as feedback_service  # noqa: E402


@admin.register(TaskReview)
class TaskReviewAdmin(admin.ModelAdmin):
    list_display = ("id", "task", "volunteer", "client", "stars", "hidden_at", "published_at", "created_at")
    list_filter = ("stars", ("hidden_at", admin.EmptyFieldListFilter), ("published_at", admin.EmptyFieldListFilter), "created_at")
    search_fields = ("volunteer__username", "client__username")
    list_select_related = ("task", "volunteer", "client")
    # Read-only: stars/comment are the client's words, and a raw edit/delete
    # would skip the denormalised Profile.review_avg/review_count recompute.
    # Moderation goes through services.feedback.hide/unhide (the actions below,
    # or the CRM list at /myapp/crm/reviews/).
    readonly_fields = tuple(f.name for f in TaskReview._meta.fields)
    actions = ("hide_reviews", "unhide_reviews")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Hide selected reviews")
    def hide_reviews(self, request, queryset):
        done = sum(1 for r in queryset if feedback_service.hide(request.user, r).code == feedback_service.OK)
        self.message_user(request, f"Скрыто отзывов: {done}.")

    @admin.action(description="Show selected reviews again")
    def unhide_reviews(self, request, queryset):
        done = sum(1 for r in queryset if feedback_service.unhide(request.user, r).code == feedback_service.OK)
        self.message_user(request, f"Снова видимых отзывов: {done}.")


# --- Achievements (Feature Pack v2, milestone 4) ---------------------------
import logging  # noqa: E402

from .models import Achievement, UserAchievement  # noqa: E402

_ach_logger = logging.getLogger("myapp.achievements")


@admin.register(Achievement)
class AchievementAdmin(admin.ModelAdmin):
    # The owner tweaks thresholds / tiers / order / on-off here; the rule behind
    # each code lives in services/achievements.py, so the code itself is fixed.
    list_display = ("code", "tier", "category", "threshold", "sort_order", "is_active")
    list_editable = ("tier", "threshold", "sort_order", "is_active")
    list_filter = ("tier", "category", "is_active")
    readonly_fields = ("code",)

    def has_add_permission(self, request):
        return False  # a new code needs a rule in services/achievements.py

    def has_delete_permission(self, request, obj=None):
        return False  # deactivate instead — earned badges stay with their owners


@admin.register(UserAchievement)
class UserAchievementAdmin(admin.ModelAdmin):
    # Awards come only from services.achievements.evaluate; staff may remove one
    # (e.g. earned through reviews later found fake) — logged with IDs only.
    list_display = ("id", "user", "achievement", "awarded_at", "progress_snapshot")
    list_filter = ("achievement", "awarded_at")
    search_fields = ("user__username",)
    list_select_related = ("user", "achievement")
    readonly_fields = ("user", "achievement", "awarded_at", "progress_snapshot")

    def has_add_permission(self, request):
        return False

    def delete_model(self, request, obj):
        _ach_logger.info("achievement removed award=%s user=%s code=%s by=%s",
                         obj.pk, obj.user_id, obj.achievement.code, request.user.pk)
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        for obj in queryset.select_related("achievement"):
            _ach_logger.info("achievement removed award=%s user=%s code=%s by=%s",
                             obj.pk, obj.user_id, obj.achievement.code, request.user.pk)
        super().delete_queryset(request, queryset)
