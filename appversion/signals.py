from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import AppVersionPolicy
from .services import invalidate_policy_cache


@receiver(post_save, sender=AppVersionPolicy)
@receiver(post_delete, sender=AppVersionPolicy)
def _invalidate_policy_cache(sender, instance, **kwargs):
    # Toutes les plateformes : couvre un éventuel changement de `platform` sur une ligne existante.
    invalidate_policy_cache()
