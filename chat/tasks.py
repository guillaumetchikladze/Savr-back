"""Tâches Celery pour le chat."""

import logging

from celery import shared_task
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

from chat.models import Conversation, Message

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=1)
def generate_conversation_title(self, conversation_id: int):
    """Génère un titre de conversation après message_complete (hors stream)."""
    conv = Conversation.objects.filter(id=conversation_id).first()
    if not conv:
        return

    if conv.title and conv.title != 'Nouvelle conversation':
        return

    first_user_msg = (
        Message.objects.filter(
            conversation=conv,
            role=Message.ROLE_USER,
            message_type=Message.TYPE_TEXT,
        )
        .order_by('created_at')
        .first()
    )
    if not first_user_msg or not first_user_msg.content:
        return

    title = first_user_msg.content.strip()[:80]
    if len(first_user_msg.content.strip()) > 80:
        title += '…'

    Conversation.objects.filter(id=conv.id).update(title=title)

    channel_layer = get_channel_layer()
    if channel_layer:
        async_to_sync(channel_layer.group_send)(
            f'chat_conv_{conv.id}',
            {
                'type': 'conversation_title_updated',
                'title': title,
            },
        )


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
