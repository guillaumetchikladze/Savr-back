"""Enrichissement Gemini des métadonnées de recherche (tags FR)."""

import json
import logging

from django.conf import settings
from google import genai
from google.genai import types

from .ai_service import AI_API_KEY, AI_MODEL, sanitize_model_string, set_google_env_from_api_key
from .search_context_models import RecipeSearchContext

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Tu enrichis des recettes pour une recherche en français.
Produis des tags courts et des expressions que l'utilisateur taperait (pas de markdown).
Reste factuel par rapport au titre, description, ingrédients et résumé fournis.
Ne répète pas les étapes complètes."""


def _gemini_model_name() -> str:
    cleaned = sanitize_model_string(AI_MODEL)
    if ':' in cleaned:
        _, cleaned = cleaned.split(':', 1)
    cleaned = cleaned.strip().strip('\'"')
    if cleaned.startswith('models/'):
        cleaned = cleaned.replace('models/', '', 1)
    return cleaned


def _format_prompt(recipe) -> str:
    ingredient_names = [ri.ingredient.name for ri in recipe.recipe_ingredients.all()]
    lines = [
        f"Titre: {recipe.title}",
        f"Description: {recipe.description or ''}",
        f"Résumé: {recipe.steps_summary or ''}",
        f"Type repas: {recipe.meal_type}",
        f"Difficulté: {recipe.difficulty}",
        f"Temps prép/cuisson: {recipe.prep_time}/{recipe.cook_time} min",
        f"Ingrédients: {', '.join(ingredient_names)}",
    ]
    return '\n'.join(lines)


def generate_recipe_search_context(recipe) -> RecipeSearchContext | None:
    if not getattr(settings, 'SEARCH_CONTEXT_GEMINI_ENABLED', True):
        return None
    if not AI_API_KEY:
        logger.warning('[RecipeSearchContext] AI_API_KEY absent — skip Gemini')
        return None
    try:
        set_google_env_from_api_key()
        prompt = _format_prompt(recipe)
        client = genai.Client(api_key=AI_API_KEY)
        response = client.models.generate_content(
            model=_gemini_model_name(),
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type='application/json',
                response_schema=RecipeSearchContext,
            ),
        )
        if getattr(response, 'parsed', None) is not None:
            parsed = response.parsed
            if isinstance(parsed, RecipeSearchContext):
                return parsed
            return RecipeSearchContext.model_validate(parsed)

        text = (response.text or '').strip()
        if not text:
            logger.warning('[RecipeSearchContext] réponse vide pour recipe %s', recipe.id)
            return None
        return RecipeSearchContext.model_validate_json(text)
    except json.JSONDecodeError as exc:
        logger.exception('[RecipeSearchContext] JSON invalide recipe %s: %s', recipe.id, exc)
        return None
    except Exception as exc:
        logger.exception('[RecipeSearchContext] Échec Gemini pour recipe %s: %s', recipe.id, exc)
        return None
