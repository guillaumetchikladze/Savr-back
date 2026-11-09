"""
« Adapter pour chez moi » : copie privée d'une recette réécrite par le LLM pour la cuisine de l'utilisateur
(airfryer au lieu du four, cocotte au lieu du Cookeo…). Contrat front : `.claude-work/specs/equipment.md`.

- **Synchrone, borné** : un seul appel chat.completions (endpoint OpenAI-compatible, `CHAT_AI_MODEL`), délai
  `EQUIPMENT_ADAPT_LLM_TIMEOUT_SECONDS` (8 s), sans nouvel essai → la requête ne dépasse jamais ~10 s. Pas de tâche
  Celery ni de polling : le résultat est mis en base et renvoyé tout de suite.
- **Idempotent** par (recette d'origine, utilisateur, empreinte des équipements) : contrainte unique
  `recipe_unique_equipment_adaptation` ; une adaptation existante est renvoyée sans appel LLM.
- Le LLM ne réécrit que les étapes, temps, températures (titre + note) ; les ingrédients sont copiés tels quels
  (un changement indispensable est expliqué dans la note). Réponse invalide, erreur ou délai → `adaptation_failed`,
  rien n'est créé.
- Copie : `is_public=False`, `created_by` = l'utilisateur, `adapted_from`, `adapted_for_equipment`, index de
  recherche repris de l'original. Ses équipements requis = ceux que la cuisine possède (`required_equipment_override`).
- Activation : `EQUIPMENT_ADAPT_ENABLED` (défaut True). Tests : mocker `equipment_adaptation._call_llm`.
"""

from __future__ import annotations

import json
import logging
import re

from decouple import config
from django.conf import settings
from django.db import IntegrityError, transaction

from recipes.models import Recipe, RecipeIngredient, Step, StepIngredient
from recipes.services import equipment_catalog as catalog
from recipes.services import equipment_service as equipment

logger = logging.getLogger(__name__)

MAX_STEPS = 40
MAX_INSTRUCTION_CHARS = 1200
MAX_TITLE_CHARS = 120
_FORBIDDEN = re.compile(r'\b(bande|strava)\b', re.I)

SYSTEM_PROMPT = """Tu adaptes une recette de l'app Tchikook pour la cuisine d'un utilisateur.
Règles strictes :
- Réponds UNIQUEMENT en JSON : {"title": "…", "note": "…", "prep_time": 15, "cook_time": 30, "steps": [{"title": "…", "instruction": "…", "tip": "…", "timer_duration": 12}]}.
- N'utilise que les équipements listés dans "available". N'utilise jamais ceux de "missing" et ne les cite pas.
- Change seulement ce qu'il faut : ustensiles, étapes, temps, températures. Garde les mêmes ingrédients et quantités ; si un ingrédient doit vraiment changer, dis-le dans "note".
- "title" : le titre d'origine + la version (« Gâteau au chocolat à l'airfryer »). "note" : une phrase qui résume l'adaptation (« Cuit à l'airfryer au lieu du four, en deux fournées. »).
- timer_duration en minutes ou null. prep_time et cook_time en minutes.
- Tutoiement, phrases courtes, impératif. Pas d'emoji, pas de « ! ». Ne parle jamais de calories.
"""


class AdaptationError(Exception):
    def __init__(self, message: str, status_code: int = 400, code: str = 'invalid'):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


def _model() -> str:
    from recipes.services.ai_service import sanitize_model_string

    name = sanitize_model_string(getattr(settings, 'CHAT_AI_MODEL', '') or 'gemini-2.5-flash').strip().strip('\'"')
    return name[len('models/') :] if name.startswith('models/') else name


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
        timeout=float(getattr(settings, 'EQUIPMENT_ADAPT_LLM_TIMEOUT_SECONDS', 8)),
        max_retries=0,
    )
    response = client.chat.completions.create(
        model=_model(),
        messages=[{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': prompt}],
        response_format={'type': 'json_object'},
        temperature=0.3,
    )
    return response.choices[0].message.content or ''


# ---------------------------------------------------------------------------
# Prompt et validation
# ---------------------------------------------------------------------------
def _prompt(recipe: Recipe, kitchen: catalog.Kitchen, missing: list[dict]) -> str:
    steps = list(recipe.steps.order_by('order'))
    lines = list(recipe.recipe_ingredients.select_related('ingredient').order_by('id'))
    available = [catalog.label(k) for k in catalog.sort_keys(kitchen.keys)]
    if kitchen.has('plaques'):
        available = [
            f'{a} ({kitchen.burners} feux{", induction" if kitchen.induction else ""})'
            if a == catalog.label('plaques')
            else a
            for a in available
        ]
    payload = {
        'recipe': {
            'title': recipe.title,
            'servings': recipe.servings,
            'prep_time': recipe.prep_time,
            'cook_time': recipe.cook_time,
            'ingredients': [f'{float(li.quantity or 0):g} {li.unit} {li.ingredient.name}'.strip() for li in lines],
            'steps': [
                {
                    'order': i + 1,
                    'title': s.title,
                    'instruction': s.instruction,
                    'tip': s.tip,
                    'timer_duration': s.timer_duration if s.has_timer else None,
                }
                for i, s in enumerate(steps)
            ],
        },
        'available': available,
        'missing': [m['label'] for m in missing],
    }
    return json.dumps(payload, ensure_ascii=False)


def _text(value, limit: int) -> str:
    if value is None:
        return ''
    if not isinstance(value, str):
        raise ValueError('texte attendu')
    text = ' '.join(value.split())
    if len(text) > limit or _FORBIDDEN.search(text):
        raise ValueError('texte refusé')
    return text


def _minutes(value, default: int | None) -> int | None:
    if isinstance(value, bool) or value in (None, ''):
        return default
    try:
        minutes = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return minutes if 0 <= minutes <= 24 * 60 else default


def parse_response(raw: str, recipe: Recipe) -> dict:
    """Réponse LLM → données validées ; lève ValueError si inutilisable."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('objet JSON attendu')
    steps_raw = data.get('steps')
    if not isinstance(steps_raw, list) or not 1 <= len(steps_raw) <= MAX_STEPS:
        raise ValueError('étapes manquantes')
    steps = []
    for entry in steps_raw:
        if not isinstance(entry, dict):
            raise ValueError('étape invalide')
        instruction = _text(entry.get('instruction'), MAX_INSTRUCTION_CHARS)
        if not instruction:
            raise ValueError('instruction vide')
        timer = _minutes(entry.get('timer_duration'), None)
        steps.append(
            {
                'title': _text(entry.get('title'), 200),
                'instruction': instruction,
                'tip': _text(entry.get('tip'), 400),
                'timer_duration': timer or None,
            }
        )
    title = _text(data.get('title'), MAX_TITLE_CHARS) or f'{recipe.title} (adaptée)'
    return {
        'title': title[:200],
        'note': _text(data.get('note'), 300),
        'prep_time': _minutes(data.get('prep_time'), recipe.prep_time),
        'cook_time': _minutes(data.get('cook_time'), recipe.cook_time),
        'steps': steps,
    }


# ---------------------------------------------------------------------------
# Copie
# ---------------------------------------------------------------------------
def _create_copy(recipe: Recipe, user, kitchen: catalog.Kitchen, adapted: dict) -> Recipe:
    from recipes.services.equipment_inference import infer_equipment

    note = adapted['note']
    description = (recipe.description or '').strip()
    if note:
        description = f'{note}\n\n{description}'.strip() if description else note
    copy = Recipe.objects.create(
        title=adapted['title'],
        description=description,
        steps_summary=recipe.steps_summary,
        meal_type=recipe.meal_type,
        difficulty=recipe.difficulty,
        prep_time=adapted['prep_time'] if adapted['prep_time'] is not None else recipe.prep_time,
        cook_time=adapted['cook_time'] if adapted['cook_time'] is not None else recipe.cook_time,
        servings=recipe.servings,
        image_path=recipe.image_path,
        cuisine=recipe.cuisine,
        created_by=user,
        is_public=False,
        source_type='user_created',
        # Même plat : l'index de recherche de l'original reste valable (pas de réindexation réseau ici).
        embedding=recipe.embedding,
        search_context_tags=recipe.search_context_tags,
        search_index_text=recipe.search_index_text,
        search_index_hash=recipe.search_index_hash,
        search_indexed_at=recipe.search_indexed_at,
        search_index_status=recipe.search_index_status,
        adapted_from=recipe,
        adapted_for_equipment=catalog.sort_keys(kitchen.keys),
        adaptation_signature=kitchen.signature,
    )
    for line in recipe.recipe_ingredients.order_by('id'):
        RecipeIngredient.objects.create(
            recipe=copy,
            ingredient_id=line.ingredient_id,
            quantity=line.quantity,
            unit=line.unit,
            sub_recipe_id=line.sub_recipe_id,
            default_mode=line.default_mode,
            sub_recipe_ratio=line.sub_recipe_ratio,
        )
    original_steps = list(recipe.steps.order_by('order').prefetch_related('step_ingredients'))
    same_shape = len(original_steps) == len(adapted['steps'])
    for i, data in enumerate(adapted['steps']):
        step = Step.objects.create(
            recipe=copy,
            order=i,
            title=data['title'],
            instruction=data['instruction'],
            tip=data['tip'],
            has_timer=bool(data['timer_duration']),
            timer_duration=data['timer_duration'],
        )
        if same_shape:  # même découpage : les ingrédients de chaque étape suivent
            StepIngredient.objects.bulk_create(
                [
                    StepIngredient(step=step, ingredient_id=si.ingredient_id, quantity=si.quantity, unit=si.unit)
                    for si in original_steps[i].step_ingredients.all()
                ]
            )
    # Faite pour cette cuisine : ses équipements requis = ceux, inférés, que la cuisine possède.
    inferred, _ = infer_equipment(copy.title, [(s['title'], s['instruction']) for s in adapted['steps']])
    copy.required_equipment_override = [k for k in inferred if kitchen.has(k)]
    copy.save(update_fields=['required_equipment_override'])
    return copy


def existing_adaptation(recipe_id: int, user, kitchen: catalog.Kitchen | None) -> Recipe | None:
    if kitchen is None:
        return None
    return Recipe.objects.filter(
        adapted_from_id=recipe_id, created_by=user, adaptation_signature=kitchen.signature
    ).first()


def adapt_recipe_for_user(user, recipe_id) -> tuple[Recipe, bool, list[dict]]:
    """(copie adaptée, créée maintenant ?, ce qui manquait). Lève `AdaptationError`."""
    from recipes.services.battle_plan_service import visible_recipes

    if not equipment.adaptation_enabled():
        raise AdaptationError('L’adaptation n’est pas disponible pour le moment.', 503, 'adaptation_disabled')
    try:
        recipe_id = int(recipe_id)
    except (TypeError, ValueError) as exc:
        raise AdaptationError('Recette introuvable.', 404, 'not_found') from exc
    recipe = visible_recipes(user).filter(pk=recipe_id).distinct().first()
    if recipe is None:
        raise AdaptationError('Recette introuvable.', 404, 'not_found')
    kitchen = equipment.effective_kitchen(user)
    if kitchen is None:
        raise AdaptationError('Dis-nous d’abord ce que tu as dans ta cuisine.', 400, 'equipment_not_set')
    if recipe.required_equipment is None:
        equipment.refresh_recipe_equipment(recipe)
    missing = equipment.recipe_missing(recipe, kitchen)
    existing = existing_adaptation(recipe.id, user, kitchen)
    if existing is not None:
        return existing, False, missing
    if not missing:
        raise AdaptationError('Rien à adapter : tu as déjà tout ce qu’il faut.', 400, 'nothing_to_adapt')

    try:
        adapted = parse_response(_call_llm(SYSTEM_PROMPT, _prompt(recipe, kitchen, missing)), recipe)
    except Exception as exc:  # délai, réseau, JSON, validation : rien n'est créé
        logger.warning('equipment adaptation failed for recipe %s: %s', recipe.id, exc)
        raise AdaptationError(
            'L’adaptation n’a pas marché, réessaie dans un instant.', 502, 'adaptation_failed'
        ) from exc

    try:
        with transaction.atomic(), equipment.deferred_equipment_refresh():
            copy = _create_copy(recipe, user, kitchen, adapted)
    except IntegrityError:  # doublon concurrent : l'autre requête a gagné
        existing = existing_adaptation(recipe.id, user, kitchen)
        if existing is None:
            raise
        return existing, False, missing
    return copy, True, missing


def adaptation_payload(copy: Recipe, created: bool, missing: list[dict], context: dict | None = None) -> dict:
    from recipes.serializers import RecipeDetailSerializer

    return {
        'created': created,
        'original_recipe_id': copy.adapted_from_id,
        'adapted_for': catalog.items_payload({'keys': copy.adapted_for_equipment or []}),
        'missing': missing,
        'recipe': RecipeDetailSerializer(copy, context=context or {}).data,
    }
