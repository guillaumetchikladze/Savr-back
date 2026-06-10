"""Notification chat quand un job recette (import / génération) se termine."""

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger(__name__)


def build_recipe_job_ready_system_content(event: dict) -> str:
    recipe_title = event.get('recipe_title') or 'Recette'
    recipe_id = event.get('recipe_id')
    job_type = event.get('job_type') or 'import'
    kind = 'génération' if job_type == 'generate' else 'import'
    return (
        f"[Événement Tchikook Agent] La {kind} de recette est terminée avec succès.\n"
        f"- Titre : {recipe_title}\n"
        f"- recipe_id : {recipe_id}\n"
        "L'utilisateur attend la suite : propose une action concrète "
        "(planifier avec add_recipe_to_meal_plan si pertinent, ou inviter à consulter la fiche). "
        "Ne relance pas generate_recipe_from_idea ni import_recipe_from_url pour cette recette."
    )


def snapshot_chat_import_job_message(import_request, *, status: str = 'success') -> None:
    """Persiste l'état final du job dans le message chat (évite le polling à la réouverture)."""
    payload = import_request.payload or {}
    chat_ctx = payload.get('chat_context') or {}
    conversation_id = chat_ctx.get('conversation_id')
    if not conversation_id:
        return

    from chat.models import Message

    request_id = str(import_request.id)
    job_type = payload.get('job_type') or ('generate' if payload.get('idea_text') else 'import')
    recipe = import_request.recipe

    snapshot = {
        'request_id': request_id,
        'status': status,
        'job_type': job_type,
        'url': payload.get('url') or '',
        'idea_text': payload.get('idea_text') or '',
    }
    if status == 'success' and recipe:
        snapshot['recipe_id'] = recipe.id
        snapshot['recipe_title'] = recipe.title
    elif status == 'error':
        snapshot['error_message'] = import_request.error_message or ''

    qs = (
        Message.objects.filter(
            conversation_id=conversation_id,
            role=Message.ROLE_ASSISTANT,
        )
        .exclude(ui_payload__isnull=True)
        .order_by('-created_at')[:40]
    )
    for msg in qs:
        ui = msg.ui_payload or {}
        ij = ui.get('import_job') or {}
        if str(ij.get('request_id')) != request_id:
            continue
        msg.ui_payload = {**ui, 'import_job': {**ij, **snapshot}}
        msg.save(update_fields=['ui_payload'])
        logger.info(
            '[RecipeJobNotify] import_job snapshot conv=%s request=%s status=%s',
            conversation_id,
            request_id,
            status,
        )
        return


def notify_chat_recipe_job_completed(import_request) -> bool:
    """
    Déclenche une continuation agent sur la conversation liée, si l'utilisateur
    est encore connecté au WebSocket (group_send → consumer).
    """
    payload = import_request.payload or {}
    chat_ctx = payload.get('chat_context') or {}
    conversation_id = chat_ctx.get('conversation_id')
    if not conversation_id or not import_request.recipe_id:
        return False

    snapshot_chat_import_job_message(import_request, status='success')

    if payload.get('chat_continuation_sent'):
        return False

    payload = {**payload, 'chat_continuation_sent': True}
    import_request.payload = payload
    import_request.save(update_fields=['payload', 'updated_at'])

    channel_layer = get_channel_layer()
    if not channel_layer:
        logger.warning('[RecipeJobNotify] channel layer unavailable conv=%s', conversation_id)
        return False

    recipe = import_request.recipe
    job_type = payload.get('job_type') or ('generate' if payload.get('idea_text') else 'import')

    async_to_sync(channel_layer.group_send)(
        f'chat_conv_{conversation_id}',
        {
            'type': 'recipe_job_completed',
            'conversation_id': conversation_id,
            'request_id': str(import_request.id),
            'recipe_id': recipe.id,
            'recipe_title': recipe.title,
            'job_type': job_type,
            'idea_text': payload.get('idea_text') or '',
            'url': payload.get('url') or '',
        },
    )
    logger.info(
        '[RecipeJobNotify] continuation queued conv=%s recipe_id=%s request=%s',
        conversation_id,
        recipe.id,
        import_request.id,
    )
    return True
