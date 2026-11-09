"""Modèles Pydantic pour l'enrichissement Gemini de la recherche."""

from pydantic import BaseModel, Field


class RecipeSearchContext(BaseModel):
    """Tags et résumé courts en français pour l'index de recherche."""

    cuisine_style: list[str] = Field(default_factory=list, description='Ex: française, italienne')
    meal_moment: list[str] = Field(default_factory=list, description='Ex: déjeuner, dîner, brunch')
    dish_type: list[str] = Field(default_factory=list, description='Ex: gratin, salade, dessert')
    diet_tags: list[str] = Field(default_factory=list, description='Ex: végétarien, sans gluten')
    flavor_profile: list[str] = Field(default_factory=list, description='Ex: crémeux, épicé, léger')
    season: list[str] = Field(default_factory=list, description='Ex: été, hiver')
    occasion: list[str] = Field(default_factory=list, description='Ex: rapide, convivial, healthy')
    main_ingredients: list[str] = Field(default_factory=list, description='Ingrédients phares en français')
    search_phrases: list[str] = Field(
        default_factory=list,
        description="3-8 expressions courtes qu'un utilisateur pourrait taper",
    )
