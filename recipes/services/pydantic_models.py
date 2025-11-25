"""
Modèles Pydantic pour la formalisation des recettes par l'IA
"""
from decimal import Decimal
from typing import List, Literal, Optional
from pydantic import BaseModel, Field


# Unités disponibles pour les ingrédients
UNIT_CHOICES = Literal['g', 'kg', 'ml', 'l', 'tsp', 'tbsp', 'cup', 'piece', 'pinch', 'clove']


class StepIngredientFormalized(BaseModel):
    """Ingrédient utilisé dans une étape"""
    ingredient_name: str = Field(..., description="Nom normalisé de l'ingrédient")
    quantity: Decimal = Field(..., description="Quantité utilisée dans cette étape")
    unit: UNIT_CHOICES = Field(default='g', description="Unité de mesure")


class StepFormalized(BaseModel):
    """Étape de préparation formalisée"""
    order: int = Field(..., ge=1, description="Ordre de l'étape (1-indexed)")
    title: Optional[str] = Field(None, max_length=200, description="Titre court de l'étape")
    instruction: str = Field(..., description="Instruction nettoyée et structurée")
    tip: Optional[str] = Field(None, description="Astuce ou conseil pour cette étape")
    has_timer: bool = Field(default=False, description="Cette étape nécessite un minuteur")
    timer_duration: Optional[int] = Field(None, ge=1, description="Durée du minuteur en minutes")
    step_ingredients: List[StepIngredientFormalized] = Field(
        default_factory=list,
        description="Ingrédients utilisés dans cette étape"
    )


class RecipeIngredientFormalized(BaseModel):
    """Ingrédient global de la recette"""
    ingredient_name: str = Field(..., description="Nom normalisé de l'ingrédient")
    quantity: Decimal = Field(..., description="Quantité totale pour la recette")
    unit: UNIT_CHOICES = Field(default='g', description="Unité de mesure")


class RecipeFormalized(BaseModel):
    """Recette formalisée par l'IA"""
    title: str = Field(..., max_length=200, description="Titre de la recette nettoyé")
    description: Optional[str] = Field(None, description="Description nettoyée")
    steps_summary: str = Field(..., description="Résumé concis des étapes (2-3 phrases)")
    meal_type: Literal['breakfast', 'lunch', 'dinner', 'snack'] = Field(
        default='lunch',
        description="Type de repas inféré par l'IA"
    )
    difficulty: Literal['easy', 'medium', 'hard'] = Field(
        default='medium',
        description="Difficulté inférée par l'IA"
    )
    prep_time: int = Field(..., ge=0, description="Temps de préparation en minutes")
    cook_time: int = Field(..., ge=0, description="Temps de cuisson en minutes")
    servings: int = Field(default=4, ge=1, description="Nombre de portions")
    recipe_ingredients: List[RecipeIngredientFormalized] = Field(
        default_factory=list,
        description="Liste des ingrédients globaux de la recette"
    )
    steps: List[StepFormalized] = Field(
        default_factory=list,
        description="Liste des étapes de préparation"
    )

