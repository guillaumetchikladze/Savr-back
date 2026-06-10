"""
Service IA pour formaliser les recettes en utilisant PydanticAI et une intégration Google Gemini
"""
import asyncio
import copy
import logging
import os
import time
from decimal import Decimal
from typing import Literal, Optional, cast
from decouple import config
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.exceptions import UserError as PydanticAIUserError, UnexpectedModelBehavior
from pydantic_ai.models.google import GoogleModel, GoogleModelName
from pydantic_ai.models.gemini import GeminiModel, GeminiModelName

from .pydantic_models import RecipeFormalized

logger = logging.getLogger(__name__)

# Configuration du modèle IA
AI_MODEL = config('AI_MODEL', default='gemini-3-flash-preview')
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
        # `pydantic-ai` (Google GLA provider) attend GEMINI_API_KEY.
        # D'autres libs (google-genai) utilisent souvent GOOGLE_API_KEY.
        os.environ.setdefault('GEMINI_API_KEY', AI_API_KEY)
        os.environ.setdefault('GOOGLE_API_KEY', AI_API_KEY)


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
   - IMPORTANT: ne supprime jamais un ingrédient. Chaque ligne d'ingrédient fournie en entrée doit être représentée dans `recipe_ingredients` (au pire en mettant une quantité approximative).
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
     - Si le texte dit "pour 15 crêpes" ou "donne 24 cookies", estime un nombre de personnes réaliste (par ex. 4 personnes) en te basant sur les quantités totales et les usages habituels.
     - Si le texte dit explicitement "pour 4 personnes", utilise ce nombre tel quel.

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


class IngredientNormalizationItem(BaseModel):
    original: str = Field(..., description="Nom d'ingrédient en entrée (exactement tel que reçu)")
    normalized: str = Field(..., description="Nom normalisé/canonique, sans quantité ni unité")


class IngredientNormalizationResult(BaseModel):
    items: list[IngredientNormalizationItem] = Field(
        default_factory=list,
        description="Liste des ingrédients normalisés (bijection: 1 entrée -> 1 sortie)",
    )


def create_ingredient_normalization_agent() -> Agent:
    """
    Agent dédié: normalise uniquement les noms d'ingrédients, sans changer la liste.
    """
    if not AI_API_KEY:
        raise ValueError("AI_API_KEY doit être configuré dans .env")

    model = resolve_model(AI_MODEL)

    agent = Agent(
        model=model,
        output_type=IngredientNormalizationResult,
        system_prompt="""Tu es un expert en normalisation d'ingrédients pour une application de recettes.

Objectif:
- Normaliser uniquement les NOMS d'ingrédients.
- IMPORTANT: ne supprime rien, ne fusionne pas les éléments, et ne ré-ordonne pas.
- Pour chaque `original`, retourne exactement un `normalized`.

Règles:
- Retire quantités, unités, parenthèses et qualificatifs inutiles.
- Garde le nom le plus commun en français (singulier/pluriel cohérent).
- Exemple: "2 oignons jaunes" -> normalized="oignon".
- Exemple: "huile d'olive (pour la poêle)" -> normalized="huile d'olive".

Retourne STRICTEMENT le schéma attendu.""",
    )

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


class InstagramCaptionParsed(BaseModel):
    is_recipe: bool = Field(..., description="True si le texte décrit une recette exploitable.")
    reason: str = Field(
        default="",
        description="Explication courte si ce n'est pas une recette exploitable ou si des informations manquent.",
    )
    title: str = Field(
        default="",
        description="Titre proposé pour la recette, si applicable.",
    )
    ingredients_text: str = Field(
        default="",
        description="Liste d'ingrédients, une ligne par ingrédient, idéalement préfixée par '- '.",
    )
    instructions_text: str = Field(
        default="",
        description="Étapes de la recette, une ligne par étape, idéalement numérotée '1. ...'. Peut être générée à partir du titre + ingrédients.",
    )
    servings: Optional[int] = Field(
        default=None,
        description="Nombre de portions si inférable.",
    )
    prep_time: Optional[int] = Field(
        default=None,
        description="Temps de préparation en minutes si inférable.",
    )
    cook_time: Optional[int] = Field(
        default=None,
        description="Temps de cuisson en minutes si inférable.",
    )


def create_instagram_caption_parser_agent() -> Agent:
    if not AI_API_KEY:
        raise ValueError("AI_API_KEY doit être configuré dans .env pour utiliser l'IA Instagram")

    model = resolve_model(AI_MODEL)

    agent = Agent(
        model=model,
        output_type=InstagramCaptionParsed,
        system_prompt="""
Tu es un assistant spécialisé dans l'analyse de posts Instagram pour une application de recettes.

Ton objectif est de déterminer si le texte correspond à une VRAIE recette de cuisine et,
si oui, d'en extraire une liste d'ingrédients + des étapes de préparation suffisamment complètes
pour que quelqu'un puisse cuisiner le plat.

CONDITIONS POUR is_recipe=true :
- Le texte doit clairement décrire une préparation de plat, boisson ou dessert.
- Il doit contenir une liste d'ingrédients reconnaissables (même sans quantités parfaites).
- Idéalement, il contient aussi des indications d'étapes (préparation/cuisson).

Si le texte est plutôt :
- du storytelling, de la motivation, des conseils santé très génériques,
- une simple liste d'aliments sans lien clair avec une recette,
- ou trop vague pour cuisiner concrètement,
ALORS mets is_recipe=false et explique la raison dans 'reason'.

INGRÉDIENTS (ingredients_text) :
- Extrais une liste d'ingrédients à partir du texte (caption + premier commentaire).
- Une ligne par ingrédient, au format libre, idéalement : "- 200 g de farine".
- Tu peux compléter légèrement les quantités si nécessaire, mais ne dois pas inventer une recette entière si rien n'est précisé.

ÉTAPES (instructions_text) :
- Si le texte fournit déjà des étapes, réécris-les proprement, une étape par ligne, numérotée "1. ...".
- Si le texte contient clairement une liste d'ingrédients mais presque pas d'étapes,
  GÉNÈRE une proposition réaliste d'étapes cohérentes avec le titre et les ingrédients.
- Ne génère PAS d'étapes si tu n'es pas sûr du type de plat (par ex. texte très vague ou sans ingrédients).

LANGUE :
- Si le texte est en français, garde la sortie en français.
- Sinon, reste dans la langue principale du texte.

IMPORTANT :
- Si tu n'es pas sûr que ce soit une recette exploitable, mets is_recipe=false
  et utilise 'reason' pour expliquer pourquoi (pas d'ingrédients, pas d'étapes, texte trop vague, etc.).
""",
    )

    agent_model = agent.model
    if isinstance(agent_model, GeminiModel):
        object_def = agent._output_schema.object_def  # type: ignore[attr-defined]
        original_schema = copy.deepcopy(object_def.json_schema)
        object_def.json_schema = flatten_schema(original_schema)

        toolset = getattr(agent._output_schema, "toolset", None)  # type: ignore[attr-defined]
        if toolset and hasattr(toolset, "_tool_defs"):
            for tool_def in toolset._tool_defs:
                tool_def.parameters_json_schema = flatten_schema(copy.deepcopy(tool_def.parameters_json_schema))

    return agent


def parse_instagram_caption(text: str) -> dict:
    """
    Wrapper synchrone pour parser une légende Instagram avec l'IA.
    Retourne un dict sérialisable contenant les champs d'InstagramCaptionParsed.
    """
    if not (text or "").strip():
        return {
            "is_recipe": False,
            "reason": "Texte vide.",
            "title": "",
            "ingredients_text": "",
            "instructions_text": "",
            "servings": None,
            "prep_time": None,
            "cook_time": None,
        }

    agent = create_instagram_caption_parser_agent()

    async def _run():
        result = await agent.run(text)
        parsed: InstagramCaptionParsed = result.output
        return parsed

    try:
        parsed = asyncio.run(_run())
    except Exception as e:
        logger.error("[AI] Échec du parsing de légende Instagram: %s", e, exc_info=True)
        return {
            "is_recipe": False,
            "reason": "Erreur technique lors de l'analyse de la légende Instagram.",
            "title": "",
            "ingredients_text": "",
            "instructions_text": "",
            "servings": None,
            "prep_time": None,
            "cook_time": None,
        }

    return {
        "is_recipe": parsed.is_recipe,
        "reason": parsed.reason,
        "title": parsed.title,
        "ingredients_text": parsed.ingredients_text,
        "instructions_text": parsed.instructions_text,
        "servings": parsed.servings,
        "prep_time": parsed.prep_time,
        "cook_time": parsed.cook_time,
    }


async def normalize_ingredient_names(names: list[str]) -> dict[str, str]:
    names_clean = [n.strip() for n in (names or []) if (n or '').strip()]
    if not names_clean:
        return {}

    agent = create_ingredient_normalization_agent()
    prompt = "\n".join(
        [
            "Voici une liste d'ingrédients (noms seulement).",
            "Retourne un mapping 1:1 sans perdre d'éléments.",
            "",
            "Ingrédients:",
            *[f"- {n}" for n in names_clean],
        ]
    )

    try:
        result = await agent.run(prompt)
        out: IngredientNormalizationResult = result.output
        mapping: dict[str, str] = {}
        for item in out.items:
            orig = (item.original or '').strip()
            norm = (item.normalized or '').strip()
            if not orig:
                continue
            mapping[orig] = norm or orig

        # Garantir l'identité pour les manquants
        for n in names_clean:
            mapping.setdefault(n, n)

        return mapping
    except Exception as e:
        logger.warning("[AI] Ingredient normalization failed, fallback to original names: %s", e)
        return {n: n for n in names_clean}


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
        
        # Implémentation manuelle de retries pour la validation de sortie
        # PydanticAI a un retry interne par défaut (1), mais on veut plus de tentatives
        # On fait une boucle de retry manuelle pour gérer UnexpectedModelBehavior
        max_retries = 3
        last_error = None
        
        for attempt in range(1, max_retries + 1):
            try:
                result = await agent.run(prompt)
                api_call_duration = time.perf_counter() - start_time
                logger.info(
                    "[AI] agent.run() terminé pour '%s' en %.2fs (tentative %d/%d)",
                    title,
                    api_call_duration,
                    attempt,
                    max_retries
                )
                
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
                
            except UnexpectedModelBehavior as e:
                last_error = e
                if attempt < max_retries:
                    logger.warning(
                        "[AI] Erreur de validation de sortie pour '%s' (tentative %d/%d): %s. Nouvelle tentative...",
                        title,
                        attempt,
                        max_retries,
                        e
                    )
                    # Petite pause avant de réessayer
                    await asyncio.sleep(0.5)
                else:
                    # Dernière tentative échouée
                    logger.error(
                        "[AI] Erreur de validation de sortie pour '%s' après %d tentatives: %s",
                        title,
                        max_retries,
                        e
                    )
                    raise
        
        # Ne devrait jamais arriver ici, mais au cas où
        if last_error:
            raise last_error
    
    except UnexpectedModelBehavior as e:
        # Cette exception devrait normalement être gérée dans la boucle ci-dessus
        # Mais on la garde comme filet de sécurité au cas où
        duration = time.perf_counter() - start_time if 'start_time' in locals() else 0
        logger.error(
            "[AI] Erreur de validation de sortie pour '%s' (%.2fs): %s",
            title,
            duration,
            e
        )
        raise
    except PydanticAIUserError as e:
        duration = time.perf_counter() - start_time if 'start_time' in locals() else 0
        logger.error(
            "[AI] Erreur PydanticAI pour '%s' (%.2fs): %s",
            title,
            duration,
            e
        )
        raise
    except Exception as e:
        duration = time.perf_counter() - start_time if 'start_time' in locals() else 0
        logger.error(
            "[AI] Erreur inattendue pendant la formalisation de '%s' (%.2fs): %s (type: %s)",
            title,
            duration,
            e,
            type(e).__name__
        )
        raise


def create_recipe_generation_agent() -> Agent:
    """Agent PydanticAI pour inventer une recette complète à partir d'une idée."""
    if not AI_API_KEY:
        raise ValueError("AI_API_KEY doit être configuré dans .env")

    model = resolve_model(AI_MODEL)
    agent = Agent(
        model=model,
        output_type=RecipeFormalized,
        system_prompt="""Tu es un chef cuisinier créatif et pragmatique.
Ton rôle est d'inventer une recette complète, réaliste et appétissante à partir d'une idée ou d'un concept décrit par l'utilisateur.

Instructions :
1. Propose un titre clair et engageant (pas générique).
2. Rédige une courte description (1-2 phrases).
3. Liste les ingrédients avec quantités réalistes pour le nombre de portions visé.
   - Unités : g, kg, ml, l, tsp, tbsp, cup, piece, pinch, clove
   - Quantités cohérentes avec les usages culinaires français/européens
4. Détaille les étapes dans l'ordre logique (une action principale par étape).
   - Associe les ingrédients utilisés à chaque étape avec leurs quantités
   - Active has_timer et timer_duration quand une durée est pertinente
5. Infère meal_type, difficulty, prep_time, cook_time, servings (défaut 4 si non précisé).
6. steps_summary : 2-3 phrases résumant la préparation.

Contraintes :
- Recette faisable à la maison avec des ingrédients courants en supermarché.
- Si l'utilisateur mentionne des ingrédients obligatoires, ils doivent figurer dans la recette.
- Si l'utilisateur impose un régime (végétarien, sans gluten…), respecte-le strictement.
- Sois concis dans les instructions, pas de blabla inutile.""",
    )

    agent_model = agent.model
    if isinstance(agent_model, GeminiModel):
        object_def = agent._output_schema.object_def  # type: ignore[attr-defined]
        original_schema = copy.deepcopy(object_def.json_schema)
        object_def.json_schema = flatten_schema(original_schema)

        toolset = getattr(agent._output_schema, 'toolset', None)  # type: ignore[attr-defined]
        if toolset and hasattr(toolset, '_tool_defs'):
            for tool_def in toolset._tool_defs:
                tool_def.parameters_json_schema = flatten_schema(
                    copy.deepcopy(tool_def.parameters_json_schema)
                )

    return agent


async def generate_recipe_from_idea(
    idea_text: str,
    servings: Optional[int] = None,
) -> RecipeFormalized:
    """Génère une recette structurée à partir d'une idée libre."""
    if not AI_API_KEY:
        raise ValueError("AI_API_KEY doit être configuré dans .env pour utiliser l'IA")

    idea = (idea_text or '').strip()
    if len(idea) < 3:
        raise ValueError("L'idée de recette est trop courte.")

    agent = create_recipe_generation_agent()
    prompt_parts = [f"Idée de recette : {idea}"]
    if servings:
        prompt_parts.append(f"Nombre de portions souhaité : {servings}")
    prompt = "\n".join(prompt_parts)

    logger.info("[AI] Génération recette depuis idée (len=%d)", len(prompt))
    start_time = time.perf_counter()
    max_retries = 3
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            result = await agent.run(prompt)
            formalized_recipe = result.output
            duration = time.perf_counter() - start_time
            logger.info(
                "[AI] Génération terminée en %.2fs (%d ingrédients, %d étapes)",
                duration,
                len(formalized_recipe.recipe_ingredients),
                len(formalized_recipe.steps),
            )
            return formalized_recipe
        except UnexpectedModelBehavior as e:
            last_error = e
            logger.warning(
                "[AI] Génération tentative %d/%d échouée: %s",
                attempt,
                max_retries,
                e,
            )
        except PydanticAIUserError:
            raise

    raise last_error or RuntimeError("Échec de la génération de recette")


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

