"""Spike: streaming Gemini + async tool wrapper."""

import asyncio
import time

from channels.db import database_sync_to_async
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from agents import RunContextWrapper, Runner, function_tool

from chat.services.agent_context import AgentContext
from chat.services.agent_factory import create_chat_model, create_planning_agent

User = get_user_model()


@function_tool
async def fake_slow_lookup(ctx: RunContextWrapper[AgentContext], query: str) -> str:
    """Tool factice pour tester database_sync_to_async."""

    @database_sync_to_async
    def _count_users():
        count = User.objects.count()
        close_old_connections()
        return count

    count = await _count_users()
    return f'{query}: {count} utilisateurs'


class Command(BaseCommand):
    help = 'Spike streaming OpenAI Agents SDK + Gemini + async tool wrapper'

    def add_arguments(self, parser):
        parser.add_argument('--parallel', type=int, default=2, help='Nombre de streams parallèles')

    def handle(self, *args, **options):
        parallel = options['parallel']
        self.stdout.write(f'Testing agent stream (parallel={parallel})...')
        asyncio.run(self._run_spike(parallel))

    async def _run_spike(self, parallel: int):
        user = await database_sync_to_async(User.objects.first)()
        if not user:
            self.stderr.write('No user in DB — create one first.')
            return

        model = create_chat_model()
        self.stdout.write(self.style.SUCCESS(f'Model created: {model.model!r}'))

        async def single_stream(idx: int):
            from agents import Agent

            agent = Agent[AgentContext](
                name=f'test-{idx}',
                instructions='Réponds brièvement en français. Utilise fake_slow_lookup si demandé.',
                model=model,
                tools=[fake_slow_lookup],
            )
            ctx = AgentContext(user=user, conversation_id=0)
            t0 = time.perf_counter()
            result = Runner.run_streamed(
                agent,
                input=[{'role': 'user', 'content': f'[stream {idx}] Combien d\'utilisateurs ? Utilise le tool.'}],
                context=ctx,
            )
            deltas = []
            async for event in result.stream_events():
                if event.type == 'raw_response_event':
                    data = getattr(event, 'data', None)
                    delta = getattr(data, 'delta', None) if data else None
                    if delta:
                        deltas.append(delta)
            elapsed = time.perf_counter() - t0
            return idx, len(deltas), elapsed

        tasks = [single_stream(i) for i in range(parallel)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, Exception):
                self.stderr.write(self.style.ERROR(f'FAILED: {r}'))
            else:
                idx, deltas, elapsed = r
                self.stdout.write(
                    self.style.SUCCESS(f'Stream {idx}: {deltas} deltas in {elapsed:.2f}s')
                )

        agent = create_planning_agent()
        self.stdout.write(self.style.SUCCESS(
            f'Planning agent OK — {len(agent.tools)} tools registered'
        ))
