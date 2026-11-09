from django.contrib import admin

from .models import IngredientProfile, UserBadge


@admin.register(IngredientProfile)
class IngredientProfileAdmin(admin.ModelAdmin):
    list_display = ('ingredient', 'food_group', 'is_plant', 'is_basic_staple', 'source', 'validated', 'updated_at')
    list_filter = ('food_group', 'is_plant', 'is_basic_staple', 'source', 'validated')
    list_editable = ('food_group', 'is_plant', 'is_basic_staple', 'validated')
    search_fields = ('ingredient__name',)
    raw_id_fields = ('ingredient',)


@admin.register(UserBadge)
class UserBadgeAdmin(admin.ModelAdmin):
    list_display = ('user', 'code', 'earned_at')
    list_filter = ('code',)
    search_fields = ('user__username', 'code')
    raw_id_fields = ('user',)
