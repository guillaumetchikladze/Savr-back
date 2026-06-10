"""Adapte le stream OpenAI Agents SDK vers le protocole WS par blocs."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from agents import Runner
from agents.items import ItemHelpers

from chat.services.agent_context import AgentContext
from chat.services.async_tools import MUTATION_TOOL_NAMES
from chat.services.session_context import build_session_context_prompt
from chat.services.text_sanitize import (
    extract_tool_args_from_item,
    is_tool_args_leak,
    parse_tool_args_json,
    should_suppress_stream_delta,
    strip_tool_json_segments,
)
from chat.services.tool_schemas import ImportJobStarted, MutationProposal

logger = logging.getLogger(__name__)

STREAM_PHASE_TEXT = 'text'
STREAM_PHASE_TOOL = 'tool'
STREAM_PHASE_MUTATION = 'mutation'
STREAM_PHASE_COMPLETE = 'complete'


@dataclass
class StreamEvent:
    type: str
    turn_id: str
    stream_phase: str
    block_index: int = 0
    content: str = ''
    tool_name: str = ''
    summary: str = ''
    action_id: str = ''
    message_id: Optional[int] = None
    payload: Optional[dict] = None
    request_id: str = ''
    url: str = ''
    code: str = ''
    message: str = ''
    result: Optional[dict] = None
    request: Optional[dict] = None


@dataclass
class StreamResult:
    turn_id: str
    assistant_text: str = ''
    tool_traces: list[dict] = field(default_factory=list)
    content_blocks: list[dict] = field(default_factory=list)
    mutation_proposal: Optional[MutationProposal] = None
    mutation_tool_name: str = ''
    import_job: Optional[ImportJobStarted] = None
    interrupted: bool = False


class BlockStreamAdapter:
    """Machine à états : text → tool → text → mutation/complete."""

    def __init__(self, turn_id: Optional[str] = None):
        self.turn_id = turn_id or str(uuid.uuid4())
        self.block_index = 0
        self.stream_phase = STREAM_PHASE_TEXT
        self._text_buffer: list[str] = []
        self._pending_text: list[str] = []
        self._tool_traces: list[dict] = []
        self._ordered_blocks: list[dict] = []
        self._mutation: Optional[MutationProposal] = None
        self._mutation_tool = ''
        self._import_job: Optional[ImportJobStarted] = None

    def _evt(self, event_type: str, **kwargs) -> StreamEvent:
        return StreamEvent(
            type=event_type,
            turn_id=self.turn_id,
            stream_phase=self.stream_phase,
            block_index=self.block_index,
            **kwargs,
        )

    def _flush_pending_text_block(self) -> str:
        pending = ''.join(self._pending_text)
        self._pending_text = []
        if pending.strip() and not is_tool_args_leak(pending):
            self._ordered_blocks.append({'type': 'text', 'content': pending.strip()})
            return pending.strip()
        return ''

    async def run(
        self,
        agent,
        history: list[dict],
        context: AgentContext,
        *,
        emit: Callable[[StreamEvent], Any],
        timeout_seconds: int = 60,
    ) -> StreamResult:
        import asyncio

        await emit(self._evt('text_block_start'))

        try:
            result = Runner.run_streamed(
                agent,
                input=history,
                context=context,
                max_turns=8,
            )

            current_tool = ''
            current_tool_request: dict | None = None
            async with asyncio.timeout(timeout_seconds):
                async for event in result.stream_events():
                    if event.type == 'raw_response_event':
                        data = getattr(event, 'data', None)
                        delta = getattr(data, 'delta', None) if data else None
                        if delta and self.stream_phase == STREAM_PHASE_TEXT:
                            self._pending_text.append(delta)
                            pending = ''.join(self._pending_text)
                            if should_suppress_stream_delta(pending):
                                continue
                            self._text_buffer.append(delta)
                            await emit(self._evt('assistant_delta', content=delta))

                    elif event.type == 'run_item_stream_event':
                        item = event.item
                        item_type = getattr(item, 'type', '')

                        if item_type == 'tool_call_item':
                            leaked_args_text = ''.join(self._pending_text)
                            if self.stream_phase == STREAM_PHASE_TEXT:
                                if should_suppress_stream_delta(leaked_args_text):
                                    await emit(self._evt('text_block_clear'))
                                    if leaked_args_text:
                                        full = ''.join(self._text_buffer)
                                        if full.endswith(leaked_args_text):
                                            trimmed = full[:-len(leaked_args_text)]
                                            self._text_buffer = [trimmed] if trimmed else []
                                else:
                                    self._flush_pending_text_block()
                                await emit(self._evt('text_block_end'))
                            self.stream_phase = STREAM_PHASE_TOOL
                            self._pending_text = []
                            current_tool = getattr(item, 'name', '') or getattr(item, 'tool_name', '')
                            current_tool_request = (
                                extract_tool_args_from_item(item)
                                or parse_tool_args_json(leaked_args_text)
                            )
                            await emit(self._evt(
                                'tool_running',
                                tool_name=current_tool,
                                request=current_tool_request,
                            ))

                        elif item_type == 'tool_call_output_item':
                            output = getattr(item, 'output', None)
                            summary = _summarize_tool_output(output)
                            result_data = _serialize_tool_result(output)
                            trace = {
                                'tool_name': current_tool,
                                'summary': summary,
                                'request': current_tool_request,
                                'result': result_data,
                            }
                            self._tool_traces.append(trace)
                            self._ordered_blocks.append({'type': 'tool', **trace})

                            if current_tool in MUTATION_TOOL_NAMES and isinstance(output, MutationProposal):
                                self._mutation = output
                                self._mutation_tool = current_tool
                                self.stream_phase = STREAM_PHASE_MUTATION
                                await emit(self._evt(
                                    'tool_done',
                                    tool_name=current_tool,
                                    summary=summary,
                                    request=current_tool_request,
                                    result=result_data,
                                ))
                                break

                            if current_tool == 'import_recipe_from_url' and isinstance(output, ImportJobStarted):
                                self._import_job = output

                            await emit(self._evt(
                                'tool_done',
                                tool_name=current_tool,
                                summary=summary,
                                request=current_tool_request,
                                result=result_data,
                            ))
                            self.stream_phase = STREAM_PHASE_TEXT
                            self.block_index += 1
                            self._pending_text = []
                            await emit(self._evt('text_block_start'))
                            current_tool = ''
                            current_tool_request = None

                        elif item_type == 'message_output_item':
                            text = ItemHelpers.text_message_output(item)
                            if text and not self._text_buffer:
                                clean = strip_tool_json_segments(text)
                                if clean:
                                    self._text_buffer.append(clean)
                                    self._ordered_blocks.append({'type': 'text', 'content': clean})

        except TimeoutError:
            logger.warning('Agent stream timeout conv=%s', context.conversation_id)
            return self._build_result(interrupted=True)
        except Exception as exc:
            logger.exception('Agent stream error conv=%s', context.conversation_id)
            await emit(self._evt('error', code='agent_error', message=str(exc)))
            return self._build_result(interrupted=True)

        if self.stream_phase == STREAM_PHASE_TEXT:
            self._flush_pending_text_block()
            await emit(self._evt('text_block_end'))

        return self._build_result()

    def _build_result(self, *, interrupted: bool = False) -> StreamResult:
        assistant_text = strip_tool_json_segments(''.join(self._text_buffer))
        return StreamResult(
            turn_id=self.turn_id,
            assistant_text=assistant_text,
            tool_traces=self._tool_traces,
            content_blocks=self._ordered_blocks,
            mutation_proposal=self._mutation,
            mutation_tool_name=self._mutation_tool,
            import_job=self._import_job,
            interrupted=interrupted,
        )


def _serialize_tool_result(output) -> Optional[dict]:
    if output is None:
        return None
    if hasattr(output, 'model_dump'):
        return output.model_dump()
    return None


def _summarize_tool_output(output) -> str:
    if output is None:
        return ''
    if hasattr(output, 'model_dump'):
        data = output.model_dump()
        if 'count' in data:
            return f"{data['count']} résultat(s)"
        if 'message' in data:
            return str(data['message'])
        if 'title' in data:
            return str(data['title'])
    return str(output)[:200]


def build_agent_history(messages: list, user) -> list[dict]:
    """Convertit les Message ORM en input agent + contexte session (date, semaine)."""
    history = [
        {'role': 'system', 'content': build_session_context_prompt(user)},
    ]
    for msg in messages:
        if msg.role == 'user' and msg.message_type == 'text':
            history.append({'role': 'user', 'content': msg.content})
        elif msg.role == 'assistant' and msg.message_type == 'text' and msg.content:
            clean = strip_tool_json_segments(msg.content)
            if clean:
                history.append({'role': 'assistant', 'content': clean})
        elif msg.role == 'system' and msg.message_type == 'system_event':
            history.append({'role': 'system', 'content': msg.content})
    return history
