"""Tâches Celery pour le chat."""

import logging

from asgiref.sync import async_to_sync
from celery import shared_task
from channels.layers import get_channel_layer

from chat.models import Conversation

logger = logging.getLogger(__name__)


@shared_task(ignore_result=True)
def generate_conversation_title(conversation_id: int):
    """Titre après le premier échange (hors stream) : LLM court sinon 1re phrase du message, puis WS."""
    from chat.services.conversation_title import generate_title

    title = generate_title(conversation_id)
    if not title:
        return
    broadcast_title(conversation_id, title)


def broadcast_title(conversation_id: int, title: str) -> None:
    """Prévient le client WS de la conversation (best effort)."""
    try:
        channel_layer = get_channel_layer()
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                f'chat_conv_{conversation_id}',
                {'type': 'conversation_title_updated', 'conversation_id': conversation_id, 'title': title},
            )
    except Exception:
        logger.warning('[chat] diffusion du titre impossible conv=%s', conversation_id)


@shared_task(ignore_result=True)
def purge_deleted_conversations_task():
    """Supprime définitivement les conversations supprimées depuis plus de CHAT_DELETED_RETENTION_DAYS."""
    from datetime import timedelta

    from django.conf import settings
    from django.utils import timezone

    days = int(getattr(settings, 'CHAT_DELETED_RETENTION_DAYS', 30))
    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = Conversation.objects.filter(deleted_at__lt=cutoff).delete()
    if deleted:
        logger.info('Purged %d rows of deleted chat conversations', deleted)
    return deleted


@shared_task
def expire_pending_actions():
    """Marque les PendingAction expirées (tâche périodique)."""
    from django.utils import timezone

    from chat.models import PendingAction

    count = PendingAction.objects.filter(
        status=PendingAction.STATUS_PENDING,
        expires_at__lte=timezone.now(),
    ).update(status=PendingAction.STATUS_EXPIRED)
    if count:
        logger.info('Expired %d pending chat actions', count)
