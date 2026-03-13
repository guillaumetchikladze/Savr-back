import logging
from typing import Iterable

import requests

from django.conf import settings

logger = logging.getLogger(__name__)


EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


def send_expo_push_notifications(messages: Iterable[dict]) -> None:
  """
  Envoyer une ou plusieurs notifications push via le service Expo.

  Chaque message doit être un dict compatible avec l'API Expo:
  {
    "to": "<ExpoPushToken>",
    "title": "...",
    "body": "...",
    "data": {...},
    "sound": "default",
  }
  """
  messages = list(messages or [])
  if not messages:
    logger.info("Expo push: no messages to send")
    return

  logger.info("Expo push: sending %d messages", len(messages))
  logger.debug("Expo push messages: %s", messages)

  try:
    headers = {
      "Accept": "application/json",
      "Content-Type": "application/json",
    }
    # Optionnel: token d'accès Expo, si configuré
    expo_access_token = getattr(settings, "EXPO_PUSH_ACCESS_TOKEN", None)
    if expo_access_token:
      headers["Authorization"] = f"Bearer {expo_access_token}"

    response = requests.post(EXPO_PUSH_URL, json=messages, headers=headers, timeout=10)
    logger.info("Expo push response status=%s body=%s", response.status_code, response.text)

    if response.status_code != 200:
      logger.error("Expo push failed: %s %s", response.status_code, response.text)
      return

    payload = response.json()
    if "data" not in payload:
      logger.warning("Expo push response without data: %s", payload)
      return

    for msg, ticket in zip(messages, payload["data"]):
      status = ticket.get("status")
      if status != "ok":
        logger.warning("Expo push error for %s: %s", msg.get("to"), ticket)
  except Exception:
    logger.exception("Error while sending Expo push notifications")


