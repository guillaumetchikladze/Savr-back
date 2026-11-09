"""
Signaux stats (jamais bloquants : tout passe par transaction.on_commit + try/except) :
- nouvel ingrédient → classification différée (lot débouncé) ;
- repas confirmé / post créé → évaluation différée des badges ;
- miam / invitation → invalidation du cache stats (le « cuisiné » est couvert par l'empreinte DB) ;
- invités / recettes du repas / fin du mode cuisine → ardoise des posts du repas à recalculer (`Post.highlights`).
"""

from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from recipes.models import CookingProgress, Ingredient, MealInvitation, MealPlan, MealPlanRecipeBatch, Post, PostCookie

from .services.highlights import invalidate_post_highlights
from .services.stats_service import invalidate_user_stats
from .tasks import schedule_badge_evaluation, schedule_ingredient_classification


@receiver(post_save, sender=Ingredient, dispatch_uid='stats_ingredient_created')
def _ingredient_created(sender, instance, created, **kwargs):
    if created and not kwargs.get('raw'):
        transaction.on_commit(schedule_ingredient_classification)


_ARDOISE_MEAL_FIELDS = frozenset({'guest_count', 'date', 'custom_label'})


@receiver(post_save, sender=MealPlan, dispatch_uid='stats_mealplan_saved')
def _meal_plan_saved(sender, instance, created=False, update_fields=None, **kwargs):
    if instance.confirmed and not kwargs.get('raw'):
        user_id = instance.user_id
        transaction.on_commit(lambda: schedule_badge_evaluation(user_id))
        if not created and (update_fields is None or _ARDOISE_MEAL_FIELDS & set(update_fields)):
            invalidate_post_highlights(meal_plan_ids=[instance.pk])


@receiver(post_save, sender=MealPlanRecipeBatch, dispatch_uid='stats_mprb_saved')
@receiver(post_delete, sender=MealPlanRecipeBatch, dispatch_uid='stats_mprb_deleted')
def _meal_plan_recipes_changed(sender, instance, **kwargs):
    if not kwargs.get('raw'):
        invalidate_post_highlights(meal_plan_ids=[instance.meal_plan_id])


@receiver(post_save, sender=CookingProgress, dispatch_uid='stats_cooking_progress_saved')
def _cooking_progress_saved(sender, instance, **kwargs):
    if instance.status == 'completed' and instance.total_time_minutes and not kwargs.get('raw'):
        invalidate_post_highlights(recipe_batch_id=instance.recipe_batch_id)


@receiver(post_save, sender=Post, dispatch_uid='stats_post_saved')
def _post_saved(sender, instance, created, **kwargs):
    if created and not kwargs.get('raw'):
        user_id = instance.user_id
        transaction.on_commit(lambda: schedule_badge_evaluation(user_id))


@receiver(post_save, sender=PostCookie, dispatch_uid='stats_cookie_saved')
@receiver(post_delete, sender=PostCookie, dispatch_uid='stats_cookie_deleted')
def _cookie_changed(sender, instance, **kwargs):
    owner_id = Post.objects.filter(pk=instance.post_id).values_list('user_id', flat=True).first()
    invalidate_user_stats(owner_id)


@receiver(post_save, sender=MealInvitation, dispatch_uid='stats_invitation_saved')
@receiver(post_delete, sender=MealInvitation, dispatch_uid='stats_invitation_deleted')
def _invitation_changed(sender, instance, **kwargs):
    invalidate_user_stats(instance.inviter_id)
    invalidate_user_stats(instance.invitee_id)
    invalidate_post_highlights(meal_plan_ids=[instance.meal_plan_id])
