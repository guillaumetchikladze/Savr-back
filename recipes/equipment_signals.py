"""
Signaux « équipements requis » : une étape créée / modifiée / supprimée, ou la recette sauvegardée (titre, portions,
surcharge) → `Recipe.required_equipment` recalculé (`equipment_service.schedule_refresh`, regroupé dans
`deferred_equipment_refresh()` pendant une création ou une édition complète).
"""

from __future__ import annotations

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from recipes.models import Recipe, Step
from recipes.services.equipment_service import schedule_refresh

_RECIPE_FIELDS = {'title', 'servings', 'required_equipment_override', 'equipment_ai_hints'}


@receiver(post_save, sender=Step, dispatch_uid='equipment_step_saved')
@receiver(post_delete, sender=Step, dispatch_uid='equipment_step_deleted')
def _step_changed(sender, instance, **kwargs):
    if kwargs.get('raw'):
        return
    schedule_refresh(instance.recipe_id)


@receiver(post_save, sender=Recipe, dispatch_uid='equipment_recipe_saved')
def _recipe_saved(sender, instance, created, update_fields=None, **kwargs):
    if kwargs.get('raw'):
        return
    if update_fields is not None and not (set(update_fields) & _RECIPE_FIELDS):
        return
    schedule_refresh(instance.pk)
