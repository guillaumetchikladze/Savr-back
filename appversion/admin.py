from django.contrib import admin

from .models import AppVersionPolicy


@admin.register(AppVersionPolicy)
class AppVersionPolicyAdmin(admin.ModelAdmin):
    list_display = ('platform', 'latest_version', 'min_supported_version', 'store_url', 'updated_at')
    readonly_fields = ('updated_at',)
    fieldsets = (
        (None, {'fields': ('platform', 'latest_version', 'min_supported_version', 'store_url', 'updated_at')}),
        ('Messages affichés dans l’app', {'fields': ('message_update_available', 'message_update_required')}),
    )
