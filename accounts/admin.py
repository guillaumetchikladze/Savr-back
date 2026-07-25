from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import User, UserBlock


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ['email', 'username', 'level', 'experience_points', 'is_staff', 'created_at']
    list_filter = ['is_staff', 'is_superuser', 'level']
    fieldsets = BaseUserAdmin.fieldsets + (
        ('Gamification', {'fields': ('level', 'experience_points')}),
    )


@admin.register(UserBlock)
class UserBlockAdmin(admin.ModelAdmin):
    list_display = ['blocker', 'blocked', 'created_at']
    list_filter = ['created_at']
    search_fields = ['blocker__username', 'blocker__email', 'blocked__username', 'blocked__email']
    raw_id_fields = ['blocker', 'blocked']
    ordering = ['-created_at']
