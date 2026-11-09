from django.contrib import admin
from django.utils import timezone

from recipes.models import IngredientInfo


@admin.register(IngredientInfo)
class IngredientInfoAdmin(admin.ModelAdmin):
    """Fiches produit des courses : une modification humaine passe la fiche en `manual` (jamais régénérée)."""

    list_display = ['id', 'ingredient', 'aisle', 'source', 'model', 'generated_at', 'updated_at']
    list_filter = ['source', 'aisle']
    search_fields = ['ingredient__name', 'what', 'aisle']
    raw_id_fields = ['ingredient']
    readonly_fields = ['model', 'generated_at', 'created_at', 'updated_at']

    def save_model(self, request, obj, form, change):
        if form.changed_data:
            obj.source = IngredientInfo.SOURCE_MANUAL
            if not obj.generated_at:
                obj.generated_at = timezone.now()
        super().save_model(request, obj, form, change)
