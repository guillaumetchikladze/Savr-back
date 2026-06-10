"""Factory agent OpenAI Agents SDK + bridge Gemini."""

import logging
import os

from decouple import config
from openai import AsyncOpenAI

from agents import Agent, set_tracing_disabled
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel

from chat.services.agent_context import AgentContext
from recipes.services.ai_service import sanitize_model_string
from chat.services.async_tools import (
    add_recipe_to_meal_plan,
    create_meal_plan_slot,
    get_meal_plans,
    import_recipe_from_url,
    propose_meal_deletion,
    search_recipes,
    send_invitation_proposal,
)

logger = logging.getLogger(__name__)

AI_MODEL = config('AI_MODEL', default='gemini-2.0-flash')
AI_API_KEY = config('AI_API_KEY', default='')
GEMINI_OPENAI_BASE_URL = config(
    'GEMINI_OPENAI_BASE_URL',
    default='https://generativelanguage.googleapis.com/v1beta/openai/',
)

AGENT_INSTRUCTIONS = """Tu es Savr, assistant culinaire pour planifier les repas et gérer les recettes.

Tu peux :
- Rechercher des recettes (search_recipes)
- Consulter le planning (get_meal_plans)
- Créer un créneau vide dans le planning (create_meal_plan_slot)
- Ajouter des recettes à un créneau existant (add_recipe_to_meal_plan)
- Importer une recette depuis une URL (import_recipe_from_url)
- Proposer de retirer une recette du planning (propose_meal_deletion) — l'utilisateur devra confirmer
- Proposer d'inviter des complices (send_invitation_proposal) — l'utilisateur devra confirmer

Règles :
- Réponds en français, de façon concise et utile.
- Un message système « Contexte Savr » te donne la date du jour et les bornes de semaine : utilise-le pour calculer les périodes toi-même.
- Pour « cette semaine », « demain », etc. : déduis les dates et appelle les tools sans redemander à l'utilisateur.
- Pour planifier une recette : appelle get_meal_plans ; si le créneau n'existe pas, create_meal_plan_slot puis add_recipe_to_meal_plan.
- Pour les actions sensibles (suppression, invitation), utilise UNIQUEMENT les tools propose_*.
- Ne demande jamais de confirmation textuelle pour les mutations : le tool propose_* affiche une carte UI.

Format de réponse :
- Markdown simple : **nom de recette** en gras, puces courtes. Pas de JSON, pas de paramètres techniques.
- Pour chaque recette suggérée, indique le temps total (préparation + cuisson) et la difficulté quand tu les connais — c'est utile même si les cartes outil sont visibles.
- Après search_recipes : 1 phrase d'intro + 2 à 4 suggestions max, chacune avec titre, durée et difficulté sur une ligne compacte.
- Pas de méta-commentaire (« je vais chercher… », « j'ai utilisé l'outil… ») : enchaîne directement avec le résultat utile.
- Termine par une question courte seulement si une action concrète est proposée (ex. ajouter au planning).
"""


def _api_key() -> str:
    return sanitize_model_string(AI_API_KEY).strip().strip('\'"')


def _ensure_gemini_env():
    key = _api_key()
    if key:
        os.environ.setdefault('GEMINI_API_KEY', key)
        os.environ.setdefault('OPENAI_API_KEY', key)


def normalize_model_for_gemini_openai_compat(raw: str) -> str:
    """
    Normalise AI_MODEL pour l'endpoint OpenAI-compatible Gemini.

    - Retire commentaires inline et guillemets (ex. .env mal parsé)
    - Retire le préfixe models/ si présent (rejeté par /v1beta/openai/)
    """
    name = sanitize_model_string(raw).strip().strip('\'"')
    if name.startswith('models/'):
        name = name[len('models/'):]
    return name


def create_chat_model() -> OpenAIChatCompletionsModel:
    _ensure_gemini_env()
    client = AsyncOpenAI(
        api_key=_api_key() or os.environ.get('GEMINI_API_KEY', ''),
        base_url=GEMINI_OPENAI_BASE_URL,
    )
    model_name = normalize_model_for_gemini_openai_compat(AI_MODEL)
    if not model_name:
        raise ValueError('AI_MODEL ne peut pas être vide.')
    return OpenAIChatCompletionsModel(model=model_name, openai_client=client)


def create_planning_agent() -> Agent[AgentContext]:
    if config('DEBUG', default=True, cast=bool):
        set_tracing_disabled(True)

    model = create_chat_model()
    return Agent[AgentContext](
        name='Savr Planning Agent',
        instructions=AGENT_INSTRUCTIONS,
        model=model,
        tools=[
            search_recipes,
            get_meal_plans,
            create_meal_plan_slot,
            add_recipe_to_meal_plan,
            import_recipe_from_url,
            propose_meal_deletion,
            send_invitation_proposal,
        ],
    )
