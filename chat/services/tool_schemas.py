"""DTOs partagés backend ↔ mobile ↔ agent tools."""

from typing import Any

from pydantic import BaseModel, Field


class RecipeSummary(BaseModel):
    id: int
    title: str
    prep_time: int | None = None
    cook_time: int | None = None
    difficulty: str | None = None
    image_url: str | None = None


class MealPlanRecipeSummary(BaseModel):
    recipe_id: int
    recipe_title: str
    recipe_batch_id: int | None = None


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
    meal_type: str | None = None
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


# ---------------------------------------------------------------------------
# Agent v2 : panier « Cette semaine », « J'ai cuisiné », courses depuis le panier
# ---------------------------------------------------------------------------
class UndoHint(BaseModel):
    """Annulation côté app (carte résultat) : appel REST à rejouer tel quel."""

    method: str
    path: str
    label: str = 'Annuler'


class BasketAddition(BaseModel):
    basket_item_id: int
    display_title: str
    recipe_id: int | None = None
    is_free_text: bool = Field(False, description='Plat libre (sans fiche recette).')
    week_start: str
    status: str = Field('todo', description='todo (sans date) ou planned (daté)')
    date: str | None = None
    meal_time: str | None = None
    meal_plan_id: int | None = None
    undo: UndoHint | None = None


class SkippedDish(BaseModel):
    recipe_id: int | None = None
    label: str = ''
    reason: str = Field(description='allergy, diet, dislike, recipe_not_found, invalid')
    detail: str = ''


class PlanWeekResult(BaseModel):
    card_type: str = 'basket_additions'
    week_start: str
    added: list[BasketAddition] = Field(default_factory=list)
    skipped: list[SkippedDish] = Field(default_factory=list)
    message: str = ''


class LogCookedResult(BaseModel):
    card_type: str = 'cooked_log'
    post_id: int
    meal_plan_id: int | None = None
    created: bool = True
    is_published: bool = False
    display_title: str = ''
    date: str
    meal_time: str = ''
    basket_item_id: int | None = None
    cuisine: dict[str, Any] | None = None
    highlights: list[dict[str, Any]] = Field(default_factory=list, description="Puces de L'ardoise (≤ 3)")
    new_badges: list[dict[str, Any]] = Field(default_factory=list)
    undo: UndoHint | None = None
    message: str = ''


class BasketShoppingResult(BaseModel):
    card_type: str = 'basket_shopping_list'
    shopping_list_id: int
    shopping_list_name: str = ''
    created_list: bool = False
    added_basket_item_ids: list[int] = Field(default_factory=list)
    skipped: list[dict[str, Any]] = Field(default_factory=list)
    message: str = ''


class BasketRemovalResult(BaseModel):
    removed_basket_item_ids: list[int] = Field(default_factory=list)
    message: str = ''


class UndoCookedResult(BaseModel):
    post_id: int
    message: str = ''
