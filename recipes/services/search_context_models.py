"""Modèles Pydantic pour l'enrichissement Gemini de la recherche."""

from typing import List

from pydantic import BaseModel, Field


class RecipeSearchContext(BaseModel):
    """Tags et résumé courts en français pour l'index de recherche."""

    cuisine_style: List[str] = Field(default_factory=list, description="Ex: française, italienne")
    meal_moment: List[str] = Field(default_factory=list, description="Ex: déjeuner, dîner, brunch")
    dish_type: List[str] = Field(default_factory=list, description="Ex: gratin, salade, dessert")
    diet_tags: List[str] = Field(default_factory=list, description="Ex: végétarien, sans gluten")
    flavor_profile: List[str] = Field(default_factory=list, description="Ex: crémeux, épicé, léger")
    season: List[str] = Field(default_factory=list, description="Ex: été, hiver")
    occasion: List[str] = Field(default_factory=list, description="Ex: rapide, convivial, healthy")
    main_ingredients: List[str] = Field(default_factory=list, description="Ingrédients phares en français")
    search_phrases: List[str] = Field(
        default_factory=list,
        description="3-8 expressions courtes qu'un utilisateur pourrait taper",
    )
