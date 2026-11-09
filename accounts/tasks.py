import logging

from celery import shared_task
from django.contrib.auth import get_user_model
from django.db import models

from accounts.models import PushDevice
from notifications.services import push
from recipes.models import MealPlan, MealPlanRecipeBatch, Post, Timer

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_timer_almost_finished_push(self, timer_id: int) -> None:
    """
    Push quelques secondes avant la fin d'un minuteur (planifiée à expires_at - 3 s).
    Rien si le minuteur est déjà complété / supprimé. Texte : registre `timer_almost_finished`.
    """
    try:
        timer = Timer.objects.select_related('user', 'recipe_batch__recipe', 'step').get(id=timer_id)
    except Timer.DoesNotExist:
        logger.info('[TimerPush][Almost] Timer %s does not exist, skipping', timer_id)
        return

    if timer.is_completed:
        logger.info('[TimerPush][Almost] Timer %s already completed, skipping push', timer_id)
        return

    recipe = getattr(timer.recipe_batch, 'recipe', None)
    step_order = getattr(timer.step, 'order', None)
    push(
        'timer_almost_finished',
        timer.user,
        context={'step_order': step_order, 'recipe': getattr(recipe, 'title', '') or ''},
        data={
            'timer_id': timer.id,
            'recipe_id': getattr(recipe, 'id', None),
            'recipe_batch_id': timer.recipe_batch_id,
            'step_order': step_order,
        },
    )


@shared_task(bind=True, max_retries=1, default_retry_delay=120)
def send_meal_time_photo_reminder_push(self, user_id: int, meal_plan_id: int) -> None:
    """
    Push Expo vers tous les appareils actifs de l'utilisateur : « Tu as cuisiné X ? » (1 tap).

    Le payload `data` porte de quoi logguer en 1 tap via POST /posts/cooked/ {meal_plan_id}
    (`meal_plan_id`, `title`, `recipe_ids`, `date`, `meal_time`, `action='cooked_prompt'`) ;
    `source`/`kind` inchangés pour les clients ≤ 1.0.8 (ouverture du flux photo du repas).

    Règles produit (nouveau workflow = 1 notif par meal plan):
    - Ne rien envoyer si un post (publié ou log privé « J'ai cuisiné ») existe déjà pour ce meal plan
    - Ne rien envoyer si un des batches du meal plan est partagé avec un autre meal plan
    - Ne rien envoyer si un post publié existe déjà sur un des batches du meal plan (legacy)
    """
    next_task_id = ''
    try:
        try:
            meal_plan = (
                MealPlan.objects.select_related('user')
                .prefetch_related(
                    'meal_plan_recipe_batches',
                    'meal_plan_recipe_batches__recipe_batch',
                    'meal_plan_recipe_batches__recipe_batch__recipe',
                )
                .get(pk=meal_plan_id)
            )
        except MealPlan.DoesNotExist:
            logger.info('[MealPhotoReminder] meal_plan %s missing, skip', meal_plan_id)
            return

        User = get_user_model()
        try:
            target_user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            logger.info('[MealPhotoReminder] user %s missing, skip', user_id)
            return

        devices = PushDevice.objects.filter(user=target_user, is_active=True).exclude(expo_push_token='')
        if not devices:
            logger.info('[MealPhotoReminder] no push devices for user=%s', user_id)
            return

        # « Pas fait » (« Je l'ai déjà ») : on ne redemande jamais.
        from recipes.services.pending_confirmations import is_dismissed

        if is_dismissed(meal_plan_id):
            logger.info('[MealPhotoReminder] meal_plan %s dismissed (pas fait), skip', meal_plan_id)
            return

        # Ne pas notifier si un post "du repas" existe déjà (publié ou log « J'ai cuisiné » privé)
        if Post.objects.filter(meal_plan_id=meal_plan_id).exists():
            logger.info('[MealPhotoReminder] meal_plan %s already has a post, skip', meal_plan_id)
            return

        mprbs = list(getattr(meal_plan, 'meal_plan_recipe_batches', []).all())
        batch_ids = [mprb.recipe_batch_id for mprb in mprbs if mprb and mprb.recipe_batch_id]
        if not batch_ids:
            logger.info('[MealPhotoReminder] meal_plan %s has no batches, skip', meal_plan_id)
            return

        # Ne pas notifier si un batch est partagé avec un autre meal plan
        shared_batch_ids = set(
            MealPlanRecipeBatch.objects.filter(recipe_batch_id__in=batch_ids)
            .values('recipe_batch_id')
            .annotate(cnt=models.Count('meal_plan_id', distinct=True))
            .filter(cnt__gt=1)
            .values_list('recipe_batch_id', flat=True)
        )
        if shared_batch_ids:
            logger.info(
                '[MealPhotoReminder] meal_plan %s has shared batches=%s, skip',
                meal_plan_id,
                list(shared_batch_ids)[:10],
            )
            return

        # Ne pas notifier si un batch a déjà un post publié (legacy)
        if Post.objects.filter(recipe_batch_id__in=batch_ids, is_published=True).exists():
            logger.info('[MealPhotoReminder] meal_plan %s has legacy published batch post, skip', meal_plan_id)
            return

        from recipes.services.cooked_service import meal_plan_display_title

        dish_title = meal_plan_display_title(meal_plan) or ''
        recipe_ids = [
            mprb.recipe_batch.recipe_id for mprb in mprbs if mprb.recipe_batch_id and mprb.recipe_batch.recipe_id
        ]
        result = push(
            'cooked_prompt',
            target_user,
            context={'dish_title': dish_title},
            data={
                'meal_plan_id': meal_plan_id,
                # 1 tap « Oui, je l'ai cuisiné » → POST /posts/cooked/ {meal_plan_id}
                'action': 'cooked_prompt',
                'title': dish_title,
                'recipe_ids': recipe_ids,
                'recipe_id': recipe_ids[0] if recipe_ids else None,
                'date': meal_plan.date.isoformat() if meal_plan.date else None,
                'meal_time': meal_plan.meal_time,
            },
            dedupe_key=f'cooked_prompt:{meal_plan_id}',
        )
        if result.status == 'deferred' and result.retry_at:
            # Heures calmes : jamais de « Tu as cuisiné… ? » à minuit, on repousse au matin.
            deferred = send_meal_time_photo_reminder_push.apply_async(args=[user_id, meal_plan_id], eta=result.retry_at)
            next_task_id = str(getattr(deferred, 'id', '') or '')
        logger.info('[MealPhotoReminder] user=%s meal_plan=%s → %s', user_id, meal_plan_id, result.status)
    except Exception as exc:
        logger.exception('[MealPhotoReminder] failed user=%s meal_plan=%s: %s', user_id, meal_plan_id, exc)
        raise
    finally:
        MealPlan.objects.filter(pk=meal_plan_id).update(meal_time_photo_reminder_task_id=next_task_id)


@shared_task(bind=True, max_retries=2, default_retry_delay=15)
def send_aggregated_post_miam_push(self, owner_id: int, post_id: int, version=None) -> None:
    """Compat : tâches planifiées avant le registre unique → push groupée générique."""
    from notifications.services import send_grouped_push

    send_grouped_push(owner_id, 'post_miam', post_id)
