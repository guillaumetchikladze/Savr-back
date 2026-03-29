from django.contrib import admin

from .models import EmailQueue


@admin.register(EmailQueue)
class EmailQueueAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "status",
        "priority",
        "action_name",
        "to_email",
        "subject",
        "retries",
        "max_retries",
        "sent_at",
        "created_at",
    )
    list_filter = ("status", "priority", "action_name")
    search_fields = ("to_email", "subject", "action_name", "error")
    readonly_fields = ("created_at", "updated_at", "sent_at")

