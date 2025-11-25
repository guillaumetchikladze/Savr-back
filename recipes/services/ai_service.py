"""
Service IA pour formaliser les recettes en utilisant PydanticAI et une intégration Google Gemini
"""
import copy
import logging
import os
import time
from decimal import Decimal
from typing import Literal, Optional, cast
from decouple import config
from pydantic_ai import Agent
from pydantic_ai.exceptions import UserError as PydanticAIUserError
from pydantic_ai.models.google import GoogleModel, GoogleModelName
from pydantic_ai.models.gemini import GeminiModel, GeminiModelName

from .pydantic_models import RecipeFormalized

logger = logging.getLogger(__name__)

# Configuration du modèle IA
AI_MODEL = config('AI_MODEL', default='gemini-2.5-flash')
AI_API_KEY = config('AI_API_KEY', default='')
GoogleProvider = Literal['google-gla', 'google-vertex', 'gateway']

DEFAULT_GOOGLE_PROVIDER: GoogleProvider = 'google-gla'
GOOGLE_PROVIDER_ALIASES: dict[str, GoogleProvider] = {
    'google': 'google-gla',
    'google-gla': 'google-gla',
    'gla': 'google-gla',
    'gemini': 'google-gla',
    'gemini-api': 'google-gla',
    'google-vertex': 'google-vertex',
    'vertex': 'google-vertex',
    'gateway': 'gateway',
}


def sanitize_model_string(name: str) -> str:
    """Nettoie la valeur AI_MODEL (espaces, commentaires inline)"""
    cleaned = (name or '').strip()
    if '#' in cleaned:
        cleaned = cleaned.split('#', 1)[0].strip()
    return cleaned


def set_google_env_from_api_key():
    """S'assure que les variables attendues par google-genai / GeminiModel sont renseignées"""
    if AI_API_KEY:
        os.environ.setdefault('GOOGLE_API_KEY', AI_API_KEY)
        os.environ.setdefault('GEMINI_API_KEY', AI_API_KEY)


def flatten_schema(schema: dict) -> dict:
    """
    Inline tous les $defs/$ref d'un JSON Schema pour éviter les erreurs
    du SDK Gemini ("Unknown name '$ref' ..."). À retirer si l'API accepte
    un jour les schémas complets ou si l'on repasse sur OpenAI.
    """
    defs = schema.get("$defs", {})

    def resolve(ref: str):
        name = ref.replace("#/$defs/", "")
        return defs.get(name, {})

    def visit(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return visit(resolve(node["$ref"]))
            return {k: visit(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [visit(i) for i in node]
        return node

    flattened = visit(schema)
    if isinstance(flattened, dict):
        flattened.pop("$defs", None)
    return flattened


def resolve_model(raw_model: str):
    """
    Construit le modèle Pydantic-AI approprié.
    - Modèles Gemini simples -> `GeminiModel`
    - Noms `models/...` ou providers explicites -> `GoogleModel`
    - Sinon fallback: laisser Pydantic gérer (OpenAI, Groq, etc.)
    """
    cleaned = sanitize_model_string(raw_model)
    if not cleaned:
        raise ValueError("AI_MODEL ne peut pas être vide.")
    
    provider_hint = None
    model_name = cleaned
    
    if ':' in cleaned:
        provider_hint, model_name = cleaned.split(':', 1)
    
    provider_key = (provider_hint or '').strip().lower()
    if not provider_key:
        provider_key = 'google'
    
    if provider_key in GOOGLE_PROVIDER_ALIASES:
        provider = GOOGLE_PROVIDER_ALIASES[provider_key]
        model_name = model_name.strip().strip('\'"')
        if not model_name:
            raise ValueError("Nom de modèle Google invalide.")
        
        # Cas provider Google standard avec nom court => utilisons GeminiModel
        if provider == 'google-gla':
            if not model_name.startswith('models/'):
                set_google_env_from_api_key()
                logger.info("[AI] Utilisation de GeminiModel (REST) '%s'", model_name)
                return GeminiModel(
                    model_name=cast(GeminiModelName, model_name),
                )
            normalized = model_name if model_name.startswith('models/') else f"models/{model_name}"
            set_google_env_from_api_key()
            logger.info("[AI] Utilisation de GoogleModel '%s' via provider '%s'", normalized, provider)
            return GoogleModel(
                model_name=cast(GoogleModelName, normalized),
                provider=provider,
            )
        
        # Providers vertex/gateway nécessitent un chemin complet déjà fourni par l'utilisateur
        set_google_env_from_api_key()
        logger.info("[AI] Utilisation de GoogleModel '%s' via provider '%s'", model_name, provider)
        return GoogleModel(
            model_name=cast(GoogleModelName, model_name),
            provider=provider,
        )
    
    # Aucun provider explicite, mais nom "gemini-..." => GeminiModel par défaut
    if cleaned.startswith('gemini-'):
        set_google_env_from_api_key()
        logger.info("[AI] Utilisation de GeminiModel (REST) '%s'", cleaned)
        return GeminiModel(
            model_name=cast(GeminiModelName, cleaned),
        )
    
    # Fallback: laisser pydantic-ai résoudre (ex: openai:gpt-4o)
    logger.info("[AI] Utilisation du modèle natif '%s'", cleaned)
    return cleaned


def create_recipe_formalization_agent() -> Agent:
    """
    Crée un agent PydanticAI pour formaliser les recettes
    """
    if not AI_API_KEY:
        raise ValueError("AI_API_KEY doit être configuré dans .env")
    
    model = resolve_model(AI_MODEL)
    
    # Créer l'agent avec le modèle de sortie
    agent = Agent(
        model=model,
        output_type=RecipeFormalized,
        system_prompt="""Tu es un expert en cuisine et en structuration de recettes. 
Ton rôle est de formaliser des recettes brutes en données structurées.

Instructions importantes:
1. Extrais et structure les ingrédients depuis le texte libre (séparé par sauts de ligne)
   - Identifie le nom de l'ingrédient (normalise-le)
   - Extrais la quantité (décimal)
   - Identifie l'unité (g, kg, ml, l, tsp, tbsp, cup, piece, pinch, clove)
   - Si l'unité n'est pas claire, utilise 'g' pour les solides et 'ml' pour les liquides

2. Extrais et structure les étapes depuis le texte libre (séparé par sauts de ligne)
   - Chaque ligne ou paragraphe est une étape
   - Si une ligne contient plusieurs actions successives, découpe-les en sous-étapes claires et numérotées (une action par sous-étape)
   - Génère un titre court pour chaque étape si pertinent
   - Nettoie et structure l'instruction
   - Détecte si un minuteur est nécessaire (mots-clés: "minutes", "cuire", "laisser", "reposer", etc.)
   - Extrais la durée du minuteur si mentionnée
   - Identifie les ingrédients utilisés dans chaque étape avec leurs quantités
   - Génère une astuce si pertinente

3. Vérifie la cohérence des quantités
   - Pour chaque ingrédient global, somme les quantités utilisées dans les étapes
   - Ajuste si nécessaire (tolérance de 5-10% pour pertes/arrondis)

4. Infère les métadonnées
   - meal_type: breakfast (petit-déj), lunch (déjeuner), dinner (dîner), snack (en-cas)
   - difficulty: easy (simple, peu d'étapes), medium (modéré), hard (complexe, techniques avancées)
   - prep_time: temps de préparation en minutes (extrait ou calculé)
   - cook_time: temps de cuisson en minutes (extrait ou calculé)
   - servings: nombre de portions (extrait ou default 4)

5. Génère un résumé des étapes (steps_summary)
   - 2-3 phrases concises résumant les étapes principales

Sois précis et structuré dans tes réponses."""
    )

    # Les modèles Gemini refusent encore les JSON Schema avec $ref.
    # On écrase donc le schéma généré par Pydantic avec une version "flattened".
    # À supprimer lorsque l'API acceptera les schémas complets (ou si l'on repasse sur OpenAI).
    agent_model = agent.model
    if isinstance(agent_model, GeminiModel):
        object_def = agent._output_schema.object_def  # type: ignore[attr-defined]
        original_schema = copy.deepcopy(object_def.json_schema)
        object_def.json_schema = flatten_schema(original_schema)
        
        toolset = getattr(agent._output_schema, 'toolset', None)  # type: ignore[attr-defined]
        if toolset and hasattr(toolset, '_tool_defs'):
            for tool_def in toolset._tool_defs:
                tool_def.parameters_json_schema = flatten_schema(copy.deepcopy(tool_def.parameters_json_schema))
    
    return agent


async def formalize_recipe(
    title: str,
    description: Optional[str],
    ingredients_text: str,
    instructions_text: str,
    servings: Optional[int] = None,
    prep_time: Optional[int] = None,
    cook_time: Optional[int] = None,
) -> RecipeFormalized:
    """
    Formalise une recette brute en utilisant l'IA
    
    Args:
        title: Titre de la recette
        description: Description optionnelle
        ingredients_text: Texte libre des ingrédients (séparés par sauts de ligne)
        instructions_text: Texte libre des instructions (séparées par sauts de ligne)
        servings: Nombre de portions (optionnel, peut être inféré)
        prep_time: Temps de préparation en minutes (optionnel, peut être inféré)
        cook_time: Temps de cuisson en minutes (optionnel, peut être inféré)
    
    Returns:
        RecipeFormalized: Recette formalisée
    """
    if not AI_API_KEY:
        raise ValueError("AI_API_KEY doit être configuré dans .env pour utiliser l'IA")
    
    agent = create_recipe_formalization_agent()
    
    # Construire le prompt avec toutes les informations
    prompt_parts = [
        f"Titre: {title}",
    ]
    
    if description:
        prompt_parts.append(f"Description: {description}")
    
    prompt_parts.append("\nIngrédients (texte libre, séparés par sauts de ligne):")
    prompt_parts.append(ingredients_text)
    
    prompt_parts.append("\nInstructions (texte libre, séparées par sauts de ligne):")
    prompt_parts.append(instructions_text)
    
    if servings:
        prompt_parts.append(f"\nNombre de portions: {servings}")
    
    if prep_time:
        prompt_parts.append(f"Temps de préparation: {prep_time} minutes")
    
    if cook_time:
        prompt_parts.append(f"Temps de cuisson: {cook_time} minutes")
    
    prompt = "\n".join(prompt_parts)
    prompt_length = len(prompt)
    
    logger.info(
        "[AI] Formalisation lancée pour '%s' (len_prompt=%d chars)",
        title,
        prompt_length
    )
    
    try:
        start_time = time.perf_counter()
        logger.info("[AI] Début de l'appel agent.run() pour '%s'", title)
        # Exécuter l'agent
        # Note: PydanticAI peut faire plusieurs appels API si la validation échoue
        result = await agent.run(prompt)
        api_call_duration = time.perf_counter() - start_time
        logger.info("[AI] agent.run() terminé pour '%s' en %.2fs (appels API possibles: 1-2 selon validation)", title, api_call_duration)
        
        # Récupérer le résultat structuré (PydanticAI utilise .output pour le résultat typé)
        formalized_recipe = result.output
        
        duration = time.perf_counter() - start_time
        logger.info(
            "[AI] Formalisation terminée pour '%s' en %.2fs (%d ingrédients, %d étapes)",
            title,
            duration,
            len(formalized_recipe.recipe_ingredients),
            len(formalized_recipe.steps)
        )
        
        return formalized_recipe
    
    except Exception as e:
        duration = time.perf_counter() - start_time if 'start_time' in locals() else 0
        logger.error(
            "[AI] Erreur pendant la formalisation de '%s' (%.2fs): %s",
            title,
            duration,
            e
        )
        raise


def verify_quantity_consistency(formalized_recipe: RecipeFormalized) -> dict:
    """
    Vérifie la cohérence des quantités entre les ingrédients globaux et les étapes
    Retourne un dictionnaire avec les écarts détectés
    """
    inconsistencies = {}
    tolerance = 0.10  # 10% de tolérance
    
    # Pour chaque ingrédient global
    for recipe_ingredient in formalized_recipe.recipe_ingredients:
        ingredient_name = recipe_ingredient.ingredient_name
        total_quantity = recipe_ingredient.quantity
        total_unit = recipe_ingredient.unit
        
        # Sommer les quantités dans les étapes
        step_total = Decimal('0')
        step_unit = None
        
        for step in formalized_recipe.steps:
            for step_ingredient in step.step_ingredients:
                if step_ingredient.ingredient_name == ingredient_name:
                    # Convertir les unités si nécessaire (simplification: même unité)
                    if step_ingredient.unit == total_unit:
                        step_total += step_ingredient.quantity
                        step_unit = step_ingredient.unit
        
        # Vérifier la cohérence
        if step_total > 0:
            difference = (total_quantity - step_total).copy_abs()
            percentage_diff = float(difference / total_quantity) if total_quantity != 0 else 0
            
            if percentage_diff > tolerance:
                inconsistencies[ingredient_name] = {
                    'recipe_total': float(total_quantity),
                    'steps_total': float(step_total),
                    'difference': float(difference),
                    'percentage_diff': percentage_diff * 100
                }
    
    return inconsistencies

