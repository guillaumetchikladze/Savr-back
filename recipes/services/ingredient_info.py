"""
Fiche produit d'un ingrédient (« Courses » : toucher un article). Contrat front : section « Contrat front » de
`.claude-work/status/*/ITEM-20261110-0010-shp2.md`.

- **Cache en base, partagé** : `IngredientInfo` (une ligne par ingrédient). Lecture = 1 requête.
- **Génération synchrone bornée** si absente : un appel chat.completions JSON (endpoint OpenAI-compatible) avec
  `INGREDIENT_INFO_MODEL` (défaut `gemini-3.5-flash`). Si le fournisseur refuse ce nom de modèle (404, ou 400 qui
  parle du modèle), avertissement en log, le refus est retenu `MODEL_REJECTED_CACHE_SECONDS` et on rejoue **une**
  fois avec `CHAT_AI_MODEL` dans le temps restant. Budget total `INGREDIENT_INFO_LLM_TIMEOUT_SECONDS` (8 s), sans
  nouvel essai SDK.
- **Validation stricte** (`parse_response`) : clés attendues, longueurs bornées, aucun propos santé / nutrition /
  calories, ni « bande » ni nom d'app de référence. Invalide / erreur / délai → `InfoUnavailable` (503
  `info_unavailable`), **rien n'est stocké**.
- **Concurrence** : verrou consultatif Postgres par ingrédient (`pg_try_advisory_lock`) : une seule génération à la
  fois, tous processus confondus ; les autres attendent la ligne (≤ budget) puis la relisent. L'unicité
  `ingredient` garantit qu'aucun doublon n'est stocké.
- Fiche `source=manual` (admin) : jamais régénérée. Tests : mocker `ingredient_info._call_llm`.
"""

from __future__ import annotations

import json
import logging
import re
import time

from decouple import config
from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError, IntegrityError, connection, transaction
from django.utils import timezone

from recipes.models import Ingredient, IngredientInfo

logger = logging.getLogger(__name__)

ADVISORY_LOCK_CLASS = 0x1F0  # espace de verrous consultatifs « fiche produit »
WAIT_POLL_SECONDS = 0.25
MODEL_REJECTED_CACHE_SECONDS = 3600
MAX_SUBSTITUTES = 5

_LIMITS = {'what': 450, 'uses': 400, 'aisle': 80, 'choose': 350, 'keep': 350}
_REQUIRED = ('what', 'uses', 'aisle')
_MIN_WHAT = 20
_MAX_SUBSTITUTE_CHARS = 60

# Allégations santé / régime refusées (règle produit) ; les faits culinaires neutres passent (« source de protéines
# végétales », « riche en protéines »). Vitamines / antioxydants = toujours présentés comme un bienfait : refusés.
# Pas de « bande » ni de nom d'app de référence. Le prompt demande en plus d'éviter de parler nutrition.
FORBIDDEN_RE = re.compile(
    r'\b('
    # calories, perte de poids
    r'calori\w*|kcal|minceur|maigrir|amaigri\w*|amincissant\w*|perdre\s+du\s+poids|perte\s+de\s+poids|'
    # santé, régime (sauf « régime de bananes »), diététique, détox
    r'sant[ée]|r[ée]gimes?(?!\s+de\s+(?:bananes?|dattes?|plantains?))|di[ée]t[ée]ti\w*|d[ée]tox\w*|'
    r'superaliment\w*|bienfaits?|vertus?|vitamines?|antioxydant\w*|'
    r'bons?\s+pour\s+(?:la|ta|le|ton|les|tes|l\W)\s*(?:sant[ée]|c(?:œ|oe)ur|ligne|peau|cerveau|os|intestins?|'
    r'transit|organisme|corps|immunit[ée]|digestion)|'
    # termes médicaux
    r'm[ée]dic\w*|maladies?|cholest[ée]rol|glyc[ée]mi\w*|diab[èe]t\w*|immunit\w*|inflammat\w*|cancers?|'
    r'anticanc\w*|gu[ée]ri(?:r|t|son)|soigner|th[ée]rapeut\w*|hypertension|digestion|'
    r'bande|strava'
    r')\b',
    re.I,
)

SYSTEM_PROMPT = """Tu écris la fiche produit d'un ingrédient pour une liste de courses (app Tchikook, France).
Réponds UNIQUEMENT en JSON avec exactement ces clés :
{"what": "…", "uses": "…", "aisle": "…", "choose": "…", "keep": "…", "substitutes": ["…"]}
- "what" : ce que c'est, 2 à 3 phrases courtes.
- "uses" : à quoi ça sert en cuisine, 1 à 2 phrases.
- "aisle" : le rayon où le trouver dans un supermarché français, en quelques mots (ex. « Fruits et légumes »,
  « Crèmerie », « Épicerie salée », « Épicerie sucrée », « Boucherie », « Poissonnerie », « Surgelés »,
  « Produits du monde », « Boulangerie », « Boissons »).
- "choose" : comment bien le choisir, 1 à 2 phrases.
- "keep" : comment le conserver, 1 à 2 phrases.
- "substitutes" : 0 à 4 ingrédients de remplacement, quelques mots chacun.
Règles : français, tutoiement, ton simple et concret, pas d'emoji, pas de « ! ». Ne parle jamais de santé, de
nutrition, de vitamines, de régime ni de calories. Si tu ne connais pas l'ingrédient, reste général et prudent."""


class InfoUnavailable(Exception):
    """Fiche impossible à produire maintenant (→ 503 `info_unavailable`)."""


class ModelRejected(Exception):
    """Le fournisseur refuse le nom de modèle demandé."""


# ---------------------------------------------------------------------------
# Appel LLM
# ---------------------------------------------------------------------------
def _clean_model(name: str) -> str:
    from recipes.services.ai_service import sanitize_model_string

    name = sanitize_model_string(name or '').strip().strip('\'"')
    return name[len('models/') :] if name.startswith('models/') else name


def primary_model() -> str:
    return _clean_model(getattr(settings, 'INGREDIENT_INFO_MODEL', '') or 'gemini-3.5-flash')


def fallback_model() -> str:
    return _clean_model(getattr(settings, 'CHAT_AI_MODEL', '') or 'gemini-2.5-flash')


def _failure_key(ingredient_id: int) -> str:
    return f'ingredient_info:failed:{ingredient_id}'


def _rejected_key(model: str) -> str:
    return f'ingredient_info:model_rejected:{model}'


def is_model_rejection(exc: Exception) -> bool:
    """404 du fournisseur, ou 400 dont le message parle du modèle (nom inconnu / non pris en charge)."""
    import openai

    if isinstance(exc, openai.NotFoundError):
        return True
    if isinstance(exc, openai.BadRequestError):
        return 'model' in str(exc).lower()
    return False


def _call_llm(model: str, system_prompt: str, prompt: str, timeout: float) -> str:
    """Un appel chat.completions (JSON), sans nouvel essai. Lève `ModelRejected` si le modèle est refusé."""
    from openai import OpenAI

    from recipes.services.ai_service import AI_API_KEY, sanitize_model_string

    api_key = sanitize_model_string(AI_API_KEY).strip().strip('\'"')
    if not api_key:
        raise RuntimeError('AI_API_KEY absent')
    client = OpenAI(
        api_key=api_key,
        base_url=config('GEMINI_OPENAI_BASE_URL', default='https://generativelanguage.googleapis.com/v1beta/openai/'),
        timeout=timeout,
        max_retries=0,
    )
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': prompt}],
            response_format={'type': 'json_object'},
            temperature=0.3,
        )
    except Exception as exc:
        if is_model_rejection(exc):
            raise ModelRejected(str(exc)) from exc
        raise
    return response.choices[0].message.content or ''


def _prompt(ingredient: Ingredient) -> str:
    payload = {'ingredient': ingredient.name}
    category = getattr(ingredient, 'category', None)
    if category is not None:
        payload['categorie_app'] = category.name
    return json.dumps(payload, ensure_ascii=False)


def _generate_raw(ingredient: Ingredient) -> tuple[str, str]:
    """(réponse brute, modèle utilisé). Modèle principal, repli sur le modèle du chat si le nom est refusé."""
    budget = float(getattr(settings, 'INGREDIENT_INFO_LLM_TIMEOUT_SECONDS', 8))
    deadline = time.monotonic() + budget
    primary, fallback = primary_model(), fallback_model()
    models = [primary] if primary == fallback else [primary, fallback]
    if len(models) == 2 and cache.get(_rejected_key(primary)):
        models = [fallback]
    prompt = _prompt(ingredient)
    for index, model in enumerate(models):
        remaining = deadline - time.monotonic()
        if remaining <= 0.5:
            raise InfoUnavailable('délai dépassé')
        try:
            return _call_llm(model, SYSTEM_PROMPT, prompt, remaining), model
        except ModelRejected as exc:
            cache.set(_rejected_key(model), 1, timeout=MODEL_REJECTED_CACHE_SECONDS)
            if index + 1 < len(models):
                logger.warning(
                    '[ingredient_info] modèle %r refusé par le fournisseur (%s) : repli sur %r',
                    model,
                    str(exc)[:200],
                    models[index + 1],
                )
                continue
            raise InfoUnavailable(f'modèle refusé : {model}') from exc
    raise InfoUnavailable('aucun modèle disponible')


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _text(data: dict, key: str) -> str:
    value = data.get(key, '')
    if value is None:
        value = ''
    if not isinstance(value, str):
        raise ValueError(f'{key} : texte attendu')
    text = ' '.join(value.split())
    if len(text) > _LIMITS[key]:
        raise ValueError(f'{key} : trop long')
    if key in _REQUIRED and not text:
        raise ValueError(f'{key} : vide')
    if FORBIDDEN_RE.search(text):
        raise ValueError(f'{key} : propos refusé')
    return text


_FENCE_RE = re.compile(r'^```(?:json)?\s*(.*?)\s*```$', re.S | re.I)


def _strip_fences(raw: str) -> str:
    """Certains modèles entourent le JSON de ```json … ``` malgré `response_format` : on l'accepte."""
    text = (raw or '').strip()
    match = _FENCE_RE.match(text)
    return match.group(1) if match else text


def parse_response(raw: str) -> dict:
    """Réponse LLM → champs validés ; ValueError si inutilisable (rien n'est alors stocké)."""
    data = json.loads(_strip_fences(raw))
    if not isinstance(data, dict):
        raise ValueError('objet JSON attendu')
    fields = {key: _text(data, key) for key in _LIMITS}
    if len(fields['what']) < _MIN_WHAT:
        raise ValueError('what : trop court')
    if '{' in fields['what'] or 'http' in fields['what'].lower():
        raise ValueError('what : contenu suspect')
    subs = data.get('substitutes') or []
    if not isinstance(subs, list):
        raise ValueError('substitutes : liste attendue')
    substitutes = []
    for item in subs[:MAX_SUBSTITUTES]:
        if not isinstance(item, str):
            raise ValueError('substitutes : textes attendus')
        text = ' '.join(item.split()).rstrip('.')
        if not text:
            continue
        if len(text) > _MAX_SUBSTITUTE_CHARS or FORBIDDEN_RE.search(text):
            raise ValueError('substitutes : propos refusé')
        substitutes.append(text)
    fields['substitutes'] = substitutes
    fields['aisle'] = fields['aisle'].rstrip('.')
    return fields


# ---------------------------------------------------------------------------
# Lecture / génération
# ---------------------------------------------------------------------------
def _try_lock(ingredient_id: int) -> bool:
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_try_advisory_lock(%s, %s)', [ADVISORY_LOCK_CLASS, ingredient_id])
        return bool(cursor.fetchone()[0])


def _unlock(ingredient_id: int) -> None:
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_unlock(%s, %s)', [ADVISORY_LOCK_CLASS, ingredient_id])
    except Exception:  # noqa: BLE001 — le verrou de session tombe de toute façon avec la connexion
        logger.warning('[ingredient_info] déverrouillage impossible ingredient=%s', ingredient_id)


def _wait_for_info(ingredient_id: int, deadline: float) -> IngredientInfo | None:
    while time.monotonic() < deadline:
        info = IngredientInfo.objects.filter(ingredient_id=ingredient_id).first()
        if info:
            return info
        time.sleep(WAIT_POLL_SECONDS)
    return IngredientInfo.objects.filter(ingredient_id=ingredient_id).first()


def get_cached(ingredient_id: int) -> IngredientInfo | None:
    return IngredientInfo.objects.filter(ingredient_id=ingredient_id).first()


def generate(ingredient: Ingredient) -> IngredientInfo:
    """Génère et stocke la fiche (verrou par ingrédient). Lève `InfoUnavailable` ; ne stocke rien d'invalide."""
    if not getattr(settings, 'INGREDIENT_INFO_LLM_ENABLED', True):
        raise InfoUnavailable('génération désactivée')
    if cache.get(_failure_key(ingredient.id)):
        # Échec récent : pas de nouvel appel LLM avant la fin du délai (coût, latence).
        raise InfoUnavailable('échec récent')
    budget = float(getattr(settings, 'INGREDIENT_INFO_LLM_TIMEOUT_SECONDS', 8))
    try:
        locked = _try_lock(ingredient.id)
    except DatabaseError as exc:
        logger.warning('[ingredient_info] verrou impossible ingredient=%s : %s', ingredient.id, exc)
        raise InfoUnavailable('verrou indisponible') from exc
    if not locked:
        # Une autre requête génère déjà cette fiche : on attend qu'elle apparaisse.
        info = _wait_for_info(ingredient.id, time.monotonic() + budget)
        if info:
            return info
        raise InfoUnavailable('génération en cours ailleurs')
    try:
        existing = get_cached(ingredient.id)
        if existing:
            return existing
        try:
            raw, model = _generate_raw(ingredient)
            fields = parse_response(raw)
        except Exception as exc:  # noqa: BLE001 — modèle refusé / réseau / délai / JSON / validation
            logger.warning(
                '[ingredient_info] fiche refusée ingredient=%s : %s: %s', ingredient.id, type(exc).__name__, exc
            )
            cooldown = int(getattr(settings, 'INGREDIENT_INFO_FAILURE_COOLDOWN_SECONDS', 600))
            if cooldown > 0:
                cache.set(_failure_key(ingredient.id), 1, timeout=cooldown)
            if isinstance(exc, InfoUnavailable):
                raise
            raise InfoUnavailable(str(exc)) from exc
        try:
            with transaction.atomic():
                return IngredientInfo.objects.create(
                    ingredient=ingredient,
                    source=IngredientInfo.SOURCE_LLM,
                    model=model[:100],
                    generated_at=timezone.now(),
                    **fields,
                )
        except IntegrityError:
            existing = get_cached(ingredient.id)
            if existing:
                return existing
            raise InfoUnavailable('écriture concurrente') from None
    finally:
        _unlock(ingredient.id)


def get_or_generate(ingredient: Ingredient) -> tuple[IngredientInfo, bool]:
    """(fiche, cached). `cached=False` si elle vient d'être générée."""
    info = get_cached(ingredient.id)
    if info:
        return info, True
    return generate(ingredient), False


def payload(info: IngredientInfo, ingredient: Ingredient, *, cached: bool) -> dict:
    category = getattr(ingredient, 'category', None)
    return {
        'ingredient': {
            'id': ingredient.id,
            'name': ingredient.name,
            'category': category.name if category is not None else None,
        },
        'what': info.what,
        'uses': info.uses,
        'aisle': info.aisle,
        'choose': info.choose,
        'keep': info.keep,
        'substitutes': list(info.substitutes or []),
        'source': info.source,
        'model': info.model,
        'generated_at': info.generated_at.isoformat() if info.generated_at else None,
        'updated_at': info.updated_at.isoformat() if info.updated_at else None,
        'cached': cached,
    }


# ---------------------------------------------------------------------------
# Pré-génération (commande + Celery)
# ---------------------------------------------------------------------------
def most_used_missing_ids(limit: int) -> list[int]:
    """Ingrédients sans fiche, les plus utilisés d'abord (lignes de recettes + lignes de courses)."""
    from django.db.models import Count

    qs = (
        Ingredient.objects.filter(info__isnull=True)
        .annotate(
            uses_count=Count('recipeingredient', distinct=True) + Count('shopping_list_items', distinct=True),
        )
        .filter(uses_count__gt=0)
        .order_by('-uses_count', 'id')
        .values_list('id', flat=True)
    )
    return list(qs[: max(limit, 0)])


def generate_many(ingredient_ids: list[int]) -> dict:
    """Génère les fiches manquantes d'une liste d'ids (ignore les échecs). Renvoie des compteurs."""
    stats = {'generated': 0, 'skipped': 0, 'failed': 0}
    by_id = Ingredient.objects.select_related('category').in_bulk(ingredient_ids)
    for ingredient in (by_id[i] for i in ingredient_ids if i in by_id):  # ordre demandé (plus utilisés d'abord)
        if get_cached(ingredient.id):
            stats['skipped'] += 1
            continue
        try:
            generate(ingredient)
            stats['generated'] += 1
        except Exception as exc:  # noqa: BLE001 — un échec ne bloque pas le lot
            stats['failed'] += 1
            logger.info('[ingredient_info] pré-génération échouée ingredient=%s : %s', ingredient.id, exc)
    return stats
