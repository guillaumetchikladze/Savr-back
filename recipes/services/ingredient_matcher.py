"""
Service pour identifier et créer des ingrédients en utilisant l'embedding
"""
import logging
import time
from typing import List, Optional, Tuple

import numpy as np
import requests
from decouple import config
from django.db import transaction
from pgvector.django import CosineDistance
from unidecode import unidecode

from ..models import Ingredient

logger = logging.getLogger(__name__)

# Configuration de l'API d'embedding
EMBEDDING_API_URL = config('EMBEDDING_API_URL', default='http://localhost:8001')
EMBEDDING_API_SECRET = config('EMBEDDING_API_SECRET', default='')

# Seuil de similarité pour considérer deux ingrédients comme identiques
# Note: CosineDistance retourne une distance (0 = identique, 2 = opposé)
# On convertit en similarité: similarity = 1 - (distance / 2)
# Donc distance < 0.3 correspond à similarity > 0.85
SIMILARITY_DISTANCE_THRESHOLD = 0.3  # Distance cosinus max (correspond à ~0.85 de similarité)


def normalize_ingredient_name(name: str) -> str:
    """
    Normalise le nom d'un ingrédient pour la comparaison textuelle
    - Lowercase
    - Suppression des accents
    - Suppression des espaces multiples
    - Suppression des pluriels basiques (s, es)
    """
    # Lowercase
    normalized = name.lower().strip()
    
    # Suppression des accents
    normalized = unidecode(normalized)
    
    # Suppression des espaces multiples
    normalized = ' '.join(normalized.split())
    
    # Suppression des pluriels basiques (s, es à la fin)
    if normalized.endswith('es') and len(normalized) > 2:
        normalized = normalized[:-2]
    elif normalized.endswith('s') and len(normalized) > 1:
        normalized = normalized[:-1]
    
    return normalized


def get_batch_embeddings(texts: List[str]) -> List[Optional[list]]:
    """
    Récupère les embeddings de plusieurs textes en une seule requête (batch)
    """
    if not EMBEDDING_API_SECRET:
        logger.warning("EMBEDDING_API_SECRET non configuré, impossible de générer des embeddings")
        return [None] * len(texts)
    
    if not texts:
        return []
    
    try:
        start_time = time.perf_counter()
        logger.info("[Embeddings][Batch] Génération de %d embeddings (normalisation=%s)", len(texts), True)
        response = requests.post(
            f"{EMBEDDING_API_URL}/embed/batch",
            headers={
                "X-API-Key": EMBEDDING_API_SECRET,
                "Content-Type": "application/json"
            },
            json={
                "texts": texts,
                "normalize": True
            },
            timeout=30  # Timeout plus long pour les batch
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


def get_embedding(text: str) -> Optional[list]:
    """
    Récupère l'embedding d'un texte via l'API d'embedding
    """
    if not EMBEDDING_API_SECRET:
        logger.warning("EMBEDDING_API_SECRET non configuré, impossible de générer des embeddings")
        return None
    
    try:
        start_time = time.perf_counter()
        truncated_text = text[:60] + ("..." if len(text) > 60 else "")
        logger.info("[Embeddings][Single] Génération pour '%s'", truncated_text)
        response = requests.post(
            f"{EMBEDDING_API_URL}/embed",
            headers={
                "X-API-Key": EMBEDDING_API_SECRET,
                "Content-Type": "application/json"
            },
            json={
                "text": text,
                "normalize": True
            },
            timeout=10
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


def find_similar_ingredient(ingredient_name: str, embedding: list) -> Optional[Ingredient]:
    """
    Trouve un ingrédient similaire en utilisant la recherche vectorielle PostgreSQL (pgvector)
    Retourne l'ingrédient le plus similaire si la distance < seuil, sinon None
    """
    try:
        logger.debug("[Embeddings][Search] Recherche d'un ingrédient similaire pour '%s'", ingredient_name)
        # Utiliser la recherche vectorielle PostgreSQL avec CosineDistance
        # CosineDistance retourne une valeur entre 0 (identique) et 2 (opposé)
        # On filtre par distance et on récupère le meilleur match
        similar_ingredients = Ingredient.objects.exclude(
            embedding__isnull=True
        ).annotate(
            distance=CosineDistance('embedding', embedding)
        ).filter(
            distance__lt=SIMILARITY_DISTANCE_THRESHOLD
        ).order_by('distance')[:1]
        
        if similar_ingredients.exists():
            best_match = similar_ingredients.first()
            # Calculer la similarité pour le log (1 - distance/2)
            distance_value = float(best_match.distance)
            similarity = 1.0 - (distance_value / 2.0)
            logger.info(f"Ingrédient '{ingredient_name}' correspond à '{best_match.name}' (similarité: {similarity:.3f}, distance: {distance_value:.3f})")
            return best_match
        
        return None
    except Exception as e:
        logger.error("[Embeddings][Search] Erreur lors de la recherche pour '%s': %s", ingredient_name, e)
        # Fallback: retourner None si pgvector n'est pas disponible
        return None


@transaction.atomic
def get_or_create_ingredient(ingredient_name: str) -> Tuple[Ingredient, bool]:
    """
    Récupère ou crée un ingrédient en utilisant l'approche hybride :
    1. Normalisation textuelle et recherche exacte
    2. Si non trouvé, génération d'embedding et recherche par similarité
    3. Si toujours non trouvé, création d'un nouvel ingrédient avec embedding
    
    Retourne (ingredient, created) où created=True si l'ingrédient a été créé
    """
    logger.info("[IngredientMatcher] Traitement de l'ingrédient '%s'", ingredient_name)
    
    # Étape 1 : Normalisation textuelle et recherche exacte
    normalized_name = normalize_ingredient_name(ingredient_name)
    
    # Chercher par nom exact (insensible à la casse)
    exact_match = Ingredient.objects.filter(name__iexact=ingredient_name).first()
    if exact_match:
        logger.debug("[IngredientMatcher] '%s' trouvé par correspondance exacte (%s)", ingredient_name, exact_match.id)
        return exact_match, False
    
    # Chercher par nom normalisé
    for ingredient in Ingredient.objects.all():
        if normalize_ingredient_name(ingredient.name) == normalized_name:
            logger.debug("[IngredientMatcher] '%s' trouvé par correspondance normalisée (%s)", ingredient_name, ingredient.id)
            return ingredient, False
    
    # Étape 2 : Génération d'embedding et recherche par similarité
    embedding = get_embedding(ingredient_name)
    
    if embedding:
        similar_ingredient = find_similar_ingredient(ingredient_name, embedding)
        if similar_ingredient:
            return similar_ingredient, False
    
    # Étape 3 : Création d'un nouvel ingrédient
    logger.info("[IngredientMatcher] Création d'un nouvel ingrédient '%s' (pas de correspondance trouvée)", ingredient_name)
    ingredient = Ingredient.objects.create(
        name=ingredient_name,
        embedding=embedding  # Stocker l'embedding si disponible
    )
    
    return ingredient, True

