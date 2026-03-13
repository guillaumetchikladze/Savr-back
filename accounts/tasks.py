import logging

from celery import shared_task
from django.utils import timezone

from accounts.models import PushDevice
from accounts.services.expo_push import send_expo_push_notifications
from recipes.models import Timer

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

