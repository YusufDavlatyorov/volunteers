from django.contrib import admin, messages
from django.db.models import Count, Q
from django.urls import reverse
from django.utils.html import format_html

from .models import (
    Broadcast,
    Donation,
    DonationPhoto,
    EmergencyReport,
    Event,
    FAQCategory,
    FAQItem,
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


class DonationPhotoInline(admin.TabularInline):
    """The offer's photos, read-only. Private files: the preview goes through
    the permission-checked donation_photo view, never a /media/ URL."""

    model = DonationPhoto
    extra = 0
    can_delete = False
    fields = ("preview", "created_at")
    readonly_fields = ("preview", "created_at")

    @admin.display(description="Фото")
    def preview(self, obj):
        if not obj.pk:
            return "—"
        url = reverse("donation_photo", args=[obj.pk])
        return format_html('<a href="{}" target="_blank" rel="noopener"><img src="{}" alt="" style="max-height:120px;border-radius:6px"></a>', url, url)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Donation)
class DonationAdmin(admin.ModelAdmin):
    list_display = (
        "id", "donor", "donor_type", "category", "quantity", "status",
        "assigned_volunteer", "created_at",
    )
    list_filter = ("status", "category", "region", "condition", "fulfilment", "donor_type", "created_at")
    search_fields = ("donor__username", "donor__email", "organization_name", "item_name", "message", "contact_phone")
    inlines = (DonationPhotoInline,)
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
    list_display = ("title", "author", "status", "region", "created_at")
    list_filter = ("status", "region", "created_at")
    search_fields = ("title", "description")
    # The review goes through the CRM (/myapp/crm/photo-reports/ — it notifies
    # the author and releases the task's reward); a raw admin edit would skip both.
    readonly_fields = ("status", "reviewed_by", "reviewed_at", "rejection_reason", "uploaded_by")

    def save_model(self, request, obj, form, change):
        # Who really uploaded it (the site's forms set it in photo_reports.create).
        if not change:
            obj.uploaded_by = request.user
        super().save_model(request, obj, form, change)

    # Deleting here takes the same path as the CRM "Delete" button: files +
    # WebP variants removed, an approved report's task reward revoked, the
    # author notified, one ID-only log line (services/photo_reports.delete).
    def delete_model(self, request, obj):
        from .services import photo_reports

        photo_reports.delete(obj, actor=request.user)

    def delete_queryset(self, request, queryset):
        from .services import photo_reports

        for report in queryset:
            photo_reports.delete(report, actor=request.user)


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
    list_display = ("id", "task", "volunteer", "client", "stars", "show_on_landing", "hidden_at", "created_at")
    list_filter = ("stars", "show_on_landing", ("hidden_at", admin.EmptyFieldListFilter), "created_at")
    search_fields = ("volunteer__username", "client__username")
    list_select_related = ("task", "volunteer", "client")
    # Read-only: stars/comment are the client's words, and a raw edit/delete
    # would skip the denormalised Profile.review_avg/review_count recompute.
    # Moderation goes through services.feedback.hide/unhide (the actions below,
    # or the CRM list at /myapp/crm/reviews/). The one editable field is the
    # About-page opt-in, saved through services.feedback.set_landing.
    readonly_fields = tuple(f.name for f in TaskReview._meta.fields if f.name != "show_on_landing")
    actions = ("hide_reviews", "unhide_reviews", "feature_on_landing", "remove_from_landing")

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

    def save_model(self, request, obj, form, change):
        # Only show_on_landing is editable; route it through the service (cache
        # + ID-only log) instead of a raw save.
        if change and "show_on_landing" in form.changed_data:
            feedback_service.set_landing(request.user, obj, obj.show_on_landing)

    @admin.action(description="Показывать на главной (только 5★ с текстом)")
    def feature_on_landing(self, request, queryset):
        done = sum(1 for r in queryset if feedback_service.set_landing(request.user, r, True).code == feedback_service.OK)
        shown = queryset.filter(stars=5, hidden_at__isnull=True).exclude(comment="").count()
        self.message_user(request, f"Отмечено для главной: {done}. Подходят для показа (5★, с текстом, не скрыты): {shown}.")

    @admin.action(description="Убрать с главной")
    def remove_from_landing(self, request, queryset):
        done = sum(1 for r in queryset if feedback_service.set_landing(request.user, r, False).code == feedback_service.OK)
        self.message_user(request, f"Убрано с главной: {done}.")


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


# --- FAQ (/faq/) ---------------------------------------------------------------
# Order is edited as a number right in the lists (no drag-and-drop dependency);
# the page shows only active categories that have at least one active question.
class FAQItemInline(admin.StackedInline):
    model = FAQItem
    extra = 0
    ordering = ("order", "id")
    fieldsets = (
        (None, {"fields": (("order", "is_active"),)}),
        ("Русский", {"fields": ("question_ru", "answer_ru")}),
        ("Тоҷикӣ", {"fields": ("question_tj", "answer_tj"), "classes": ("collapse",)}),
        ("English", {"fields": ("question_en", "answer_en"), "classes": ("collapse",)}),
    )


@admin.register(FAQCategory)
class FAQCategoryAdmin(admin.ModelAdmin):
    list_display = ("name_ru", "order", "is_active", "items_total", "items_active")
    list_display_links = ("name_ru",)
    list_editable = ("order", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name_ru", "name_tj", "name_en")
    ordering = ("order", "id")
    fields = ("name_ru", "name_tj", "name_en", ("order", "is_active"))
    inlines = [FAQItemInline]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _total=Count("items", distinct=True),
            _active=Count("items", filter=Q(items__is_active=True), distinct=True),
        )

    @admin.display(description="Вопросов", ordering="_total")
    def items_total(self, obj):
        return obj._total

    @admin.display(description="Активных", ordering="_active")
    def items_active(self, obj):
        return obj._active


@admin.register(FAQItem)
class FAQItemAdmin(admin.ModelAdmin):
    list_display = ("question_ru", "category", "order", "is_active", "updated_at")
    list_display_links = ("question_ru",)
    list_editable = ("order", "is_active")
    list_filter = ("category", "is_active")
    list_select_related = ("category",)
    search_fields = ("question_ru", "question_tj", "question_en")
    ordering = ("category__order", "category_id", "order", "id")
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("category", ("order", "is_active"))}),
        ("Русский", {"fields": ("question_ru", "answer_ru")}),
        ("Тоҷикӣ", {"fields": ("question_tj", "answer_tj")}),
        ("English", {"fields": ("question_en", "answer_en")}),
        ("Даты", {"fields": (("created_at", "updated_at"),), "classes": ("collapse",)}),
    )
