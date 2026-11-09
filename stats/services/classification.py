"""
Enrichissement des données pour les stats :
- ingrédients → IngredientProfile (groupe alimentaire, végétal, « de base ») : seed déterministe puis LLM ;
- recettes → Recipe.cuisine (liste fermée) : synonymes puis LLM.

Idempotent et reprenable : chaque lot est écrit dès qu'il est classé ; les profils `validated` ne sont
jamais modifiés. Les appels LLM passent par `_call_llm` (mocké dans les tests).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from recipes.models import Ingredient, Recipe

from ..cuisines import CUISINE_CODES, CUISINES, normalize_cuisine_styles, recipe_cuisine_styles
from ..food_groups import FOOD_GROUPS, seed_classify
from ..models import IngredientProfile
from .llm import generate_structured

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 50


# ---------------------------------------------------------------------------
# Schémas LLM
# ---------------------------------------------------------------------------
class IngredientClass(BaseModel):
    id: int
    food_group: str = Field(description='Un code parmi la liste fournie')
    is_plant: bool
    is_basic_staple: bool


class IngredientClassBatch(BaseModel):
    items: list[IngredientClass] = Field(default_factory=list)


class RecipeCuisine(BaseModel):
    id: int
    cuisine: str = Field(description='Un code parmi la liste fournie')


class RecipeCuisineBatch(BaseModel):
    items: list[RecipeCuisine] = Field(default_factory=list)


_INGREDIENT_SYSTEM = (
    'Tu classes des ingrédients de cuisine (noms en français) pour des statistiques de curiosité culinaire.\n'
    "Pour chaque ingrédient, renvoie : food_group (un code EXACT de la liste), is_plant (true si d'origine "
    'végétale : légumes, fruits, légumineuses, céréales, noix, graines, herbes, épices, champignons, algues), '
    'is_basic_staple (true pour les ingrédients de base du placard qui ne reflètent pas la variété du plat : '
    'sel, poivre, huiles, eau, sucre, farine de blé, levure, bouillon, vinaigre, beurre, épices sèches '
    'courantes). Les herbes fraîches, légumes, fromages, viandes ne sont pas « de base ».\n'
    'Aucun jugement nutritionnel. Renvoie exactement un item par id fourni.'
)
_CUISINE_SYSTEM = (
    "Tu rattaches des recettes à une cuisine du monde d'une liste fermée, d'après le titre, les tags de "
    'style et les ingrédients. Utilise « fusion » pour un mélange explicite de cuisines et « autre » si '
    "rien n'est identifiable. Renvoie exactement un item par id fourni, avec un code EXACT de la liste."
)


def _call_llm(system_prompt: str, prompt: str, schema):
    return generate_structured(system_prompt, prompt, schema)


def _chunks(items: Sequence, size: int) -> Iterable[Sequence]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


# ---------------------------------------------------------------------------
# Ingrédients
# ---------------------------------------------------------------------------
@dataclass
class ClassificationReport:
    seeded: int = 0
    llm: int = 0
    skipped: int = 0
    failed_batches: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.seeded + self.llm


def _upsert_profiles(rows: list[IngredientProfile]) -> None:
    if not rows:
        return
    IngredientProfile.objects.bulk_create(
        rows,
        update_conflicts=True,
        unique_fields=['ingredient'],
        update_fields=['food_group', 'is_plant', 'is_basic_staple', 'source', 'updated_at'],
    )


def _ingredient_prompt(batch: Sequence[Ingredient]) -> str:
    groups = '\n'.join(f'- {code} : {label}' for code, label in FOOD_GROUPS.items())
    lines = '\n'.join(f'{ing.id}\t{ing.name}' for ing in batch)
    return f'Codes food_group autorisés :\n{groups}\n\nIngrédients (id<TAB>nom) :\n{lines}'


def classify_ingredients(
    ingredients: Sequence[Ingredient],
    *,
    use_llm: bool = True,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
) -> ClassificationReport:
    """Classe une liste d'ingrédients (les profils `validated` sont ignorés)."""
    report = ClassificationReport()
    validated_ids = set(
        IngredientProfile.objects.filter(ingredient__in=ingredients, validated=True).values_list(
            'ingredient_id', flat=True
        )
    )
    seeded_rows, to_llm = [], []
    for ing in ingredients:
        if ing.id in validated_ids:
            report.skipped += 1
            continue
        seed = seed_classify(ing.name)
        if seed:
            seeded_rows.append(
                IngredientProfile(
                    ingredient=ing,
                    food_group=seed.food_group,
                    is_plant=seed.is_plant,
                    is_basic_staple=seed.is_basic_staple,
                    source=IngredientProfile.SOURCE_MANUAL,
                )
            )
        else:
            to_llm.append(ing)
    if not dry_run:
        _upsert_profiles(seeded_rows)
    report.seeded = len(seeded_rows)

    if not use_llm:
        report.skipped += len(to_llm)
        return report

    for batch in _chunks(to_llm, max(1, batch_size)):
        by_id = {ing.id: ing for ing in batch}
        try:
            result = _call_llm(_INGREDIENT_SYSTEM, _ingredient_prompt(batch), IngredientClassBatch)
        except Exception as exc:  # lot suivant : la commande est reprenable
            logger.warning('[classify_ingredients] lot en échec (%d ingrédients): %s', len(batch), exc)
            report.failed_batches += 1
            report.errors.append(str(exc)[:200])
            continue
        rows = []
        for item in result.items:
            ing = by_id.pop(item.id, None)
            if ing is None:
                continue
            group = item.food_group if item.food_group in FOOD_GROUPS else 'autres'
            rows.append(
                IngredientProfile(
                    ingredient=ing,
                    food_group=group,
                    is_plant=bool(item.is_plant),
                    is_basic_staple=bool(item.is_basic_staple),
                    source=IngredientProfile.SOURCE_LLM,
                )
            )
        if not dry_run:
            _upsert_profiles(rows)
        report.llm += len(rows)
        report.skipped += len(by_id)  # oubliés par le LLM : repris au prochain passage --only-missing
    return report


def ingredients_to_classify(*, only_missing: bool, limit: int | None = None) -> list[Ingredient]:
    """Ingrédients à classer, les plus utilisés d'abord (les stats en profitent dès les premiers lots)."""
    from django.db.models import Count

    qs = Ingredient.objects.annotate(_uses=Count('recipeingredient')).order_by('-_uses', 'id')
    if only_missing:
        qs = qs.filter(profile__isnull=True)
    else:
        qs = qs.exclude(profile__validated=True)
    qs = qs.only('id', 'name')
    return list(qs[:limit] if limit else qs)


# ---------------------------------------------------------------------------
# Cuisines
# ---------------------------------------------------------------------------
@dataclass
class CuisineReport:
    synonyms: int = 0
    llm: int = 0
    unresolved: int = 0
    failed_batches: int = 0


def _cuisine_prompt(batch: Sequence[Recipe]) -> str:
    codes = '\n'.join(f'- {c.code} : {c.label}' for c in CUISINES)
    lines = []
    for r in batch:
        styles = ', '.join(recipe_cuisine_styles(r.search_context_tags)) or '-'
        ings = ', '.join(ri.ingredient.name for ri in list(r.recipe_ingredients.all())[:8]) or '-'
        lines.append(f'{r.id}\t{r.title}\tstyles: {styles}\tingrédients: {ings}')
    return f'Codes cuisine autorisés :\n{codes}\n\nRecettes (id<TAB>titre<TAB>styles<TAB>ingrédients) :\n' + '\n'.join(
        lines
    )


def normalize_recipe_cuisines(
    recipes: Sequence[Recipe],
    *,
    use_llm: bool = True,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
) -> CuisineReport:
    """Synonymes d'abord ; le reste (tags inconnus/génériques ou absents) part au LLM par lots."""
    report = CuisineReport()
    to_llm: list[Recipe] = []
    updates: list[Recipe] = []
    for recipe in recipes:
        code = normalize_cuisine_styles(recipe_cuisine_styles(recipe.search_context_tags))
        if code:
            if recipe.cuisine != code:
                recipe.cuisine = code
                updates.append(recipe)
            report.synonyms += 1
        else:
            to_llm.append(recipe)
    if updates and not dry_run:
        Recipe.objects.bulk_update(updates, ['cuisine'], batch_size=500)

    if not use_llm:
        report.unresolved += len(to_llm)
        return report

    for batch in _chunks(to_llm, max(1, batch_size)):
        by_id = {r.id: r for r in batch}
        try:
            result = _call_llm(_CUISINE_SYSTEM, _cuisine_prompt(batch), RecipeCuisineBatch)
        except Exception as exc:
            logger.warning('[normalize_cuisines] lot en échec (%d recettes): %s', len(batch), exc)
            report.failed_batches += 1
            continue
        changed = []
        for item in result.items:
            recipe = by_id.pop(item.id, None)
            if recipe is None:
                continue
            recipe.cuisine = item.cuisine if item.cuisine in CUISINE_CODES else 'autre'
            changed.append(recipe)
        if changed and not dry_run:
            Recipe.objects.bulk_update(changed, ['cuisine'], batch_size=500)
        report.llm += len(changed)
        report.unresolved += len(by_id)
    return report


def recipes_to_normalize(*, only_missing: bool, limit: int | None = None):
    qs = Recipe.objects.order_by('id').only('id', 'title', 'search_context_tags', 'cuisine')
    if only_missing:
        qs = qs.filter(cuisine__isnull=True)
    qs = qs.prefetch_related('recipe_ingredients__ingredient')
    return list(qs[:limit] if limit else qs)
