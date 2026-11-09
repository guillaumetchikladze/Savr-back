"""
Reformulation optionnelle du plan de bataille par LLM (endpoint OpenAI-compatible du chat, `CHAT_AI_MODEL`).

Le plan déterministe reste la source de vérité : le modèle ne reçoit que des textes numérotés (libellés d'étapes,
prépa commune, phrases du résumé) et doit en renvoyer **autant, dans le même ordre**. Ordre, horaires, durées,
températures et la phrase d'horaire ne passent jamais par le LLM. Toute réponse invalide (nombre de textes, chiffre
inventé, mot interdit), erreur ou dépassement du délai court (`BATTLE_PLAN_LLM_TIMEOUT_SECONDS`) → plan déterministe.

Activation : `BATTLE_PLAN_LLM_ENABLED` (défaut False, toujours False en test). Cache par (recettes + version +
portions, textes) : `BATTLE_PLAN_POLISH_CACHE_SECONDS`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re

from decouple import config
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

CACHE_VERSION = 'v1'
MAX_TEXT_CHARS = 220
_FORBIDDEN = re.compile(r'\b(bande|strava)\b', re.I)
_NUMBER = re.compile(r'\d+')

SYSTEM_PROMPT = """Tu reformules le plan de cuisine d'un utilisateur de l'app Tchikook.
Règles strictes :
- Tu reçois un JSON {"texts": ["…", …]}. Réponds UNIQUEMENT {"texts": [...]} avec EXACTEMENT le même nombre de textes, dans le même ordre : le texte i reformule le texte i.
- Tutoiement, phrases courtes, chaleureuses, impératif (« Émince l'oignon », « Enfourne le gâteau »).
- Ne change aucun fait : ni ingrédient, ni quantité, ni durée, ni température, ni ordre. N'ajoute aucun chiffre.
- Pas d'emoji, pas de « ! », ne parle jamais de calories.
"""


def enabled() -> bool:
    return bool(getattr(settings, 'BATTLE_PLAN_LLM_ENABLED', False))


def _model() -> str:
    from recipes.services.ai_service import sanitize_model_string

    name = sanitize_model_string(getattr(settings, 'CHAT_AI_MODEL', '') or 'gemini-2.5-flash').strip().strip('\'"')
    return name[len('models/') :] if name.startswith('models/') else name


def _call_llm(system_prompt: str, prompt: str) -> str:
    """Un appel chat.completions (JSON) avec délai court et sans nouvel essai. Point de mock des tests."""
    from openai import OpenAI

    from recipes.services.ai_service import AI_API_KEY, sanitize_model_string

    api_key = sanitize_model_string(AI_API_KEY).strip().strip('\'"')
    if not api_key:
        raise RuntimeError('AI_API_KEY absent')
    client = OpenAI(
        api_key=api_key,
        base_url=config('GEMINI_OPENAI_BASE_URL', default='https://generativelanguage.googleapis.com/v1beta/openai/'),
        timeout=float(getattr(settings, 'BATTLE_PLAN_LLM_TIMEOUT_SECONDS', 6)),
        max_retries=0,
    )
    response = client.chat.completions.create(
        model=_model(),
        messages=[{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': prompt}],
        response_format={'type': 'json_object'},
        temperature=0.3,
    )
    return response.choices[0].message.content or ''


def _slots(plan: dict) -> list[tuple[str, int, str]]:
    """(section, index, champ) des textes reformulables ; la dernière phrase du résumé (horaire) est exclue."""
    slots = [('steps', i, 'label') for i in range(len(plan.get('steps') or []))]
    slots += [('day_before', i, 'label') for i in range(len(plan.get('day_before') or []))]
    slots += [('shared_prep', i, 'label') for i in range(len(plan.get('shared_prep') or []))]
    slots += [('summary', i, '') for i in range(max(0, len(plan.get('summary') or []) - 1))]
    return slots


def _get(plan: dict, slot) -> str:
    section, i, key = slot
    return plan[section][i] if not key else plan[section][i][key]


def _valid(original: str, rewritten) -> bool:
    if not isinstance(rewritten, str):
        return False
    text = rewritten.strip()
    if not text or len(text) > MAX_TEXT_CHARS or _FORBIDDEN.search(text):
        return False
    return set(_NUMBER.findall(text)) <= set(_NUMBER.findall(original))


def polish_plan(plan: dict, *, recipes_key) -> dict:
    """Plan reformulé (`polished: true`) ou plan déterministe inchangé en cas de souci."""
    if not enabled():
        return plan
    slots = _slots(plan)
    texts = [_get(plan, s) for s in slots]
    if not texts:
        return plan
    digest = hashlib.sha256(
        json.dumps({'r': recipes_key, 't': texts, 'm': _model()}, ensure_ascii=False, default=str).encode()
    ).hexdigest()
    cache_key = f'battle_plan:polish:{CACHE_VERSION}:{digest}'
    rewritten = cache.get(cache_key)
    if rewritten is None:
        try:
            raw = _call_llm(SYSTEM_PROMPT, json.dumps({'texts': texts}, ensure_ascii=False))
            rewritten = json.loads(raw).get('texts')
        except Exception as exc:  # délai, réseau, JSON : jamais d'erreur côté utilisateur
            logger.warning('battle plan polish failed: %s', exc)
            return plan
        if (
            not isinstance(rewritten, list)
            or len(rewritten) != len(texts)
            or not all(_valid(o, r) for o, r in zip(texts, rewritten, strict=True))
        ):
            logger.warning('battle plan polish rejected (invalid response)')
            return plan
        rewritten = [r.strip() for r in rewritten]
        cache.set(cache_key, rewritten, int(getattr(settings, 'BATTLE_PLAN_POLISH_CACHE_SECONDS', 7 * 24 * 3600)))

    out = copy.deepcopy(plan)
    for slot, text in zip(slots, rewritten, strict=True):
        section, i, key = slot
        if key:
            out[section][i][key] = text
        else:
            out[section][i] = text
    out['polished'] = True
    return out
