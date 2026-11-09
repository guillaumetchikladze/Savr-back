from django.contrib import admin

from .models import NotificationDelivery, NotificationPreference


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = ('user', 'timezone', 'quiet_hours_start', 'quiet_hours_end', 'updated_at')
    search_fields = ('user__username', 'user__email')
    raw_id_fields = ('user',)


@admin.register(NotificationDelivery)
class NotificationDeliveryAdmin(admin.ModelAdmin):
    list_display = ('type', 'user', 'status', 'transactional', 'device_count', 'created_at', 'opened_at')
    list_filter = ('type', 'category', 'status', 'transactional')
    search_fields = ('user__username', 'title', 'body', 'dedupe_key')
    raw_id_fields = ('user', 'inbox_notification')
    date_hierarchy = 'created_at'
