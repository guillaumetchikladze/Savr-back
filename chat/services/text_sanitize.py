"""Nettoyage du texte assistant (fuites JSON des arguments d'outils)."""

from __future__ import annotations

import json
import re

_TOOL_ARG_KEYS = (
    '"query"',
    '"limit"',
    '"start_date"',
    '"end_date"',
    '"meal_plan_id"',
    '"recipe_ids"',
    '"recipe_batch_id"',
    '"invitee_usernames"',
    '"url"',
)


def is_tool_args_leak(text: str) -> bool:
    t = (text or '').strip()
    if not t:
        return False
    if t.startswith('{') or t.startswith('[{'):
        if any(k in t for k in _TOOL_ARG_KEYS):
            return True
    if re.search(r'\}\s*\{', t) and any(k in t for k in _TOOL_ARG_KEYS):
        return True
    return bool(re.fullmatch(r'(\{[^{}]*\})+', t) and any(k in t for k in _TOOL_ARG_KEYS))


def should_suppress_stream_delta(pending: str) -> bool:
    t = (pending or '').strip()
    if not t:
        return False
    if t.startswith('{') or t.startswith('[{'):
        return True
    if re.search(r'\}\s*\{', t):
        return True
    return is_tool_args_leak(t)


def parse_tool_args_json(text: str) -> dict | None:
    """Parse les arguments d'outil depuis le JSON streamé par le modèle."""
    t = (text or '').strip()
    if not t:
        return None
    try:
        data = json.loads(t)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r'\{[^{}]*\}', t)
    if match:
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            pass
    return None


def extract_tool_args_from_item(item) -> dict | None:
    """Extrait les arguments depuis un tool_call_item du SDK Agents."""
    for attr in ('arguments', 'args', 'input'):
        val = getattr(item, attr, None)
        if not val:
            continue
        if isinstance(val, dict):
            return val
        if isinstance(val, str):
            try:
                data = json.loads(val)
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                continue
    raw = getattr(item, 'raw_item', None)
    if raw is not None:
        fn = getattr(raw, 'function', None) or getattr(raw, 'name', None)
        if fn is not None:
            args = getattr(fn, 'arguments', None) or getattr(raw, 'arguments', None)
            if isinstance(args, str):
                return parse_tool_args_json(args)
            if isinstance(args, dict):
                return args
    return None


def strip_tool_json_segments(text: str) -> str:
    if not text:
        return ''
    result = text
    pattern = re.compile(
        r'\{[^{}]*(?:"query"|"limit"|"start_date"|"end_date"|'
        r'"meal_plan_id"|"recipe_ids"|"recipe_batch_id"|'
        r'"invitee_usernames"|"url")[^{}]*\}'
    )
    while True:
        new = pattern.sub('', result)
        if new == result:
            break
        result = new
    return re.sub(r'\s{2,}', ' ', result).strip()
