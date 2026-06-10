"""WebSocket consumer pour le chat agent — protocole par blocs."""

import logging
import uuid
from urllib.parse import parse_qs

from asgiref.sync import sync_to_async
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.db import close_old_connections
from django.utils import timezone

from chat.models import Conversation, Message, PendingAction
from chat.services.action_executor import execute_pending_action
from chat.services.agent_context import AgentContext
from chat.services.agent_factory import create_planning_agent
from chat.services.rate_limit import (
    acquire_stream_lock,
    check_message_rate_limit,
    default_action_expiry,
    release_stream_lock,
)
from chat.services.recipe_job_notify import build_recipe_job_ready_system_content
from chat.services.stream_adapter import BlockStreamAdapter, build_agent_history
from chat.tasks import generate_conversation_title
from core.ws_auth import get_user_from_token

logger = logging.getLogger(__name__)

HISTORY_LIMIT = 30


class ChatConsumer(AsyncJsonWebsocketConsumer):
    conversation_id = None
    conversation_group = None

    async def connect(self):
        query_string = self.scope.get('query_string', b'').decode('utf-8')
        query_params = parse_qs(query_string)
        token = (query_params.get('token') or [None])[0]

        if not token:
            await self.close(code=4001)
            return

        user = await get_user_from_token(token)
        if not user:
            await self.close(code=4003)
            return

        self.scope['user'] = user
        self.conversation_id = None
        self.conversation_group = None
        await self.accept()

    async def disconnect(self, close_code):
        if self.conversation_group:
            await self.channel_layer.group_discard(
                self.conversation_group, self.channel_name
            )

    async def receive_json(self, content, **kwargs):
        action = content.get('action')
        user = self.scope.get('user')

        if action == 'join_conversation':
            await self._handle_join(content, user)
        elif action == 'user_message':
            await self._handle_user_message(content, user)
        elif action == 'confirm_action':
            await self._handle_confirm_action(content, user)
        elif action == 'cancel_action':
            await self._handle_cancel_action(content, user)

    async def _handle_join(self, content, user):
        raw_id = content.get('conversation_id')
        if raw_id is None:
            return
        try:
            conversation_id = int(raw_id)
        except (TypeError, ValueError):
            await self.send_json({
                'type': 'error',
                'code': 'invalid_conversation',
                'message': 'Identifiant de conversation invalide.',
            })
            return

        conv = await database_sync_to_async(
            Conversation.objects.filter(id=conversation_id, user=user).first
        )()
        close_old_connections()
        if not conv:
            await self.send_json({
                'type': 'error',
                'code': 'not_found',
                'message': 'Conversation introuvable.',
            })
            return

        if self.conversation_group:
            try:
                await self.channel_layer.group_discard(
                    self.conversation_group, self.channel_name
                )
            except Exception:
                logger.warning('group_discard failed conv=%s', self.conversation_id)

        self.conversation_id = conv.id
        self.conversation_group = f'chat_conv_{conv.id}'

        # Ack immédiat — ne pas bloquer sur Redis (titres Celery optionnels)
        await self.send_json({
            'type': 'conversation_joined',
            'conversation_id': conv.id,
        })

        try:
            if self.channel_layer:
                await self.channel_layer.group_add(self.conversation_group, self.channel_name)
        except Exception:
            logger.warning('group_add failed conv=%s — chat direct WS OK', conv.id)

    async def _handle_user_message(self, content, user):
        conversation_id = content.get('conversation_id') or self.conversation_id
        text = (content.get('content') or '').strip()
        context_refs = _normalize_context_refs(content.get('context_refs'))
        if context_refs:
            logger.info(
                'Chat context_refs conv=%s count=%s types=%s',
                conversation_id,
                len(context_refs),
                [r.get('type') for r in context_refs],
            )

        if not conversation_id or not text:
            return

        if not check_message_rate_limit(user.id):
            await self.send_json({
                'type': 'error',
                'code': 'rate_limit',
                'message': 'Trop de messages. Réessayez dans une minute.',
            })
            return

        conv = await database_sync_to_async(
            Conversation.objects.filter(id=conversation_id, user=user).first
        )()
        close_old_connections()
        if not conv:
            await self.send_json({
                'type': 'error',
                'code': 'not_found',
                'message': 'Conversation introuvable.',
            })
            return

        if not acquire_stream_lock(conv.id):
            await self.send_json({
                'type': 'error',
                'code': 'stream_busy',
                'message': 'Un message est déjà en cours de traitement.',
            })
            return

        try:
            await self._process_user_message(conv, user, text, context_refs)
        except Exception:
            logger.exception('Chat stream failed conv=%s user=%s', conv.id, user.id)
            await self.send_json({
                'type': 'error',
                'code': 'agent_error',
                'message': 'Une erreur est survenue. Réessayez.',
            })
        finally:
            release_stream_lock(conv.id)

    async def _process_user_message(self, conv, user, text, context_refs=None):
        await database_sync_to_async(_save_user_message)(conv, text, context_refs)
        close_old_connections()
        await self._run_agent_turn(conv, user)

    async def recipe_job_completed(self, event):
        """Continuation silencieuse après import/génération (utilisateur encore sur le chat)."""
        conv_id = event.get('conversation_id')
        if not conv_id or self.conversation_id != conv_id:
            return

        user = self.scope.get('user')
        if not user:
            return

        if not acquire_stream_lock(conv_id):
            logger.info('Skip recipe continuation — stream busy conv=%s', conv_id)
            return

        try:
            conv = await database_sync_to_async(
                Conversation.objects.filter(id=conv_id, user=user).first
            )()
            close_old_connections()
            if not conv:
                return

            await database_sync_to_async(_save_recipe_ready_system_event)(conv, event)
            close_old_connections()

            await self.send_json({
                'type': 'import_job_finished',
                'request_id': event.get('request_id'),
                'recipe_id': event.get('recipe_id'),
                'recipe_title': event.get('recipe_title'),
                'job_type': event.get('job_type'),
            })

            turn_id = str(uuid.uuid4())
            await self.send_json({
                'type': 'continuation_turn_start',
                'turn_id': turn_id,
                'stream_phase': 'text',
            })

            await self._run_agent_turn(conv, user, turn_id=turn_id, continuation=True)
        except Exception:
            logger.exception('Recipe job continuation failed conv=%s', conv_id)
            await self.send_json({
                'type': 'error',
                'code': 'agent_error',
                'message': 'Une erreur est survenue. Réessayez.',
            })
        finally:
            release_stream_lock(conv_id)

    async def _run_agent_turn(self, conv, user, *, turn_id=None, continuation=False):
        turn_id = turn_id or str(uuid.uuid4())

        history_msgs = await database_sync_to_async(_load_history)(conv)
        close_old_connections()
        history = await database_sync_to_async(build_agent_history)(history_msgs, user)
        close_old_connections()

        agent = create_planning_agent(user)
        context = AgentContext(user=user, conversation_id=conv.id)
        adapter = BlockStreamAdapter(turn_id=turn_id)

        async def emit(event):
            payload = {
                'type': event.type,
                'turn_id': event.turn_id,
                'stream_phase': event.stream_phase,
                'block_index': event.block_index,
            }
            if continuation:
                payload['continuation'] = True
            if event.content:
                payload['content'] = event.content
            if event.tool_name:
                payload['tool_name'] = event.tool_name
            if event.summary:
                payload['summary'] = event.summary
            if event.result is not None:
                payload['result'] = event.result
            if event.request is not None:
                payload['request'] = event.request
            if event.code:
                payload['code'] = event.code
            if event.message:
                payload['message'] = event.message
            await self.send_json(payload)

        result = await adapter.run(agent, history, context, emit=emit)
        assistant_msg_id = await self._finalize_agent_result(
            conv, turn_id, result, continuation=continuation
        )

        await database_sync_to_async(_touch_conversation)(conv)
        close_old_connections()

        await self.send_json({
            'type': 'message_complete',
            'turn_id': turn_id,
            'stream_phase': 'complete',
            'message_id': assistant_msg_id,
            'continuation': continuation,
        })

        if not continuation:
            generate_conversation_title.delay(conv.id)

    async def _finalize_agent_result(self, conv, turn_id, result, *, continuation=False):
        if result.mutation_proposal:
            msg, action = await database_sync_to_async(_save_mutation)(
                conv, turn_id, result.mutation_proposal, result.mutation_tool_name
            )
            close_old_connections()
            await self.send_json({
                'type': 'mutation_card',
                'turn_id': turn_id,
                'stream_phase': 'mutation',
                'action_id': str(action.id),
                'message_id': msg.id,
                'payload': result.mutation_proposal.model_dump(),
                'continuation': continuation,
            })
            return msg.id

        ui_payload = None
        if result.interrupted:
            ui_payload = {'interrupted': True}
        if result.import_job:
            ui_payload = ui_payload or {}
            ui_payload['import_job'] = result.import_job.model_dump()
        if result.content_blocks:
            ui_payload = ui_payload or {}
            ui_payload['content_blocks'] = result.content_blocks
        if continuation:
            ui_payload = ui_payload or {}
            ui_payload['continuation'] = True

        assistant_msg = await database_sync_to_async(_save_assistant_message)(
            conv, turn_id, result.assistant_text, result.tool_traces, ui_payload
        )
        close_old_connections()

        if result.import_job:
            await self.send_json({
                'type': 'import_job_started',
                'turn_id': turn_id,
                'stream_phase': 'tool',
                'request_id': result.import_job.request_id,
                'url': result.import_job.url,
                'idea_text': result.import_job.idea_text,
                'job_type': result.import_job.job_type,
                'continuation': continuation,
            })

        return assistant_msg.id

    async def _handle_confirm_action(self, content, user):
        action_id = content.get('action_id')
        if not action_id:
            return

        try:
            result, system_msg = await database_sync_to_async(_confirm_action)(user, action_id)
            close_old_connections()
        except ValueError as exc:
            await self.send_json({
                'type': 'error',
                'code': 'action_error',
                'message': str(exc),
            })
            return

        await self.send_json({
            'type': 'action_executed',
            'action_id': action_id,
            'result': result,
            'message_id': system_msg.id,
        })

    async def _handle_cancel_action(self, content, user):
        action_id = content.get('action_id')
        if not action_id:
            return

        cancelled = await database_sync_to_async(_cancel_action)(user, action_id)
        close_old_connections()
        if cancelled:
            await self.send_json({
                'type': 'action_cancelled',
                'action_id': action_id,
            })

    async def conversation_title_updated(self, event):
        await self.send_json({
            'type': 'conversation_title_updated',
            'title': event.get('title'),
        })


def _save_recipe_ready_system_event(conv, event):
    content = build_recipe_job_ready_system_content(event)
    return Message.objects.create(
        conversation=conv,
        role=Message.ROLE_SYSTEM,
        message_type=Message.TYPE_SYSTEM_EVENT,
        content=content,
        ui_payload={
            'event': 'recipe_job_ready',
            'hidden': True,
            'request_id': event.get('request_id'),
            'recipe_id': event.get('recipe_id'),
        },
    )


def _normalize_context_refs(raw):
    if not raw or not isinstance(raw, list):
        return None
    refs = []
    for item in raw[:8]:
        if not isinstance(item, dict):
            continue
        kind = item.get('type')
        ref_id = item.get('id')
        label = (item.get('label') or '').strip()
        if kind not in ('recipe', 'meal_plan', 'shopping_list'):
            continue
        if ref_id is None:
            continue
        entry = {'type': kind, 'id': ref_id, 'label': label[:200]}
        meta = item.get('meta')
        if isinstance(meta, dict):
            clean_meta = {}
            for mk, mv in list(meta.items())[:20]:
                if mv is None or mv == '':
                    continue
                if isinstance(mv, (str, int, float, bool)):
                    clean_meta[str(mk)[:40]] = mv
            if clean_meta:
                entry['meta'] = clean_meta
        refs.append(entry)
    return refs or None


def _save_user_message(conv, text, context_refs=None):
    ui_payload = {'context_refs': context_refs} if context_refs else None
    return Message.objects.create(
        conversation=conv,
        role=Message.ROLE_USER,
        message_type=Message.TYPE_TEXT,
        content=text,
        ui_payload=ui_payload,
    )


def _load_history(conv):
    msgs = list(
        Message.objects.filter(conversation=conv)
        .order_by('-created_at')[:HISTORY_LIMIT]
    )
    msgs.reverse()
    return msgs


def _save_assistant_message(conv, turn_id, text, tool_traces, ui_payload):
    payload = ui_payload or {}
    if tool_traces:
        payload['tool_traces'] = tool_traces
    return Message.objects.create(
        conversation=conv,
        role=Message.ROLE_ASSISTANT,
        message_type=Message.TYPE_TEXT,
        content=text,
        turn_id=turn_id,
        ui_payload=payload if payload else None,
    )


def _save_mutation(conv, turn_id, proposal, tool_name):
    action_type_map = {
        'propose_meal_deletion': PendingAction.ACTION_MEAL_DELETION,
        'send_invitation_proposal': PendingAction.ACTION_MEAL_INVITATION,
    }
    action_type = action_type_map.get(tool_name, PendingAction.ACTION_MEAL_DELETION)

    msg = Message.objects.create(
        conversation=conv,
        role=Message.ROLE_ASSISTANT,
        message_type=Message.TYPE_MUTATION_PROPOSAL,
        content=proposal.subtitle,
        turn_id=turn_id,
        ui_payload=proposal.model_dump(),
    )
    action = PendingAction.objects.create(
        conversation=conv,
        message=msg,
        action_type=action_type,
        payload=proposal.details,
        status=PendingAction.STATUS_PENDING,
        expires_at=default_action_expiry(),
    )
    return msg, action


def _touch_conversation(conv):
    Conversation.objects.filter(id=conv.id).update(updated_at=timezone.now())


def _confirm_action(user, action_id):
    action = (
        PendingAction.objects.select_related('conversation', 'conversation__user')
        .filter(id=action_id, conversation__user=user)
        .first()
    )
    if not action:
        raise ValueError('Action introuvable.')
    result = execute_pending_action(user, action)
    system_msg = Message.objects.create(
        conversation=action.conversation,
        role=Message.ROLE_SYSTEM,
        message_type=Message.TYPE_SYSTEM_EVENT,
        content=f'Action confirmée: {action.action_type}',
        ui_payload={'action_id': str(action.id), 'result': result},
    )
    return result, system_msg


def _cancel_action(user, action_id):
    action = PendingAction.objects.filter(
        id=action_id,
        conversation__user=user,
        status=PendingAction.STATUS_PENDING,
    ).first()
    if not action:
        return False
    if action.mark_expired_if_needed():
        return False
    action.status = PendingAction.STATUS_CANCELLED
    action.save(update_fields=['status'])
    return True
