"""
Titres automatiques des conversations (historique du chat, « Tes conversations »).

Deux étages, jamais bloquants pour le chat :

1. **Tout de suite, sans réseau** : au premier message utilisateur, `apply_initial_title` pose un titre tiré
   du message (`smart_title` : salutation retirée, 1re phrase, coupe sur un mot, URL → « Recette de site.fr »).
   Une seule requête UPDATE conditionnelle (`title_source='default'`).
2. **Après le premier échange, en Celery** (`chat.tasks.generate_conversation_title`) : un appel LLM court
   (`CHAT_AI_MODEL`, délai `CHAT_TITLE_LLM_TIMEOUT_SECONDS`, sans nouvel essai) propose un titre de 2 à 6 mots ;
   réponse invalide / erreur → on garde le titre du message. `CHAT_TITLE_LLM_ENABLED` (défaut True, sans clé
   d'API → ignoré).

Un titre renommé par l'utilisateur (`title_source='user'`) n'est jamais réécrit. Tests : mocker `_call_llm`.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

from decouple import config
from django.conf import settings

from chat.models import DEFAULT_CONVERSATION_TITLE, Conversation, Message

logger = logging.getLogger(__name__)

MAX_TITLE_CHARS = 60
MAX_USER_TITLE_CHARS = 120
MIN_SENTENCE_CHARS = 12

_URL_RE = re.compile(r'https?://\S+', re.I)
_GREETING_RE = re.compile(
    r'^(?:salut|bonjour|bonsoir|coucou|hello|hey|yo|hi)\b[\s,!.:;-]*(?:(?:toi|tchikook|chef)\b[\s,!.:;-]*)?',
    re.I,
)
_SENTENCE_END_RE = re.compile(r'(?<=[.?!])\s+|\n+')
_TRAILING_PUNCT = ' ,;:-–—(['
# Copie : jamais « bande » ni nom d'app de référence, aucun propos santé / calories dans un titre généré.
_FORBIDDEN_TITLE_RE = re.compile(
    r'\b(bande|strava|calories?|kcal|minceur|maigrir|régime\s+(?:minceur|amaigrissant)|santé)\b', re.I
)

TITLE_SYSTEM_PROMPT = """Tu donnes un titre court à une conversation de cuisine dans l'app Tchikook.
Réponds UNIQUEMENT en JSON : {"title": "…"}.
Règles : 2 à 6 mots, en français, sans guillemets, sans emoji, sans point final, première lettre en majuscule.
Décris le sujet (« Idées de dîners végé », « Menu de la semaine », « Gâteau au chocolat sans four »).
Ne parle jamais de santé, de régime ni de calories."""


# ---------------------------------------------------------------------------
# Titre « cheap » depuis le premier message
# ---------------------------------------------------------------------------
def _domain(url: str) -> str:
    try:
        host = urlparse(url).hostname or ''
    except ValueError:
        return ''
    return host[4:] if host.startswith('www.') else host


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(' ')
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip(_TRAILING_PUNCT) + '…'


def smart_title(text: str, *, limit: int = MAX_TITLE_CHARS) -> str:
    """Titre lisible tiré d'un message utilisateur ; `DEFAULT_CONVERSATION_TITLE` si rien d'exploitable."""
    raw = (text or '').strip()
    if not raw:
        return DEFAULT_CONVERSATION_TITLE

    urls = _URL_RE.findall(raw)
    no_urls = _URL_RE.sub(' ', raw).strip()
    flat = ' '.join(no_urls.split())
    if urls and len(flat) < MIN_SENTENCE_CHARS:
        domain = _domain(urls[0])
        return _cut(f'Recette de {domain}' if domain else 'Recette importée', limit)

    body = _GREETING_RE.sub('', no_urls).strip() or no_urls
    parts = [' '.join(p.split()) for p in _SENTENCE_END_RE.split(body) if p.strip()]
    if parts and len(parts[0]) >= MIN_SENTENCE_CHARS:
        body = parts[0]
    body = ' '.join(body.split()).strip(_TRAILING_PUNCT).rstrip('.')
    if not body:
        return DEFAULT_CONVERSATION_TITLE
    body = body[0].upper() + body[1:]
    return _cut(body, limit)


def clean_user_title(value) -> str:
    """Titre saisi par l'utilisateur (renommage) : espaces normalisés, 1 à 120 caractères ; ValueError sinon."""
    if not isinstance(value, str):
        raise ValueError('Le titre doit être un texte.')
    title = ' '.join(value.split())
    if not title:
        raise ValueError('Le titre ne peut pas être vide.')
    if len(title) > MAX_USER_TITLE_CHARS:
        raise ValueError(f'Le titre fait au plus {MAX_USER_TITLE_CHARS} caractères.')
    return title


def apply_initial_title(conversation_id: int, user_text: str) -> str | None:
    """Pose le titre tiré du premier message si la conversation a encore le titre par défaut (idempotent)."""
    title = smart_title(user_text)
    if title == DEFAULT_CONVERSATION_TITLE:
        return None
    updated = Conversation.objects.filter(id=conversation_id, title_source=Conversation.TITLE_DEFAULT).update(
        title=title, title_source=Conversation.TITLE_AUTO
    )
    return title if updated else None


# ---------------------------------------------------------------------------
# Titre LLM (Celery, après le premier échange)
# ---------------------------------------------------------------------------
def _model() -> str:
    from recipes.services.ai_service import sanitize_model_string

    name = sanitize_model_string(getattr(settings, 'CHAT_AI_MODEL', '') or 'gemini-2.5-flash').strip().strip('\'"')
    return name[len('models/') :] if name.startswith('models/') else name


def llm_available() -> bool:
    from recipes.services.ai_service import AI_API_KEY, sanitize_model_string

    if not getattr(settings, 'CHAT_TITLE_LLM_ENABLED', True):
        return False
    return bool(sanitize_model_string(AI_API_KEY).strip().strip('\'"'))


def _call_llm(system_prompt: str, prompt: str) -> str:
    """Un appel chat.completions (JSON), délai court, sans nouvel essai. Point de mock des tests."""
    from openai import OpenAI

    from recipes.services.ai_service import AI_API_KEY, sanitize_model_string

    api_key = sanitize_model_string(AI_API_KEY).strip().strip('\'"')
    if not api_key:
        raise RuntimeError('AI_API_KEY absent')
    client = OpenAI(
        api_key=api_key,
        base_url=config('GEMINI_OPENAI_BASE_URL', default='https://generativelanguage.googleapis.com/v1beta/openai/'),
        timeout=float(getattr(settings, 'CHAT_TITLE_LLM_TIMEOUT_SECONDS', 5)),
        max_retries=0,
    )
    response = client.chat.completions.create(
        model=_model(),
        messages=[{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': prompt}],
        response_format={'type': 'json_object'},
        temperature=0.2,
    )
    return response.choices[0].message.content or ''


def parse_llm_title(raw: str) -> str:
    """Réponse LLM → titre validé ; ValueError si inutilisable."""
    data = json.loads(raw)
    title = data.get('title') if isinstance(data, dict) else None
    if not isinstance(title, str):
        raise ValueError('title manquant')
    title = ' '.join(title.split()).strip('"«»“” ').rstrip('.!')
    if not (3 <= len(title) <= MAX_TITLE_CHARS):
        raise ValueError('longueur de titre invalide')
    if _FORBIDDEN_TITLE_RE.search(title) or _URL_RE.search(title) or '{' in title:
        raise ValueError('titre refusé')
    return title[0].upper() + title[1:]


def _first_exchange(conv: Conversation) -> tuple[str, str] | None:
    msgs = list(
        Message.objects.filter(
            conversation=conv,
            role__in=[Message.ROLE_USER, Message.ROLE_ASSISTANT],
            message_type=Message.TYPE_TEXT,
        )
        .order_by('created_at', 'id')
        .values_list('role', 'content')[:6]
    )
    user_text = next((c for r, c in msgs if r == Message.ROLE_USER and (c or '').strip()), '')
    if not user_text:
        return None
    assistant_text = next((c for r, c in msgs if r == Message.ROLE_ASSISTANT and (c or '').strip()), '')
    return user_text, assistant_text


def generate_title(conversation_id: int) -> str | None:
    """
    Titre après le premier échange : LLM (si dispo et 1er échange seulement) sinon titre du message.
    Renvoie le nouveau titre s'il a changé, sinon None. Ne lève jamais.
    """
    conv = Conversation.objects.alive().filter(id=conversation_id).first()
    if not conv or conv.title_source in (Conversation.TITLE_USER, Conversation.TITLE_LLM):
        return None
    exchange = _first_exchange(conv)
    if not exchange:
        return None
    user_text, assistant_text = exchange

    changed = None
    if conv.title_source == Conversation.TITLE_DEFAULT:
        changed = apply_initial_title(conv.id, user_text)

    user_turns = Message.objects.filter(
        conversation=conv, role=Message.ROLE_USER, message_type=Message.TYPE_TEXT
    ).count()
    if user_turns > 1 or not llm_available():
        return changed

    prompt = json.dumps(
        {'user': ' '.join(user_text.split())[:600], 'assistant': ' '.join((assistant_text or '').split())[:600]},
        ensure_ascii=False,
    )
    try:
        title = parse_llm_title(_call_llm(TITLE_SYSTEM_PROMPT, prompt))
    except Exception as exc:  # noqa: BLE001 — repli silencieux sur le titre du message
        logger.info('[chat] titre LLM indisponible conv=%s : %s', conv.id, exc)
        return changed
    updated = Conversation.objects.filter(
        id=conv.id, title_source__in=[Conversation.TITLE_DEFAULT, Conversation.TITLE_AUTO]
    ).update(title=title, title_source=Conversation.TITLE_LLM)
    return title if updated else changed
