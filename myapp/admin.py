from django.contrib import admin

from .models import Broadcast, Event, HelpRequest, PhotoReport


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
