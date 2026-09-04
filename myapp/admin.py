from django.contrib import admin, messages

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
from .notifications import notify_users


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
    list_display = ("name", "price", "currency", "is_active", "updated_at")
    list_filter = ("is_active", "currency")
    search_fields = ("name", "description")
    readonly_fields = ("created_at", "updated_at")


@admin.register(Donation)
class DonationAdmin(admin.ModelAdmin):
    list_display = ("id", "donor", "amount", "currency", "status", "product", "quantity", "created_at")
    list_filter = ("status", "currency", "created_at")
    search_fields = ("donor__username", "donor__email", "message")
    # A financial record: the Django admin is read-only. Status changes go
    # through the app (services.donations, which enforces the transition rules);
    # donations are only ever created by the donate flow.
    readonly_fields = tuple(f.name for f in Donation._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PetReport)
class PetReportAdmin(admin.ModelAdmin):
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
class PhotoReportAdmin(admin.ModelAdmin):
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
            notify_users(
                [application.user],
                "Заявка на волонтерство одобрена",
                "Поздравляем! Ваша заявка на волонтерство одобрена. Теперь вам доступен список запросов.",
            )
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
            notify_users(
                [application.user],
                "Заявка на волонтерство отклонена",
                "Ваша заявка на волонтерство отклонена администратором.",
            )
            rejected += 1
        self.message_user(request, f"Отклонено заявок: {rejected}.")
