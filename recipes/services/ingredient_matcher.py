"""
Service pour identifier et créer des ingrédients par correspondance textuelle exacte.
Les embeddings restent disponibles pour la recherche de recettes, pas pour fusionner des ingrédients.
"""
import logging
import time
from typing import List, Literal, Optional, Tuple

import requests
from decouple import config
from django.conf import settings as django_settings
from django.db import transaction
from unidecode import unidecode

from ..models import Ingredient

logger = logging.getLogger(__name__)

# Configuration de l'API d'embedding (recherche de recettes uniquement)
EMBEDDING_API_URL = config('EMBEDDING_API_URL', default='http://localhost:8001')
EMBEDDING_API_SECRET = config('EMBEDDING_API_SECRET', default='')


def normalize_ingredient_name(name: str) -> str:
    """
    Normalise le nom d'un ingrédient pour la comparaison textuelle :
    lowercase, sans accents, espaces normalisés.
    """
    normalized = name.lower().strip()
    normalized = unidecode(normalized)
    return ' '.join(normalized.split())


def get_batch_embeddings(
    texts: List[str],
    *,
    input_type: Literal['query', 'passage'] = 'passage',
    dimensions: int | None = None,
    timeout: float | None = None,
) -> List[Optional[list]]:
    """Récupère les embeddings de plusieurs textes en une seule requête (batch)."""
    if not EMBEDDING_API_SECRET:
        logger.warning("EMBEDDING_API_SECRET non configuré, impossible de générer des embeddings")
        return [None] * len(texts)

    if not texts:
        return []

    dims = dimensions or getattr(django_settings, 'EMBEDDING_DIMENSION', 512)
    req_timeout = timeout if timeout is not None else (15.0 if input_type == 'query' else 30.0)

    try:
        start_time = time.perf_counter()
        logger.info(
            "[Embeddings][Batch] %d textes type=%s dim=%s",
            len(texts),
            input_type,
            dims,
        )
        response = requests.post(
            f"{EMBEDDING_API_URL}/embed/batch",
            headers={
                "X-API-Key": EMBEDDING_API_SECRET,
                "Content-Type": "application/json"
            },
            json={
                "texts": texts,
                "normalize": True,
                "input_type": input_type,
                "dimensions": dims,
            },
            timeout=req_timeout,
        )
        response.raise_for_status()
        data = response.json()
        embeddings = data.get("embeddings", [])
        duration_ms = (time.perf_counter() - start_time) * 1000
        logger.info(
            "[Embeddings][Batch] %d embeddings générés en %.1f ms (dimension=%s)",
            len(embeddings),
            duration_ms,
            len(embeddings[0]) if embeddings else "n/a"
        )
        return embeddings
    except requests.exceptions.RequestException as e:
        logger.error("[Embeddings][Batch] Erreur (%s): %s", type(e).__name__, e)
        return [None] * len(texts)


def get_embedding(
    text: str,
    *,
    input_type: Literal['query', 'passage'] = 'passage',
) -> Optional[list]:
    """Récupère l'embedding d'un texte via l'API d'embedding."""
    if not EMBEDDING_API_SECRET:
        logger.warning("EMBEDDING_API_SECRET non configuré, impossible de générer des embeddings")
        return None

    dims = getattr(django_settings, 'EMBEDDING_DIMENSION', 512)
    req_timeout = 15.0 if input_type == 'query' else 10.0

    try:
        start_time = time.perf_counter()
        truncated_text = text[:60] + ("..." if len(text) > 60 else "")
        logger.info("[Embeddings][Single] Génération pour '%s' type=%s", truncated_text, input_type)
        response = requests.post(
            f"{EMBEDDING_API_URL}/embed",
            headers={
                "X-API-Key": EMBEDDING_API_SECRET,
                "Content-Type": "application/json"
            },
            json={
                "text": text,
                "normalize": True,
                "input_type": input_type,
                "dimensions": dims,
            },
            timeout=req_timeout,
        )
        response.raise_for_status()
        data = response.json()
        embedding = data.get("embedding")
        duration_ms = (time.perf_counter() - start_time) * 1000
        logger.info(
            "[Embeddings][Single] Embedding généré (dimension=%s) en %.1f ms",
            len(embedding) if embedding else "n/a",
            duration_ms
        )
        return embedding
    except requests.exceptions.RequestException as e:
        logger.error("[Embeddings][Single] Erreur (%s): %s", type(e).__name__, e)
        return None


def find_ingredient_by_name(ingredient_name: str) -> Optional[Ingredient]:
    """Recherche un ingrédient par correspondance exacte (casse) ou normalisée (lower, sans accents)."""
    normalized_name = normalize_ingredient_name(ingredient_name)

    exact_match = Ingredient.objects.filter(name__iexact=ingredient_name).first()
    if exact_match:
        return exact_match

    for ingredient in Ingredient.objects.all():
        if normalize_ingredient_name(ingredient.name) == normalized_name:
            return ingredient

    return None


@transaction.atomic
def get_or_create_ingredient(ingredient_name: str) -> Tuple[Ingredient, bool]:
    """
    Récupère ou crée un ingrédient par correspondance textuelle exacte uniquement.
    Retourne (ingredient, created) où created=True si l'ingrédient a été créé.
    """
    logger.info("[IngredientMatcher] Traitement de l'ingrédient '%s'", ingredient_name)

    existing = find_ingredient_by_name(ingredient_name)
    if existing:
        logger.debug(
            "[IngredientMatcher] '%s' trouvé par correspondance textuelle (%s)",
            ingredient_name,
            existing.id,
        )
        return existing, False

    logger.info(
        "[IngredientMatcher] Création d'un nouvel ingrédient '%s' (pas de correspondance trouvée)",
        ingredient_name,
    )
    ingredient = Ingredient.objects.create(name=ingredient_name)
    try:
        from recipes.services.ingredient_categorization import ensure_ingredient_category

        ensure_ingredient_category(ingredient)
    except Exception:
        logger.exception(
            "[IngredientMatcher] Échec catégorisation pour '%s' (ingrédient créé sans catégorie)",
            ingredient_name,
        )
    return ingredient, True

