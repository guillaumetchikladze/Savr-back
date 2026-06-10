"""Service sync pour les listes de courses — réutilisable par ViewSets et agent chat."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import AbstractBaseUser
from django.db import transaction
from django.utils import timezone

from chat.services.tool_schemas import (
    AddShoppingListItemResult,
    GetShoppingListItemsResult,
    ShoppingListItemSummary,
)
from recipes.models import ShoppingList, ShoppingListItem, ShoppingListItemQuantity, ShoppingListMember
from recipes.services.ingredient_matcher import get_or_create_ingredient
from recipes.utils import shopping_list_item_quantity_is_stale

_VALID_UNITS = frozenset({'g', 'kg', 'ml', 'l', 'tsp', 'tbsp', 'cup', 'piece', 'pinch', 'clove'})
_HIDE_AFTER = timedelta(days=1)


def _get_accessible_shopping_list(user: AbstractBaseUser, shopping_list_id: int) -> ShoppingList:
    is_member = ShoppingListMember.objects.filter(
        shopping_list_id=shopping_list_id,
        user=user,
    ).exists()
    if not is_member:
        raise ValueError('Liste de courses introuvable ou accès refusé.')
    shopping_list = ShoppingList.objects.filter(pk=shopping_list_id).first()
    if not shopping_list:
        raise ValueError('Liste de courses introuvable ou accès refusé.')
    return shopping_list


def _unit_group_for_unit(unit: str) -> str:
    unit = (unit or '').lower()
    if unit in ('g', 'kg'):
        return 'weight'
    if unit in ('ml', 'l'):
        return 'volume'
    if unit == 'piece':
        return 'count'
    if unit == 'pinch':
        return 'pinch'
    if unit == 'clove':
        return 'clove'
    return 'other'


def _canonicalize_quantity(quantity: float, unit: str) -> tuple[float, str]:
    unit = (unit or '').lower()
    if unit == 'kg':
        return float(quantity) * 1000.0, 'g'
    if unit == 'l':
        return float(quantity) * 1000.0, 'ml'
    return float(quantity), unit


def _normalize_unit(unit: str) -> str:
    normalized = (unit or 'piece').lower()
    if normalized not in _VALID_UNITS:
        raise ValueError(
            f"Unité invalide « {unit} ». Unités valides : {', '.join(sorted(_VALID_UNITS))}."
        )
    return normalized


def _summarize_item(item: ShoppingListItem, *, now, include_purchased: bool) -> ShoppingListItemSummary | None:
    quantities = list(item.quantities.all())
    active_quantities = [
        q for q in quantities
        if not shopping_list_item_quantity_is_stale(q, now, _HIDE_AFTER)
    ]
    if not active_quantities:
        return None

    total_qty = float(sum(Decimal(str(q.quantity or 0)) for q in active_quantities))
    total_checked = float(sum(Decimal(str(q.checked_quantity or 0)) for q in active_quantities))
    pantry = float(item.pantry_quantity or 0)
    remaining = total_qty - total_checked - pantry
    if remaining < 0:
        remaining = 0.0

    status = 'purchased' if remaining <= 0 else 'to_buy'
    if status == 'purchased' and not include_purchased:
        return None

    unit = ''
    first_q = active_quantities[0]
    if first_q and first_q.unit:
        unit = first_q.unit
    elif item.pantry_unit:
        unit = item.pantry_unit

    return ShoppingListItemSummary(
        item_id=item.id,
        ingredient_name=item.ingredient.name if item.ingredient_id else '?',
        remaining_quantity=remaining,
        unit=unit,
        status=status,
    )


def get_shopping_list_items_for_user(
    user: AbstractBaseUser,
    shopping_list_id: int,
    *,
    include_purchased: bool = False,
) -> GetShoppingListItemsResult:
    shopping_list = _get_accessible_shopping_list(user, shopping_list_id)
    now = timezone.now()
    items_qs = (
        ShoppingListItem.objects.filter(shopping_list=shopping_list)
        .select_related('ingredient')
        .prefetch_related('quantities')
        .order_by('ingredient__name')
    )

    summaries: list[ShoppingListItemSummary] = []
    for item in items_qs:
        summary = _summarize_item(item, now=now, include_purchased=include_purchased)
        if summary:
            summaries.append(summary)

    return GetShoppingListItemsResult(
        shopping_list_id=shopping_list.id,
        shopping_list_name=shopping_list.name or 'Liste de courses',
        count=len(summaries),
        items=summaries,
    )


def add_item_to_shopping_list(
    user: AbstractBaseUser,
    shopping_list_id: int,
    ingredient_name: str,
    *,
    quantity: float = 1.0,
    unit: str = 'piece',
) -> AddShoppingListItemResult:
    ingredient_name = (ingredient_name or '').strip()
    if not ingredient_name:
        raise ValueError("Nom d'ingrédient requis.")

    unit = _normalize_unit(unit)
    shopping_list = _get_accessible_shopping_list(user, shopping_list_id)

    with transaction.atomic():
        ingredient, _ = get_or_create_ingredient(ingredient_name)

        if not ingredient.category:
            from recipes.services.ingredient_categorization import resolve_category_for_ingredient

            category = resolve_category_for_ingredient(ingredient_name, ingredient)
            if category:
                ingredient.category = category
                ingredient.save(update_fields=['category'])

        unit_group = _unit_group_for_unit(unit)
        item, item_created = ShoppingListItem.objects.get_or_create(
            shopping_list=shopping_list,
            ingredient=ingredient,
            unit_group=unit_group,
            defaults={'pantry_unit': unit},
        )

        canonical_qty, canonical_unit = _canonicalize_quantity(float(quantity), unit)
        quantity_obj, qty_created = ShoppingListItemQuantity.objects.get_or_create(
            shopping_list_item=item,
            recipe_batch=None,
            defaults={
                'quantity': Decimal(str(canonical_qty)),
                'unit': canonical_unit,
            },
        )
        if not qty_created:
            quantity_obj.quantity = Decimal(str(quantity_obj.quantity)) + Decimal(str(canonical_qty))
            quantity_obj.save(update_fields=['quantity', 'updated_at'])

        shopping_list.updated_at = timezone.now()
        shopping_list.save(update_fields=['updated_at'])

    from recipes.views import (
        _broadcast_shopping_list_item_update,
        _mark_shopping_done_if_list_complete,
    )

    _mark_shopping_done_if_list_complete(shopping_list)
    _broadcast_shopping_list_item_update(item, user)

    action = 'ajouté' if item_created else 'mis à jour'
    return AddShoppingListItemResult(
        shopping_list_id=shopping_list.id,
        item_id=item.id,
        ingredient_name=ingredient.name,
        quantity=float(canonical_qty),
        unit=canonical_unit,
        created=item_created,
        message=f'« {ingredient.name} » {action} ({canonical_qty} {canonical_unit}).',
    )
