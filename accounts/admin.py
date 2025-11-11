from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ['email', 'username', 'level', 'experience_points', 'is_staff', 'created_at']
    list_filter = ['is_staff', 'is_superuser', 'level']
    fieldsets = BaseUserAdmin.fieldsets + (
        ('Gamification', {'fields': ('level', 'experience_points')}),
    )

