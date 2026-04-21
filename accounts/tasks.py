import logging

from celery import shared_task
from django.contrib.auth import get_user_model
from django.db import models
from django.utils import timezone

from accounts.models import PushDevice
from accounts.services.expo_push import send_expo_push_notifications
from recipes.models import MealPlan, MealPlanRecipeBatch, Post, PostPhoto, RecipeBatch, Timer

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def send_timer_almost_finished_push(self, timer_id: int) -> None:
  """
  Tâche Celery: envoyer une notification push quelques secondes avant la fin d'un minuteur.

  Cette tâche est planifiée juste avant l'heure d'expiration du minuteur (ex: expires_at - 3s).
  Si le minuteur a déjà été complété/annulé, elle ne fait rien.
  """
  try:
    timer = Timer.objects.select_related("user", "recipe_batch", "step").get(id=timer_id)
  except Timer.DoesNotExist:
    logger.info("[TimerPush][Almost] Timer %s does not exist, skipping", timer_id)
    return

  # Si le minuteur est déjà marqué comme complété, ne rien envoyer
  if timer.is_completed:
    logger.info("[TimerPush][Almost] Timer %s already completed, skipping push", timer_id)
    return

  user = timer.user
  devices = PushDevice.objects.filter(user=user, is_active=True).exclude(expo_push_token="").all()
  if not devices:
    logger.info("[TimerPush][Almost] No active push devices for user %s", user.id)
    return

  step_label = f"l'étape {timer.step.order}" if getattr(timer.step, "order", None) is not None else "l'étape"
  recipe_title = getattr(getattr(timer.recipe_batch, "recipe", None), "title", "") or ""

  if recipe_title:
    body = f"Le timer de {step_label} se termine dans quelques secondes, reviens dans la cuisine pour continuer la recette {recipe_title}."
  else:
    body = f"Le timer de {step_label} se termine dans quelques secondes, reviens dans la cuisine pour continuer la recette."

  data = {
    "source": "timer",
    "kind": "almost_finished",
    "timerId": timer.id,
    "recipeId": getattr(getattr(timer.recipe_batch, "recipe", None), "id", None),
    "recipeBatchId": timer.recipe_batch_id,
    "stepOrder": getattr(timer.step, "order", None),
  }

  messages = []
  for device in devices:
    messages.append(
      {
        "to": device.expo_push_token,
        "channelId": "timer_near_end",
        "title": "Le minuteur va se terminer",
        "body": body,
        "data": data,
        "sound": "default",
      }
    )

  send_expo_push_notifications(messages)


@shared_task(bind=True, max_retries=1, default_retry_delay=120)
def send_meal_time_photo_reminder_push(self, user_id: int, meal_plan_id: int) -> None:
  """
  Push Expo vers tous les appareils actifs de l'utilisateur : rappel photo « à table ».

  Règles produit (nouveau workflow = 1 notif par meal plan):
  - Ne rien envoyer si un post publié existe déjà pour ce meal plan
  - Ne rien envoyer si un des batches du meal plan est partagé avec un autre meal plan
  - Ne rien envoyer si un post publié existe déjà sur un des batches du meal plan (legacy)
  """
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

    devices = PushDevice.objects.filter(
        user=target_user, is_active=True
    ).exclude(expo_push_token='')
    if not devices:
      logger.info('[MealPhotoReminder] no push devices for user=%s', user_id)
      return

    # Ne pas notifier si un post "du repas" existe déjà
    if Post.objects.filter(meal_plan_id=meal_plan_id, is_published=True).exists():
      logger.info('[MealPhotoReminder] meal_plan %s already has published post, skip', meal_plan_id)
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

    # Wording concis
    title = 'Photo du repas'
    if len(mprbs) == 1:
      recipe_title = (getattr(getattr(mprbs[0], 'recipe_batch', None), 'recipe', None) and getattr(mprbs[0].recipe_batch.recipe, 'title', None)) or ''
      recipe_title = str(recipe_title or '').strip()
      body = f'À table ! Pense à prendre une photo{f" de « {recipe_title} »" if recipe_title else ""}.'
    else:
      body = "À table ! Pense à prendre une photo de tes plats."

    data = {
        'source': 'cooking',
        'kind': 'meal_time_photo_reminder',
        'meal_plan_id': meal_plan_id,
    }

    messages = []
    for device in devices:
      msg = {
          'to': device.expo_push_token,
          'title': title,
          'body': body,
          'data': data,
          'sound': 'default',
          'channelId': 'meal_photos',
      }
      messages.append(msg)

    logger.info(
        '[MealPhotoReminder] sending %d pushes user=%s meal_plan=%s',
        len(messages),
        user_id,
        meal_plan_id,
    )
    send_expo_push_notifications(messages)
  except Exception as exc:
    logger.exception('[MealPhotoReminder] failed user=%s meal_plan=%s: %s', user_id, meal_plan_id, exc)
    raise
  finally:
    MealPlan.objects.filter(pk=meal_plan_id).update(meal_time_photo_reminder_task_id='')

