"""Factory agent OpenAI Agents SDK + bridge Gemini."""

import logging
import os

from chat.services.gemini_tool_compat import install as install_gemini_tool_compat

install_gemini_tool_compat()

from decouple import config
from openai import AsyncOpenAI

from agents import Agent, set_tracing_disabled
from agents.model_settings import ModelSettings
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel

from chat.services.agent_context import AgentContext
from chat.services.session_context import build_session_context_prompt
from recipes.services.ai_service import sanitize_model_string
from chat.services.async_tools import (
    add_recipe_to_meal_plan,
    add_shopping_list_item,
    create_meal_plan_slot,
    generate_recipe_from_idea,
    get_meal_plans,
    get_shopping_list_items,
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

AGENT_INSTRUCTIONS = """Tu es Tchikook Agent, assistant culinaire pour planifier les repas et gérer les recettes.

Tu peux :
- Rechercher des recettes (search_recipes)
- Consulter le planning (get_meal_plans)
- Créer un créneau vide dans le planning (create_meal_plan_slot)
- Ajouter des recettes à un créneau existant (add_recipe_to_meal_plan)
- Importer une recette depuis une URL (import_recipe_from_url)
- Créer une recette à partir d'une idée (generate_recipe_from_idea) — quand aucune recette existante ne convient
- Proposer de retirer une recette du planning (propose_meal_deletion) — l'utilisateur devra confirmer
- Proposer d'inviter des amis (send_invitation_proposal) — un ou plusieurs jours via meal_plan_ids — l'utilisateur devra confirmer
- Consulter une liste de courses (get_shopping_list_items)
- Ajouter un article à une liste de courses (add_shopping_list_item)

Règles :
- Réponds en français, de façon concise et utile.
- Un message système « Contexte Tchikook Agent » te donne la date du jour et les bornes de semaine : utilise-le pour calculer les périodes toi-même.
- Un préfixe « [Contexte utilisateur attaché] » dans le message utilisateur signale une recette, un repas, un ami ou une liste déjà sélectionnés — le bloc « Contenu résolu depuis Tchikook Agent » contient les détails (ingrédients, étapes, id ami, etc.). Base ta réponse dessus ; ne redemande pas ces infos et n'appelle pas search_recipes sauf si l'utilisateur cherche d'autres recettes.
- Si un ami est attaché via @ et que l'utilisateur veut l'inviter : utilise son id dans invitee_ids de send_invitation_proposal (prioritaire sur invitee_usernames).
- Questions « qui est invité », « qui vient », « qui est convié », « qui mange avec moi » : appelle get_meal_plans sur la date visée et réponds en texte via le champ invitees. N'appelle pas send_invitation_proposal pour une simple consultation.
- N'appelle send_invitation_proposal que si l'utilisateur demande explicitement d'inviter ou d'ajouter quelqu'un à un repas.
- Dans meal_plan_ids : un seul id par créneau (date + repas), jamais de doublon.
- Exception listes de courses : pour « quoi acheter », « contenu de la liste », quantités, etc., appelle TOUJOURS get_shopping_list_items (données temps réel). L'aperçu dans le contexte attaché peut être périmé et n'inclut pas les articles déjà cochés.
- Pour les tools : arguments JSON stricts (guillemets doubles), ex. {"query": "curry", "limit": 5}. N'invente pas de champs hors du schéma.
- Pour « cette semaine », « demain », etc. : déduis les dates et appelle les tools sans redemander à l'utilisateur.
- Pour planifier une recette : appelle get_meal_plans ; si le créneau n'existe pas, create_meal_plan_slot puis add_recipe_to_meal_plan.
- Si l'utilisateur demande une recette sur mesure ou qu'aucune recette du carnet ne convient : appelle generate_recipe_from_idea (ne rédige pas la recette en entier dans le chat). Une fois créée, propose de la planifier ou de la consulter.
- Un message système [Événement Tchikook Agent] signale qu'un import ou une génération est terminé : enchaîne tout de suite avec la suite (planifier via add_recipe_to_meal_plan, ou proposer de consulter la fiche). Ne relance pas d'import ni de génération pour cette recette.
- Pour les actions sensibles (suppression, invitation), utilise UNIQUEMENT les tools propose_*.
- Pour inviter sur plusieurs jours : appelle get_meal_plans pour les dates visées, puis send_invitation_proposal avec meal_plan_ids (liste d'ids).
- Ne demande jamais de confirmation textuelle pour les mutations : le tool propose_* affiche une carte UI.
- Pour les listes de courses : si une liste est attachée au message (contexte), utilise son id sans redemander. Sinon demande quelle liste ou appelle get_shopping_list_items avec l'id connu.
- Pour ajouter un article : appelle add_shopping_list_item avec shopping_list_id, ingredient_name, et quantity/unit si précisés (défaut : 1 pièce).
- Pour lister les articles : appelle get_shopping_list_items ; include_purchased=true seulement si l'utilisateur veut aussi les articles déjà cochés/achetés.
- Dans ta réponse, cite uniquement remaining_quantity (reste à acheter), jamais la quantité totale d'origine. Si count=0, dis clairement que la liste est vide ou que tout est déjà acheté — ne liste pas d'anciens ingrédients.

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


def create_planning_agent(user=None) -> Agent[AgentContext]:
    if config('DEBUG', default=True, cast=bool):
        set_tracing_disabled(True)

    instructions = AGENT_INSTRUCTIONS
    if user is not None:
        instructions = f'{instructions}\n\n{build_session_context_prompt(user)}'

    model = create_chat_model()
    return Agent[AgentContext](
        name='Tchikook Agent',
        instructions=instructions,
        model=model,
        model_settings=ModelSettings(parallel_tool_calls=False),
        tools=[
            search_recipes,
            get_meal_plans,
            create_meal_plan_slot,
            add_recipe_to_meal_plan,
            generate_recipe_from_idea,
            import_recipe_from_url,
            propose_meal_deletion,
            send_invitation_proposal,
            get_shopping_list_items,
            add_shopping_list_item,
        ],
    )
