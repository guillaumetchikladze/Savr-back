"""Appel Gemini à sortie JSON structurée (même client et même modèle que l'enrichissement de recherche)."""

from __future__ import annotations

import logging

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class LLMUnavailable(RuntimeError):
    pass


def generate_structured(system_prompt: str, prompt: str, schema: type[BaseModel]) -> BaseModel:
    """Un appel `generate_content` → instance de `schema`. Lève LLMUnavailable si aucune clé n'est configurée."""
    from google import genai
    from google.genai import types

    from recipes.services.ai_service import AI_API_KEY, set_google_env_from_api_key
    from recipes.services.recipe_search_context import _gemini_model_name

    if not AI_API_KEY:
        raise LLMUnavailable('AI_API_KEY absent')
    set_google_env_from_api_key()
    client = genai.Client(api_key=AI_API_KEY)
    response = client.models.generate_content(
        model=_gemini_model_name(),
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type='application/json',
            response_schema=schema,
            temperature=0,
        ),
    )
    parsed = getattr(response, 'parsed', None)
    if parsed is not None:
        return parsed if isinstance(parsed, schema) else schema.model_validate(parsed)
    return schema.model_validate_json((response.text or '').strip() or '{}')
