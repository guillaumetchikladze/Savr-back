"""
« Plan de bataille » côté base : résolution de la source (repas, envies du panier, recettes), droits d'accès,
portions, horaire (« commence à 18 h 20 pour manger à 20 h ») puis `battle_plan.build_plan` (déterministe).

Accès : repas accessibles (propriétaire ou invité, comme le détail du repas), envies de **son** panier, recettes
visibles (publiques, à soi, ou d'un repas où l'on est invité). Inaccessible → 404, jamais 403.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date as date_type
from datetime import datetime, time

from django.contrib.auth.models import AbstractBaseUser
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Prefetch, Q

from recipes.models import MealPlan, MealPlanRecipeBatch, Recipe, RecipeIngredient, Step, StepIngredient
from recipes.services import battle_plan as bp
from recipes.services import diners_service
from recipes.utils import get_accessible_meal_plan_filter, get_invited_recipe_filter

MAX_SERVINGS = 99


class BattlePlanError(Exception):
    def __init__(self, message: str, status_code: int = 400, code: str = 'invalid'):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _int_list(value, field: str) -> list[int]:
    if value in (None, ''):
        return []
    if not isinstance(value, list | tuple):
        raise BattlePlanError(f'{field} doit être une liste d’entiers.')
    try:
        return list(dict.fromkeys(int(v) for v in value if not isinstance(v, bool)))
    except (TypeError, ValueError) as exc:
        raise BattlePlanError(f'{field} doit être une liste d’entiers.') from exc


def _servings(value) -> int | None:
    if value in (None, ''):
        return None
    try:
        servings = int(value)
    except (TypeError, ValueError) as exc:
        raise BattlePlanError('servings doit être un entier.') from exc
    if not 1 <= servings <= MAX_SERVINGS:
        raise BattlePlanError(f'servings doit être entre 1 et {MAX_SERVINGS}.')
    return servings


_CLOCK_RE = re.compile(r'^\s*(\d{1,2})\s*(?:h|:)\s*(\d{2})?\s*(?:min)?\s*$', re.I)


def parse_moment(value, field: str) -> tuple[date_type | None, time | datetime] | None:
    """« 20:00 », « 20h », « 20 h 30 » → (None, time) ; ISO « 2026-11-09T20:00[+01:00] » → (date, datetime|time)."""
    if value in (None, ''):
        return None
    raw = str(value).strip()
    m = _CLOCK_RE.match(raw)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if hour > 23 or minute > 59:
            raise BattlePlanError(f'{field} : heure invalide.')
        return None, time(hour, minute)
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise BattlePlanError(f'{field} : « HH:MM » ou date-heure ISO attendue.') from exc
    if moment.tzinfo is not None:
        return moment.date(), moment
    return moment.date(), moment.time()


def _user_zone(user: AbstractBaseUser):
    from notifications.services import get_preferences, user_zone

    return user_zone(get_preferences(user))


def _anchor(user, moment, default_day: date_type | None) -> datetime | None:
    if moment is None:
        return None
    day, value = moment
    zone = _user_zone(user)
    if isinstance(value, datetime):
        return value.astimezone(zone)
    if day is None:
        if default_day is None:
            from notifications.services import get_preferences, local_now

            default_day = local_now(get_preferences(user)).date()
        day = default_day
    return datetime.combine(day, value).replace(tzinfo=zone)


# ---------------------------------------------------------------------------
# Chargement des recettes
# ---------------------------------------------------------------------------
def visible_recipes(user: AbstractBaseUser):
    return Recipe.objects.filter(Q(is_public=True) | Q(created_by=user) | get_invited_recipe_filter(user))


def _is_staple(ingredient) -> bool:
    try:
        profile = ingredient.profile
    except ObjectDoesNotExist:
        profile = None
    if profile is not None:
        return bool(profile.is_basic_staple)
    from stats.food_groups import is_staple_name

    return is_staple_name(ingredient.name)


def load_recipe_inputs(recipe_ids: list[int], servings_by_recipe: dict[int, int | None]) -> list[bp.RecipeInput]:
    """Recettes + étapes + ingrédients en 4 requêtes, dans l'ordre de `recipe_ids`."""
    step_qs = Step.objects.order_by('order').prefetch_related(
        Prefetch('step_ingredients', queryset=StepIngredient.objects.only('id', 'step_id', 'ingredient_id'))
    )
    ingredient_qs = RecipeIngredient.objects.select_related('ingredient__profile').order_by('id')
    recipes = {
        r.id: r
        for r in Recipe.objects.filter(id__in=recipe_ids)
        .only('id', 'title', 'prep_time', 'cook_time', 'servings', 'image_path', 'updated_at')
        .prefetch_related(Prefetch('steps', queryset=step_qs), Prefetch('recipe_ingredients', queryset=ingredient_qs))
    }
    out = []
    for rid in recipe_ids:
        recipe = recipes.get(rid)
        if recipe is None:
            continue
        base = recipe.servings or 4
        out.append(
            bp.RecipeInput(
                recipe_id=recipe.id,
                title=recipe.title,
                prep_time=recipe.prep_time,
                cook_time=recipe.cook_time,
                servings=servings_by_recipe.get(rid) or base,
                base_servings=base,
                image_url=recipe.image_url,
                version=recipe.updated_at.isoformat() if recipe.updated_at else '',
                steps=[
                    bp.StepInput(
                        step_id=s.id,
                        order=s.order,
                        title=s.title,
                        instruction=s.instruction,
                        has_timer=s.has_timer,
                        timer_duration=s.timer_duration,
                        ingredient_ids=tuple(si.ingredient_id for si in s.step_ingredients.all()),
                    )
                    for s in recipe.steps.all()
                ],
                ingredients=[
                    bp.IngredientInput(
                        ingredient_id=ri.ingredient_id,
                        name=ri.ingredient.name,
                        quantity=float(ri.quantity or 0),
                        unit=ri.unit or '',
                        is_staple=_is_staple(ri.ingredient),
                    )
                    for ri in recipe.recipe_ingredients.all()
                ],
            )
        )
    return out


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
def _from_meal_plan(user, meal_plan_id: int, servings: int | None):
    meal_plan = (
        MealPlan.objects.filter(get_accessible_meal_plan_filter(user), pk=meal_plan_id)
        .prefetch_related(
            Prefetch(
                'meal_plan_recipe_batches',
                queryset=MealPlanRecipeBatch.objects.select_related('recipe_batch').order_by('order', 'created_at'),
            )
        )
        .first()
    )
    if meal_plan is None:
        raise BattlePlanError('Repas introuvable.', 404, 'not_found')
    recipe_ids: list[int] = []
    by_recipe: dict[int, int | None] = {}
    modes: dict[str, str] = {}
    people = None
    for mprb in meal_plan.meal_plan_recipe_batches.all():
        rid = mprb.recipe_batch.recipe_id
        if rid in by_recipe:
            continue
        modes.update(mprb.recipe_batch.sub_recipe_modes or {})
        recipe_ids.append(rid)
        if servings is None and not mprb.portions and people is None:
            people = diners_service.meal_plan_people_count(meal_plan)
        by_recipe[rid] = servings or mprb.portions or people
    source = {'type': 'meal_plan', 'meal_plan_id': meal_plan.id}
    return recipe_ids, by_recipe, [], source, meal_plan, modes


def _from_basket(user, item_ids: list[int], servings: int | None):
    from recipes.services import basket_service

    items = list(basket_service.basket_queryset(user).filter(id__in=item_ids).select_related('recipe', 'recipe_batch'))
    if len(items) != len(item_ids):
        raise BattlePlanError('Envie introuvable dans ton panier.', 404, 'not_found')
    items.sort(key=lambda i: item_ids.index(i.id))
    recipe_ids: list[int] = []
    by_recipe: dict[int, int | None] = {}
    skipped = []
    cache: dict = {}
    modes: dict[str, str] = {}
    for item in items:
        if not item.recipe_id:
            skipped.append({'basket_item_id': item.id, 'label': item.label, 'reason': 'no_recipe'})
            continue
        if item.recipe_id in by_recipe:
            continue
        if item.recipe_batch_id and item.recipe_batch.recipe_id == item.recipe_id:
            modes.update(item.recipe_batch.sub_recipe_modes or {})
        recipe_ids.append(item.recipe_id)
        by_recipe[item.recipe_id] = (
            servings or item.servings or diners_service.basket_item_people_count(item, cache=cache)
        )
    source = {'type': 'basket', 'basket_item_ids': [i.id for i in items]}
    return recipe_ids, by_recipe, skipped, source, modes


def _from_recipes(user, recipe_ids: list[int], servings: int | None):
    found = set(visible_recipes(user).filter(id__in=recipe_ids).values_list('id', flat=True).distinct())
    if len(found) != len(recipe_ids):
        raise BattlePlanError('Recette introuvable.', 404, 'not_found')
    source = {'type': 'recipes', 'recipe_ids': recipe_ids}
    return recipe_ids, dict.fromkeys(recipe_ids, servings), [], source, {}


def kitchen_resources(user) -> bp.KitchenResources:
    """Ressources du plan d'après « Ta cuisine » (la sienne, sinon celle du foyer) ; inconnue → défaut historique."""
    from recipes.services.equipment_service import effective_kitchen

    kitchen = effective_kitchen(user)
    if kitchen is None:
        return bp.DEFAULT_KITCHEN
    has_hob = kitchen.has('plaques')
    return bp.KitchenResources(
        burners=kitchen.burners if has_hob and kitchen.burners else None,
        ovens=max(kitchen.ovens, 1),
        has_oven=kitchen.has('four'),
        airfryer=kitchen.has('airfryer'),
        has_hob=has_hob,
        induction=kitchen.induction,
        known=True,
    )


MAX_PLAN_RECIPES = bp.MAX_RECIPES * 2  # recettes du repas + sous-recettes faites maison


def with_sub_recipes(recipes: list[bp.RecipeInput], modes: dict[str, str]) -> tuple[list[bp.RecipeInput], list[dict]]:
    """
    Ajoute les sous-recettes faites maison (choix du batch, sinon `default_mode`) avant leur recette parente, à
    l'échelle des portions de la parente × `sub_recipe_ratio`. Au-delà de `MAX_PLAN_RECIPES` → `skipped`.
    """
    from recipes.services.sub_recipe_service import made_sub_recipes

    trees = {r.recipe_id: made_sub_recipes(r.recipe_id, r.servings / (r.base_servings or 1), modes) for r in recipes}
    sub_ids: list[int] = []

    def collect(nodes):
        for node in nodes:
            sub_ids.append(node.recipe_id)
            collect(node.children)

    for nodes in trees.values():
        collect(nodes)
    if not sub_ids:
        return recipes, []
    loaded = {r.recipe_id: r for r in load_recipe_inputs(list(dict.fromkeys(sub_ids)), {})}
    out: list[bp.RecipeInput] = []
    parents: dict[int, bp.RecipeInput] = {}  # id(sous-recette) → recette qui l'utilise
    skipped: list[dict] = []

    def emit(nodes, parent: bp.RecipeInput) -> None:
        # Post-ordre : sous-recettes d'abord, la parente ensuite (dépendances toujours vers une recette antérieure).
        for node in nodes:
            base = loaded.get(node.recipe_id)
            if base is None:
                continue
            if len(out) + len(recipes) >= MAX_PLAN_RECIPES:
                skipped.append({'recipe_id': node.recipe_id, 'line_id': node.line_id, 'reason': 'too_many_recipes'})
                continue
            sub = replace(
                base,
                servings=max(1, round((base.base_servings or 1) * node.ratio)),
                used_line_id=node.line_id,
                used_ingredient_id=node.ingredient_id,
                used_ingredient_name=node.ingredient_name,
            )
            emit(node.children, sub)
            out.append(sub)
            parents[id(sub)] = parent

    for recipe in recipes:
        top = replace(recipe)
        emit(trees[recipe.recipe_id], top)
        out.append(top)
    position = {id(r): i for i, r in enumerate(out)}
    for r in out:
        if id(r) in parents:
            r.parent_idx = position[id(parents[id(r)])]
    return out, skipped


def battle_plan_for_user(
    user: AbstractBaseUser,
    *,
    meal_plan_id=None,
    basket_item_ids=None,
    recipe_ids=None,
    servings=None,
    start_time=None,
    target_ready_time=None,
    polish: bool = False,
    sub_recipe_modes=None,
) -> dict:
    """
    Plan de bataille d'un repas, d'envies du panier ou de recettes (exactement une source). Sous-recettes faites
    maison : choix des batches, surchargés par `sub_recipe_modes` ({line_id: "make" | "buy"}), sinon `default_mode`.
    """
    from recipes.services.sub_recipe_service import SubRecipeError, clean_modes

    servings = _servings(servings)
    try:
        mode_overrides = clean_modes(sub_recipe_modes)
    except SubRecipeError as exc:
        raise BattlePlanError(exc.message, 400, 'invalid_sub_recipe_modes') from exc
    basket_ids = _int_list(basket_item_ids, 'basket_item_ids')
    rids = _int_list(recipe_ids, 'recipe_ids')
    sources = sum(bool(x) for x in (meal_plan_id not in (None, ''), basket_ids, rids))
    if sources != 1:
        raise BattlePlanError('Indique meal_plan_id, basket_item_ids ou recipe_ids (un seul).', 400, 'invalid_source')
    start = parse_moment(start_time, 'start_time')
    ready = parse_moment(target_ready_time, 'target_ready_time')
    if start and ready:
        raise BattlePlanError('Donne start_time ou target_ready_time, pas les deux.', 400, 'invalid_time')

    meal_plan = None
    if meal_plan_id not in (None, ''):
        try:
            mp_id = int(meal_plan_id)
        except (TypeError, ValueError) as exc:
            raise BattlePlanError('meal_plan_id doit être un entier.') from exc
        rids, by_recipe, skipped, source, meal_plan, modes = _from_meal_plan(user, mp_id, servings)
    elif basket_ids:
        rids, by_recipe, skipped, source, modes = _from_basket(user, basket_ids, servings)
    else:
        rids, by_recipe, skipped, source, modes = _from_recipes(user, rids, servings)
    modes = {**modes, **mode_overrides}

    if not rids:
        raise BattlePlanError('Aucune recette avec des étapes à organiser.', 400, 'no_recipes')
    if len(rids) > bp.MAX_RECIPES:
        raise BattlePlanError(f'{bp.MAX_RECIPES} recettes maximum.', 400, 'too_many_recipes')

    default_day = meal_plan.date if meal_plan is not None else None
    if meal_plan is not None and not start and not ready and meal_plan.scheduled_time:
        ready = (None, meal_plan.scheduled_time)
    recipes, too_many = with_sub_recipes(load_recipe_inputs(rids, by_recipe), modes)
    skipped = [*skipped, *too_many]
    plan = bp.build_plan(
        recipes,
        start_at=_anchor(user, start, default_day),
        ready_at=_anchor(user, ready, default_day),
        kitchen=kitchen_resources(user),
    )
    plan['source'] = source
    plan['skipped'] = skipped
    plan['polished'] = False
    if polish:
        from recipes.services.battle_plan_polish import polish_plan

        key = [(r.recipe_id, r.servings, r.version) for r in recipes] + [plan['kitchen']]
        plan = polish_plan(plan, recipes_key=key)
    return plan
