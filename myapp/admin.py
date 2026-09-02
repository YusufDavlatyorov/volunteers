from django.contrib import admin, messages

from .models import Broadcast, EmergencyReport, Event, HelpRequest, PhotoReport, VolunteerApplication
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
