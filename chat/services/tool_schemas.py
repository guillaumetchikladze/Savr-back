"""DTOs partagés backend ↔ mobile ↔ agent tools."""

from typing import Any, Optional

from pydantic import BaseModel, Field


class RecipeSummary(BaseModel):
    id: int
    title: str
    prep_time: Optional[int] = None
    cook_time: Optional[int] = None
    difficulty: Optional[str] = None
    image_url: Optional[str] = None


class MealPlanRecipeSummary(BaseModel):
    recipe_id: int
    recipe_title: str
    recipe_batch_id: Optional[int] = None


class MealPlanInviteeSummary(BaseModel):
    username: str
    status: str = Field(
        'pending',
        description='pending, accepted ou declined',
    )


class MealPlanSummary(BaseModel):
    id: int
    date: str
    meal_time: str
    meal_type: Optional[str] = None
    confirmed: bool = False
    is_owner: bool = True
    recipes: list[MealPlanRecipeSummary] = Field(default_factory=list)
    invitees: list[MealPlanInviteeSummary] = Field(
        default_factory=list,
        description='Invitations envoyées pour ce repas (repas dont tu es propriétaire).',
    )


class CompliceSummary(BaseModel):
    id: int
    username: str


class MutationProposal(BaseModel):
    card_type: str
    title: str
    subtitle: str
    details: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class AddRecipeResult(BaseModel):
    meal_plan_id: int
    added_recipe_ids: list[int] = Field(default_factory=list)
    already_present_recipe_ids: list[int] = Field(default_factory=list)
    message: str = ''


class CreateMealPlanSlotResult(BaseModel):
    meal_plan_id: int
    date: str
    meal_time: str
    created: bool = True
    message: str = ''


class ImportJobStarted(BaseModel):
    request_id: str
    url: str = ''
    idea_text: str = ''
    job_type: str = 'import'


class SearchRecipesResult(BaseModel):
    query: str
    count: int
    recipes: list[RecipeSummary] = Field(default_factory=list)


class GetMealPlansResult(BaseModel):
    start_date: str
    end_date: str
    count: int
    meal_plans: list[MealPlanSummary] = Field(default_factory=list)


class ShoppingListItemSummary(BaseModel):
    item_id: int
    ingredient_name: str
    remaining_quantity: float = Field(
        0,
        description='Quantité restant à acheter (après déduction du déjà coché et du placard).',
    )
    unit: str = ''
    status: str = Field('to_buy', description='to_buy ou purchased')


class GetShoppingListItemsResult(BaseModel):
    shopping_list_id: int
    shopping_list_name: str = ''
    count: int = 0
    items: list[ShoppingListItemSummary] = Field(default_factory=list)


class AddShoppingListItemResult(BaseModel):
    shopping_list_id: int
    item_id: int
    ingredient_name: str
    quantity: float = 1
    unit: str = 'piece'
    created: bool = True
    message: str = ''
