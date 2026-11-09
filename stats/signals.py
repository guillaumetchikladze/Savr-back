"""
Signaux stats (jamais bloquants : tout passe par transaction.on_commit + try/except) :
- nouvel ingrédient → classification différée (lot débouncé).
"""

from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from recipes.models import Ingredient

from .tasks import schedule_ingredient_classification


@receiver(post_save, sender=Ingredient, dispatch_uid='stats_ingredient_created')
def _ingredient_created(sender, instance, created, **kwargs):
    if created and not kwargs.get('raw'):
        transaction.on_commit(schedule_ingredient_classification)
