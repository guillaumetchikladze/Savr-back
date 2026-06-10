"""Compatibilité tool-calling Gemini (OpenAI-compatible) + Agents SDK."""

from __future__ import annotations

import ast
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False

_GEMINI_UNSUPPORTED_CREATE_PARAMS = frozenset({
    'reasoning_effort',
    'verbosity',
    'top_logprobs',
    'prompt_cache_retention',
    'store',
    'stream_options',
    'metadata',
})


def _extract_first_json_object(raw: str) -> str | None:
    """Extrait le premier objet JSON valide d'une chaîne (ex. `{}{}` concaténés)."""
    depth = 0
    in_string = False
    escape = False
    start: int | None = None

    for i, ch in enumerate(raw):
        if escape:
            escape = False
            continue
        if ch == '\\' and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start is not None:
                candidate = raw[start:i + 1]
                try:
                    json.loads(candidate)
                    return candidate
                except json.JSONDecodeError:
                    pass
    return None


def repair_tool_arguments_json(input_json: str) -> str:
    """
    Répare les arguments tool souvent mal formés par Gemini avant json.loads du SDK.

    Cas couverts :
    - chaîne vide
    - littéral Python (guillemets simples)
    - clés non quotées
    - plusieurs objets JSON concaténés (parallel tool calls mal sérialisés)
    """
    raw = (input_json or '').strip()
    if not raw:
        return '{}'

    try:
        json.loads(raw)
        return raw
    except json.JSONDecodeError:
        pass

    if re.search(r'\}\s*\{', raw):
        first = _extract_first_json_object(raw)
        if first:
            if first != raw:
                logger.info('Repaired concatenated tool JSON: kept first object only')
            return first

    try:
        obj = ast.literal_eval(raw)
        if isinstance(obj, dict):
            return json.dumps(obj, ensure_ascii=False)
    except (ValueError, SyntaxError):
        pass

    try:
        fixed = re.sub(
            r'(\{|,)\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:',
            r'\1"\2":',
            raw,
        )
        json.loads(fixed)
        return fixed
    except (json.JSONDecodeError, re.error):
        pass

    first = _extract_first_json_object(raw)
    if first:
        return first

    return raw


def _sanitize_messages_tool_arguments(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Nettoie les arguments tool dans l'historique renvoyé à l'API."""
    sanitized: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get('role') == 'assistant' and msg.get('tool_calls'):
            msg = dict(msg)
            tool_calls = []
            for tc in msg['tool_calls']:
                tc = dict(tc)
                fn = dict(tc.get('function') or {})
                args = fn.get('arguments')
                if isinstance(args, str) and args.strip():
                    fn['arguments'] = repair_tool_arguments_json(args)
                tc['function'] = fn
                tool_calls.append(tc)
            msg['tool_calls'] = tool_calls
        sanitized.append(msg)
    return sanitized


def _sanitize_gemini_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retire `strict` et champs OpenAI-only rejetés par Gemini."""
    cleaned: list[dict[str, Any]] = []
    for tool in tools:
        tool = dict(tool)
        fn = tool.get('function')
        if isinstance(fn, dict):
            fn = dict(fn)
            fn.pop('strict', None)
            tool['function'] = fn
        cleaned.append(tool)
    return cleaned


def _is_gemini_base_url(base_url: str | None) -> bool:
    return bool(base_url and 'generativelanguage.googleapis.com' in base_url)


def install() -> None:
    """Patches légers du SDK Agents pour tolérer Gemini."""
    global _INSTALLED
    if _INSTALLED:
        return

    import agents.tool as agents_tool
    from agents.models.chatcmpl_converter import Converter
    from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel

    original_parse = agents_tool._parse_function_tool_json_input

    def patched_parse(*, tool_name: str, input_json: str):
        repaired = repair_tool_arguments_json(input_json)
        if repaired != (input_json or '').strip():
            logger.debug('Repaired tool JSON for %s', tool_name)
        return original_parse(tool_name=tool_name, input_json=repaired)

    agents_tool._parse_function_tool_json_input = patched_parse

    original_items_to_messages = Converter.items_to_messages.__func__

    @classmethod
    def patched_items_to_messages(cls, *args, **kwargs):
        result = original_items_to_messages(cls, *args, **kwargs)
        model = kwargs.get('model') or (args[2] if len(args) > 2 else None)
        if model and 'gemini' in str(model).lower():
            return _sanitize_messages_tool_arguments(result)
        return result

    Converter.items_to_messages = patched_items_to_messages

    original_tool_to_openai = Converter.tool_to_openai.__func__

    @classmethod
    def patched_tool_to_openai(cls, tool):
        result = original_tool_to_openai(cls, tool)
        fn = dict(result.get('function') or {})
        fn.pop('strict', None)
        result = dict(result)
        result['function'] = fn
        return result

    Converter.tool_to_openai = patched_tool_to_openai

    original_fetch = OpenAIChatCompletionsModel._fetch_response

    async def patched_fetch(self, *args, **kwargs):
        return await _patched_fetch_impl(original_fetch, self, *args, **kwargs)

    OpenAIChatCompletionsModel._fetch_response = patched_fetch
    _INSTALLED = True


async def _patched_fetch_impl(original_fetch, self, *args, **kwargs):
    """Intercepte _fetch_response pour nettoyer tools/params Gemini."""
    base_url = str(getattr(self._client, 'base_url', '') or '')
    if not _is_gemini_base_url(base_url):
        return await original_fetch(self, *args, **kwargs)

    # Monkey-patch temporaire sur create pour filtrer kwargs
    client = self._get_client()
    original_create = client.chat.completions.create

    async def sanitized_create(**create_kwargs):
        tools = create_kwargs.get('tools')
        if tools and tools is not ...:
            create_kwargs['tools'] = _sanitize_gemini_tools(tools)
        for key in _GEMINI_UNSUPPORTED_CREATE_PARAMS:
            create_kwargs.pop(key, None)
        return await original_create(**create_kwargs)

    client.chat.completions.create = sanitized_create
    try:
        return await original_fetch(self, *args, **kwargs)
    except Exception as exc:
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
        if status == 400:
            logger.error('Gemini chat.completions 400 — historique ou tools invalides')
        raise
    finally:
        client.chat.completions.create = original_create
