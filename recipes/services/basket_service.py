"""
Panier « Cette semaine » (envies non datées) et « À mon tour ».

Modèle : `BasketItem` (une envie = recette ou plat libre, pour la semaine `week_start`, lundi dans le
fuseau de l'utilisateur). Une envie n'est **pas** un repas : le « cuisiné » reste défini par
`MealPlan.confirmed` (`cooked_service.cooked_meal_plans`, source unique des stats). Dater une envie
(`schedule_item`) ou la cuisiner (`POST /posts/cooked/` avec `basket_item_id`) la rattache à un vrai
`MealPlan` ; elle n'est jamais supprimée automatiquement (historique « Pas fait »).

Statut (dérivé, jamais stocké → pas de dérive quand un repas est confirmé / supprimé ailleurs) :
    cooked        repas lié confirmé
    carried_over  reportée sur une semaine suivante (`carried_over_to`)
    not_done      semaine passée, ni cuisinée ni reportée (« Pas fait »)
    planned       repas lié (daté), pas encore cuisiné
    todo          dans le panier, sans date

« À mon tour » : `POST /posts/{id}/my-turn/` ajoute le plat du post au panier avec
`inspired_by_post` et prévient l'auteur (`a_mon_tour`, agrégé, une fois par ami et par post).
Quand l'envie est cuisinée, le post « J'ai cuisiné » porte `inspired_by_post` et l'auteur reçoit
`a_mon_tour_cooked` (« Léa a refait « Quiche ». »).

Convives (« Foyer + invités ») : chaque envie a la composition d'un repas (`guest_count`,
`excluded_household_ids`, foyer inclus par défaut, `diners_service`). Une nouvelle envie reprend celle
de la semaine (en-tête « Pour 4 : toi, Pauline + 2 invités ») ; `servings` vide = ce total (portions et
liste de courses). Une envie datée prend les convives de son repas.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import timedelta

from django.contrib.auth.models import AbstractBaseUser
from django.db import transaction
from django.db.models import Count, Max, Q, QuerySet
from django.utils import timezone

from recipes.models import (
    BasketItem,
    MealPlan,
    MealPlanRecipeBatch,
    Post,
    Recipe,
    RecipeBatch,
    ShoppingList,
    ShoppingListBatch,
    ShoppingListItem,
    ShoppingListItemQuantity,
    ShoppingListMember,
)

LABEL_MAX_LENGTH = 120
MAX_SERVINGS = 99
DEFAULT_SHOPPING_LIST_NAME = 'Courses de la semaine'
_STANDARD_SLOTS = ('breakfast', 'lunch', 'dinner')
_MEAL_TIMES = (*_STANDARD_SLOTS, 'other')

STATUS_TODO = 'todo'
STATUS_PLANNED = 'planned'
STATUS_COOKED = 'cooked'
STATUS_CARRIED_OVER = 'carried_over'
STATUS_NOT_DONE = 'not_done'
# Envies encore « ouvertes » (reportables, à mettre dans la liste de courses).
OPEN_STATUSES = (STATUS_TODO, STATUS_PLANNED, STATUS_NOT_DONE)


class BasketError(Exception):
    def __init__(self, message: str, status_code: int = 400, code: str = 'invalid'):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


# ---------------------------------------------------------------------------
# Semaines (lundi, fuseau de l'utilisateur)
# ---------------------------------------------------------------------------
def monday_of(d: date_type) -> date_type:
    return d - timedelta(days=d.weekday())


def user_today(user: AbstractBaseUser) -> date_type:
    """Date du jour dans le fuseau de l'utilisateur (préférences de notifications, sinon fuseau serveur)."""
    from notifications.services import get_preferences, local_now

    return local_now(get_preferences(user)).date()


def current_week_start(user: AbstractBaseUser) -> date_type:
    return monday_of(user_today(user))


def parse_week(value, user: AbstractBaseUser) -> date_type:
    """`?week=YYYY-MM-DD` (n'importe quel jour de la semaine) → lundi ; absent → semaine courante."""
    if value in (None, ''):
        return current_week_start(user)
    try:
        return monday_of(date_type.fromisoformat(str(value)[:10]))
    except ValueError as exc:
        raise BasketError('week must be a date (YYYY-MM-DD)') from exc


def _parse_date(value, field_name: str) -> date_type:
    try:
        return date_type.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as exc:
        raise BasketError(f'{field_name} must be a date (YYYY-MM-DD)') from exc


def _parse_int(value, field_name: str) -> int | None:
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        raise BasketError(f'{field_name} must be an integer')
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise BasketError(f'{field_name} must be an integer') from exc


def parse_servings(value) -> int | None:
    servings = _parse_int(value, 'servings')
    if servings is None:
        return None
    if not 1 <= servings <= MAX_SERVINGS:
        raise BasketError(f'servings must be between 1 and {MAX_SERVINGS}')
    return servings


# ---------------------------------------------------------------------------
# Lecture
# ---------------------------------------------------------------------------
def basket_queryset(user: AbstractBaseUser) -> QuerySet[BasketItem]:
    return BasketItem.objects.filter(user=user).select_related('recipe', 'meal_plan', 'inspired_by_post__user')


def item_status(item: BasketItem, week_now: date_type) -> str:
    meal_plan = item.meal_plan if item.meal_plan_id else None
    if meal_plan is not None and meal_plan.confirmed:
        return STATUS_COOKED
    if item.carried_over_to_id:
        return STATUS_CARRIED_OVER
    if item.week_start < week_now:
        return STATUS_NOT_DONE
    if meal_plan is not None:
        return STATUS_PLANNED
    return STATUS_TODO


def cooked_post_ids(items: list[BasketItem]) -> dict[int, int]:
    """meal_plan_id → id du post « J'ai cuisiné » (une requête pour toute la semaine)."""
    meal_plan_ids = {i.meal_plan_id for i in items if i.meal_plan_id and i.meal_plan.confirmed}
    if not meal_plan_ids:
        return {}
    rows = (
        Post.objects.filter(meal_plan_id__in=meal_plan_ids)
        .order_by('-is_published', 'id')
        .values_list('meal_plan_id', 'id')
    )
    result: dict[int, int] = {}
    for meal_plan_id, post_id in rows:
        result.setdefault(meal_plan_id, post_id)
    return result


def week_items(user: AbstractBaseUser, week: date_type) -> list[BasketItem]:
    return list(basket_queryset(user).filter(week_start=week).order_by('position', 'id'))


def open_items_count(user: AbstractBaseUser, week: date_type) -> int:
    """Envies ni cuisinées ni reportées d'une semaine (proposition neutre de report, jamais de relance)."""
    return (
        BasketItem.objects.filter(user=user, week_start=week, carried_over_to__isnull=True)
        .exclude(meal_plan__confirmed=True)
        .count()
    )


def get_item(user: AbstractBaseUser, item_id) -> BasketItem:
    item = basket_queryset(user).filter(id=_parse_int(item_id, 'id')).first()
    if not item:
        raise BasketError('Basket item not found', 404, 'basket_item_not_found')
    return item


# ---------------------------------------------------------------------------
# Écriture : ajout, modification, suppression, ordre
# ---------------------------------------------------------------------------
def _next_position(user: AbstractBaseUser, week: date_type) -> int:
    current = BasketItem.objects.filter(user=user, week_start=week).aggregate(m=Max('position'))['m']
    return 0 if current is None else current + 1


def add_item(
    user: AbstractBaseUser,
    *,
    recipe_id=None,
    label: str | None = None,
    servings=None,
    week=None,
    inspired_by_post: Post | None = None,
    guest_count=None,
    excluded_household_ids=None,
) -> BasketItem:
    """Ajoute une envie (recette ou plat libre). Plusieurs envies par semaine, doublons permis."""
    label = (label or '').strip()[:LABEL_MAX_LENGTH]
    recipe = None
    rid = _parse_int(recipe_id, 'recipe_id')
    if rid is not None:
        recipe = Recipe.objects.filter(id=rid).first()
        if not recipe:
            raise BasketError(f'Recipe {rid} not found', 404, 'recipe_not_found')
    if not recipe and not label:
        raise BasketError('recipe_id or label is required', 400, 'missing_dish')
    servings = parse_servings(servings)
    week_start = parse_week(week, user)
    guests, excluded = week_composition(user, week_start)
    if guest_count is not None:
        guests = parse_guest_count(guest_count)
    if excluded_household_ids is not None:
        excluded = parse_excluded_household_ids(excluded_household_ids)
    return BasketItem.objects.create(
        user=user,
        week_start=week_start,
        recipe=recipe,
        label=label or (recipe.title[:LABEL_MAX_LENGTH] if recipe else ''),
        servings=servings,
        guest_count=guests,
        excluded_household_ids=excluded,
        position=_next_position(user, week_start),
        inspired_by_post=inspired_by_post,
    )


def parse_guest_count(value) -> int:
    guests = _parse_int(value, 'guest_count') or 0
    if not 0 <= guests <= MAX_SERVINGS:
        raise BasketError(f'guest_count must be between 0 and {MAX_SERVINGS}')
    return guests


def parse_excluded_household_ids(value) -> list[int]:
    from accounts.services.household_service import HouseholdError, clean_excluded_ids

    try:
        return clean_excluded_ids(value)
    except HouseholdError as exc:
        raise BasketError(exc.message) from None


def week_composition(user: AbstractBaseUser, week: date_type) -> tuple[int, list[int]]:
    """Composition de la semaine : celle de la dernière envie non datée (défaut : foyer complet, 0 invité)."""
    last = (
        BasketItem.objects.filter(user=user, week_start=week, meal_plan__isnull=True)
        .order_by('-position', '-id')
        .values('guest_count', 'excluded_household_ids')
        .first()
    )
    if not last:
        return 0, []
    return last['guest_count'] or 0, list(last['excluded_household_ids'] or [])


def _set_meal_composition(meal_plan: MealPlan, *, guest_count=..., excluded_household_ids=...) -> None:
    fields = []
    if guest_count is not ...:
        meal_plan.guest_count = guest_count
        fields.append('guest_count')
    if excluded_household_ids is not ...:
        meal_plan.excluded_household_ids = excluded_household_ids
        fields.append('excluded_household_ids')
    if fields:
        meal_plan.save(update_fields=[*fields, 'updated_at'])


def set_week_diners(user: AbstractBaseUser, week: date_type, *, guest_count=..., excluded_household_ids=...) -> int:
    """En-tête du panier : applique la composition à toutes les envies non datées et non cuisinées de la semaine."""
    values = {}
    if guest_count is not ...:
        values['guest_count'] = parse_guest_count(guest_count)
    if excluded_household_ids is not ...:
        values['excluded_household_ids'] = parse_excluded_household_ids(excluded_household_ids)
    if not values:
        raise BasketError('guest_count or excluded_household_ids is required')
    return BasketItem.objects.filter(
        user=user, week_start=week, meal_plan__isnull=True, carried_over_to__isnull=True
    ).update(**values, updated_at=timezone.now())


def update_item(
    item: BasketItem, *, servings=..., label=..., guest_count=..., excluded_household_ids=...
) -> BasketItem:
    update_fields = ['updated_at']
    composition = {}
    if guest_count is not ...:
        composition['guest_count'] = parse_guest_count(guest_count)
    if excluded_household_ids is not ...:
        composition['excluded_household_ids'] = parse_excluded_household_ids(excluded_household_ids)
    if composition:
        if item.meal_plan_id:  # envie datée : les convives sont ceux du repas
            if item.meal_plan.confirmed:
                raise BasketError('This dish is already cooked', 400, 'already_cooked')
            _set_meal_composition(item.meal_plan, **composition)
        else:
            for name, value in composition.items():
                setattr(item, name, value)
            update_fields.extend(composition)
    if servings is not ...:
        item.servings = parse_servings(servings)
        update_fields.append('servings')
        if item.meal_plan_id and item.recipe_batch_id and not item.meal_plan.confirmed:
            MealPlanRecipeBatch.objects.filter(
                meal_plan_id=item.meal_plan_id, recipe_batch_id=item.recipe_batch_id
            ).update(portions=item.servings, is_portions_overridden=item.servings is not None)
    if label is not ...:
        clean = (label or '').strip()[:LABEL_MAX_LENGTH]
        if not clean and not item.recipe_id:
            raise BasketError('label is required for a dish without recipe', 400, 'missing_dish')
        item.label = clean or item.label
        update_fields.append('label')
    item.save(update_fields=update_fields)
    return item


def _remove_batch_from_lists(batch_id: int) -> None:
    """Retire les ingrédients apportés par un batch des listes de courses (comme `remove_batch`)."""
    list_ids = list(
        ShoppingListBatch.objects.filter(recipe_batch_id=batch_id).values_list('shopping_list_id', flat=True)
    )
    ShoppingListBatch.objects.filter(recipe_batch_id=batch_id).delete()
    ShoppingListItemQuantity.objects.filter(recipe_batch_id=batch_id).delete()
    if list_ids:
        ShoppingListItem.objects.filter(shopping_list_id__in=list_ids).annotate(qcount=Count('quantities')).filter(
            qcount=0
        ).delete()


def remove_item(item: BasketItem) -> None:
    """
    Retire l'envie du panier. Datée mais pas cuisinée : le plat est retiré du repas (repas créé pour
    elle → supprimé s'il est vide). Cuisinée : le repas et le post restent (historique).
    Batch devenu orphelin : supprimé, ses ingrédients sortent de la liste de courses.
    """
    with transaction.atomic():
        if item.meal_plan_id and not item.meal_plan.confirmed:
            _detach_from_meal_plan(item)
        batch_id = item.recipe_batch_id
        item.delete()
        if (
            batch_id
            and not MealPlanRecipeBatch.objects.filter(recipe_batch_id=batch_id).exists()
            and not BasketItem.objects.filter(recipe_batch_id=batch_id).exists()
            and not Post.objects.filter(recipe_batch_id=batch_id).exists()
        ):
            _remove_batch_from_lists(batch_id)
            RecipeBatch.objects.filter(id=batch_id).delete()


def reorder(user: AbstractBaseUser, week: date_type, item_ids) -> list[BasketItem]:
    if not isinstance(item_ids, list) or not item_ids:
        raise BasketError('item_ids must be a non-empty list')
    try:
        ids = [int(i) for i in item_ids]
    except (TypeError, ValueError) as exc:
        raise BasketError('item_ids must contain integers') from exc
    items = {i.id: i for i in BasketItem.objects.filter(user=user, week_start=week)}
    if any(i not in items for i in ids):
        raise BasketError('Some items are not in this week', 400, 'unknown_items')
    ordered = ids + [i for i in sorted(items, key=lambda k: (items[k].position, k)) if i not in ids]
    with transaction.atomic():
        for position, item_id in enumerate(ordered):
            if items[item_id].position != position:
                BasketItem.objects.filter(id=item_id).update(position=position, updated_at=timezone.now())
    return week_items(user, week)


# ---------------------------------------------------------------------------
# Batch de recette (liste de courses, repas daté)
# ---------------------------------------------------------------------------
def ensure_batch(item: BasketItem) -> RecipeBatch | None:
    """Batch de la recette de l'envie (créé à la demande ; celui du repas lié s'il contient la recette)."""
    if not item.recipe_id:
        return None
    if item.recipe_batch_id:
        batch = RecipeBatch.objects.filter(id=item.recipe_batch_id).select_related('recipe').first()
        if batch:
            return batch
    batch = None
    if item.meal_plan_id:
        mprb = (
            MealPlanRecipeBatch.objects.filter(meal_plan_id=item.meal_plan_id, recipe_batch__recipe_id=item.recipe_id)
            .select_related('recipe_batch__recipe')
            .first()
        )
        batch = mprb.recipe_batch if mprb else None
    if batch is None:
        batch = RecipeBatch.objects.create(recipe_id=item.recipe_id, created_by_id=item.user_id)
    item.recipe_batch = batch
    item.save(update_fields=['recipe_batch', 'updated_at'])
    return batch


def attach_batch_to_meal_plan(item: BasketItem, meal_plan: MealPlan) -> bool:
    """Ajoute la recette de l'envie au repas avec le batch de l'envie (liste de courses conservée)."""
    if not item.recipe_id:
        return False
    if MealPlanRecipeBatch.objects.filter(meal_plan=meal_plan, recipe_batch__recipe_id=item.recipe_id).exists():
        return False
    batch = ensure_batch(item)
    order = (MealPlanRecipeBatch.objects.filter(meal_plan=meal_plan).aggregate(m=Max('order'))['m'] or 0) + 1
    MealPlanRecipeBatch.objects.create(
        meal_plan=meal_plan,
        recipe_batch=batch,
        portions=item.servings,
        is_portions_overridden=item.servings is not None,
        order=order,
    )
    return True


# ---------------------------------------------------------------------------
# Dater / dé-dater
# ---------------------------------------------------------------------------
def _meal_is_free(meal_plan: MealPlan) -> bool:
    """Repas réutilisable pour y ajouter une envie : ni cuisiné, ni posté."""
    return not meal_plan.confirmed and not Post.objects.filter(meal_plan=meal_plan).exists()


def _detach_from_meal_plan(item: BasketItem) -> None:
    """Retire le plat de l'envie de son repas (non cuisiné) ; repas créé pour elle et vide → supprimé."""
    meal_plan = item.meal_plan
    if meal_plan is None or meal_plan.confirmed:
        return
    if item.recipe_batch_id:
        MealPlanRecipeBatch.objects.filter(meal_plan=meal_plan, recipe_batch_id=item.recipe_batch_id).delete()
    elif not item.recipe_id and (meal_plan.custom_label or '').strip().lower() == item.label[:80].lower():
        if meal_plan.meal_time != 'other':
            meal_plan.custom_label = ''
            meal_plan.save(update_fields=['custom_label', 'updated_at'])
    if (
        item.owns_meal_plan
        and not MealPlanRecipeBatch.objects.filter(meal_plan=meal_plan).exists()
        and not Post.objects.filter(meal_plan=meal_plan).exists()
        and not meal_plan.invitations.exists()
        and not BasketItem.objects.filter(meal_plan=meal_plan).exclude(id=item.id).exists()
    ):
        meal_plan.delete()
    item.meal_plan = None
    item.owns_meal_plan = False


def schedule_item(
    user: AbstractBaseUser,
    item: BasketItem,
    *,
    date,
    meal_time=None,
    meal_plan_id=None,
) -> BasketItem:
    """
    Date l'envie : elle devient un repas planifié normal (invitations, mode cuisine, etc.).
    `meal_plan_id` : rejoint un repas existant de l'utilisateur. Sinon créneau standard : rejoint le
    repas du créneau s'il est libre (recette ajoutée au repas), sinon nouveau créneau « other »
    (jamais de conflit avec l'unicité (user, date, slot_key)).
    """
    if item.meal_plan_id and item.meal_plan.confirmed:
        raise BasketError('This dish is already cooked', 400, 'already_cooked')

    target = None
    mid = _parse_int(meal_plan_id, 'meal_plan_id')
    if mid is not None:
        target = MealPlan.objects.filter(user=user, id=mid).first()
        if not target:
            raise BasketError('Meal plan not found', 404, 'meal_plan_not_found')
        if not _meal_is_free(target):
            raise BasketError('This meal is already cooked', 400, 'meal_already_cooked')
        target_date = target.date
    else:
        target_date = _parse_date(date, 'date')
        meal_time = (str(meal_time or 'dinner')).strip().lower()
        if meal_time not in _MEAL_TIMES:
            raise BasketError(f'meal_time must be one of: {", ".join(_MEAL_TIMES)}')

    dish_label = item.label[:80] if not item.recipe_id else ''
    with transaction.atomic():
        if item.meal_plan_id:
            if target is not None and target.id == item.meal_plan_id:
                return item
            _detach_from_meal_plan(item)

        created = False
        if target is None and meal_time in _STANDARD_SLOTS:
            existing = MealPlan.objects.filter(user=user, date=target_date, slot_key=meal_time).first()
            if existing is None:
                target = MealPlan.objects.create(
                    user=user,
                    date=target_date,
                    meal_time=meal_time,
                    slot_key=meal_time,
                    custom_label=dish_label,
                    meal_type='recipe',
                    confirmed=False,
                    guest_count=item.guest_count,
                    excluded_household_ids=list(item.excluded_household_ids or []),
                )
                created = True
            elif _meal_is_free(existing) and not (existing.custom_label or '').strip():
                # Créneau libre sans plat libre : une recette s'ajoute au repas ; un plat libre
                # seulement si le créneau est vide. Sinon nouveau créneau « other » (pas d'écrasement).
                if item.recipe_id or not MealPlanRecipeBatch.objects.filter(meal_plan=existing).exists():
                    target = existing
        if target is None:
            target = MealPlan.objects.create(
                user=user,
                date=target_date,
                meal_time='other',
                slot_key=str(uuid.uuid4()),
                custom_label=(dish_label or item.display_title or 'Repas')[:80],
                meal_type='recipe',
                confirmed=False,
                guest_count=item.guest_count,
                excluded_household_ids=list(item.excluded_household_ids or []),
            )
            created = True

        if item.recipe_id:
            attach_batch_to_meal_plan(item, target)
            if target.meal_type != 'recipe':
                target.meal_type = 'recipe'
                target.save(update_fields=['meal_type', 'updated_at'])
        elif not created and not (target.custom_label or '').strip():
            target.custom_label = dish_label
            target.save(update_fields=['custom_label', 'updated_at'])

        item.meal_plan = target
        item.owns_meal_plan = created
        new_week = monday_of(target_date)
        if new_week != item.week_start:
            item.week_start = new_week
            item.position = _next_position(user, new_week)
        item.save()
    return item


def unschedule_item(item: BasketItem) -> BasketItem:
    """Remet l'envie dans le panier sans date (le repas créé pour elle est supprimé s'il est vide)."""
    if not item.meal_plan_id:
        return item
    if item.meal_plan.confirmed:
        raise BasketError('This dish is already cooked', 400, 'already_cooked')
    with transaction.atomic():
        _detach_from_meal_plan(item)
        item.save()
    return item


# ---------------------------------------------------------------------------
# Report sur la semaine suivante (à la demande, jamais automatique)
# ---------------------------------------------------------------------------
def carry_over(user: AbstractBaseUser, *, from_week=None, to_week=None, item_ids=None) -> list[BasketItem]:
    """
    Recopie les envies ouvertes (ni cuisinées ni déjà reportées) de `from_week` (défaut : semaine
    précédente) vers `to_week` (défaut : semaine courante). Les originales restent (historique).
    Idempotent : une envie n'est reportée qu'une fois. Le batch (liste de courses) suit l'envie.
    """
    current = current_week_start(user)
    source = parse_week(from_week, user) if from_week not in (None, '') else current - timedelta(days=7)
    target = parse_week(to_week, user) if to_week not in (None, '') else max(current, source + timedelta(days=7))
    if target <= source:
        raise BasketError('to_week must be after from_week')

    qs = basket_queryset(user).filter(week_start=source, carried_over_to__isnull=True)
    qs = qs.exclude(meal_plan__confirmed=True)
    if item_ids is not None:
        if not isinstance(item_ids, list):
            raise BasketError('item_ids must be a list')
        try:
            qs = qs.filter(id__in=[int(i) for i in item_ids])
        except (TypeError, ValueError) as exc:
            raise BasketError('item_ids must contain integers') from exc

    created: list[BasketItem] = []
    with transaction.atomic():
        position = _next_position(user, target)
        for item in qs.order_by('position', 'id'):
            copy = BasketItem.objects.create(
                user=user,
                week_start=target,
                recipe_id=item.recipe_id,
                label=item.label,
                servings=item.servings,
                guest_count=item.guest_count,
                excluded_household_ids=list(item.excluded_household_ids or []),
                position=position,
                recipe_batch_id=item.recipe_batch_id,
                inspired_by_post_id=item.inspired_by_post_id,
            )
            position += 1
            item.carried_over_to = copy
            item.save(update_fields=['carried_over_to', 'updated_at'])
            created.append(copy)
    return created


# ---------------------------------------------------------------------------
# Liste de courses depuis le panier
# ---------------------------------------------------------------------------
@dataclass
class ShoppingResult:
    shopping_list: ShoppingList
    created_list: bool
    added_item_ids: list[int] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


def items_for_shopping(user: AbstractBaseUser, *, item_ids=None, week=None) -> list[BasketItem]:
    """
    Envies à mettre dans la liste : `item_ids` explicites (toutes doivent exister), sinon les envies
    ouvertes avec recette de la semaine `week` (défaut : courante).
    """
    if item_ids is not None:
        if not isinstance(item_ids, list) or not item_ids:
            raise BasketError('item_ids must be a non-empty list')
        try:
            ids = [int(i) for i in item_ids]
        except (TypeError, ValueError) as exc:
            raise BasketError('item_ids must contain integers') from exc
        items = list(basket_queryset(user).filter(id__in=ids).order_by('week_start', 'position', 'id'))
        if len(items) != len(set(ids)):
            raise BasketError('Basket item not found', 404, 'basket_item_not_found')
        return items
    target_week = parse_week(week, user)
    week_now = current_week_start(user)
    return [i for i in week_items(user, target_week) if item_status(i, week_now) in OPEN_STATUSES and i.recipe_id]


def add_to_shopping_list(
    user: AbstractBaseUser,
    items: list[BasketItem],
    *,
    shopping_list_id=None,
    name: str | None = None,
) -> ShoppingResult:
    """
    Ajoute les ingrédients des envies à une liste (existante dont on est membre, ou nouvelle), avec
    la logique des repas (`link_batch_to_shopping_list` : une ligne par ingrédient et groupe d'unités,
    quantités par batch additionnées). Idempotent : une envie déjà dans cette liste est ignorée.
    """
    from recipes.services.shopping_list_service import link_batch_to_shopping_list

    shopping_list = None
    lid = _parse_int(shopping_list_id, 'shopping_list_id')
    if lid is not None:
        shopping_list = ShoppingList.objects.filter(id=lid, members__user=user).first()
        if not shopping_list:
            raise BasketError('Shopping list not found', 404, 'shopping_list_not_found')

    week_now = current_week_start(user)
    result = ShoppingResult(shopping_list=shopping_list, created_list=False)
    with transaction.atomic():
        if shopping_list is None:
            shopping_list = ShoppingList.objects.create(name=(name or DEFAULT_SHOPPING_LIST_NAME).strip()[:200])
            ShoppingListMember.objects.create(shopping_list=shopping_list, user=user, role='owner')
            result.shopping_list = shopping_list
            result.created_list = True
        for item in items:
            if not item.recipe_id:
                result.skipped.append({'id': item.id, 'reason': 'no_recipe'})
                continue
            if item_status(item, week_now) == STATUS_COOKED:
                result.skipped.append({'id': item.id, 'reason': 'already_cooked'})
                continue
            batch = ensure_batch(item)
            if ShoppingListBatch.objects.filter(recipe_batch=batch, shopping_list=shopping_list).exists():
                result.skipped.append({'id': item.id, 'reason': 'already_in_list'})
                continue
            link_batch_to_shopping_list(shopping_list, batch)
            result.added_item_ids.append(item.id)
        if result.added_item_ids:
            shopping_list.save(update_fields=['updated_at'])
    return result


# ---------------------------------------------------------------------------
# « À mon tour »
# ---------------------------------------------------------------------------
def can_see_post(user: AbstractBaseUser, post: Post) -> bool:
    if not post.is_published:
        return False
    if post.user_id == user.id:
        return True
    from accounts.blocks import blocked_user_ids_for
    from accounts.privacy import can_view_profile_content

    if post.user_id in blocked_user_ids_for(user):
        return False
    if post.is_public:  # partagé avec la communauté (Suggestions du feed)
        return True
    return can_view_profile_content(user, post.user)


def post_dish(post: Post) -> tuple[Recipe | None, str]:
    """Plat d'un post : recette de couverture (ou 1re recette du repas), sinon titre affichable."""
    from recipes.services.cooked_service import post_display_title

    recipe = post.recipe_batch.recipe if post.recipe_batch_id and post.recipe_batch else None
    if recipe is None and post.meal_plan_id:
        mprb = (
            MealPlanRecipeBatch.objects.filter(meal_plan_id=post.meal_plan_id)
            .select_related('recipe_batch__recipe')
            .order_by('order', 'id')
            .first()
        )
        recipe = mprb.recipe_batch.recipe if mprb else None
    label = (post.label or '').strip() if recipe else (post_display_title(post) or '')
    return recipe, label[:LABEL_MAX_LENGTH]


NOT_COOKED = Q(meal_plan__isnull=True) | Q(meal_plan__confirmed=False)


def my_turn_items(user: AbstractBaseUser, post: Post) -> QuerySet[BasketItem]:
    return basket_queryset(user).filter(inspired_by_post=post)


def has_my_turn(user: AbstractBaseUser, post: Post) -> bool:
    """« À mon tour » actif : une envie de ce post pas encore cuisinée (re-tapable après l'avoir refait)."""
    return BasketItem.objects.filter(NOT_COOKED, user=user, inspired_by_post=post).exists()


def my_turn_count(post: Post) -> int:
    return BasketItem.objects.filter(inspired_by_post=post).values('user_id').distinct().count()


def add_my_turn(user: AbstractBaseUser, post: Post, *, week=None) -> tuple[BasketItem, bool]:
    """Ajoute le plat du post au panier (idempotent) et prévient l'auteur (une fois par ami et par post)."""
    existing = my_turn_items(user, post).filter(NOT_COOKED, carried_over_to__isnull=True).order_by('-id').first()
    if existing:
        return existing, False
    recipe, label = post_dish(post)
    if not recipe and not label:
        raise BasketError('This post has no dish to add', 400, 'missing_dish')
    item = add_item(
        user,
        recipe_id=recipe.id if recipe else None,
        label=label if not recipe else (label or recipe.title),
        week=week,
        inspired_by_post=post,
    )
    _notify_author_once('a_mon_tour', post, actor=user)
    return item, True


def remove_my_turn(user: AbstractBaseUser, post: Post) -> int:
    """Annule « À mon tour » : retire du panier les envies de ce post qui ne sont pas encore cuisinées."""
    removed = 0
    for item in my_turn_items(user, post).filter(NOT_COOKED, carried_over_to__isnull=True):
        remove_item(item)
        removed += 1
    return removed


def _notify_author_once(type_key: str, post: Post, *, actor, data: dict | None = None) -> None:
    """Notification à l'auteur du post, une seule fois par (ami, post) : pas de spam par aller-retour."""
    from accounts.models import Notification
    from notifications.registry import get_type
    from notifications.services import notify

    if post.user_id == actor.id:
        return
    inbox_type = get_type(type_key).inbox_type
    already = Notification.objects.filter(
        user_id=post.user_id, notification_type=inbox_type, related_user=actor, related_post_id=post.id
    ).exists()
    if not already:
        notify(type_key, post.user, actor=actor, post=post, data=data)


# ---------------------------------------------------------------------------
# Cuisiné depuis le panier (appelé par cooked_service.log_cooked)
# ---------------------------------------------------------------------------
def resolve_basket_item_for_cooked(
    user: AbstractBaseUser,
    *,
    basket_item_id=None,
    meal_plan: MealPlan | None = None,
    cooked_on: date_type | None = None,
    recipe: Recipe | None = None,
    label: str = '',
) -> BasketItem | None:
    """
    Envie cuisinée par ce log : explicite (`basket_item_id`), sinon celle liée au repas, sinon une
    envie non datée de la semaine qui correspond au plat (cuisiner « au feeling » coche le panier).
    """
    bid = _parse_int(basket_item_id, 'basket_item_id')
    if bid is not None:
        item = basket_queryset(user).filter(id=bid).first()
        if not item:
            raise BasketError('Basket item not found', 404, 'basket_item_not_found')
        return item
    if meal_plan is not None:
        return basket_queryset(user).filter(meal_plan=meal_plan).order_by('id').first()
    if cooked_on is None or not (recipe or label):
        return None
    dish = Q(recipe=recipe) if recipe else Q(recipe__isnull=True, label__iexact=label)
    return (
        basket_queryset(user)
        .filter(dish, week_start=monday_of(cooked_on), meal_plan__isnull=True, carried_over_to__isnull=True)
        .order_by('position', 'id')
        .first()
    )


def mark_item_cooked(item: BasketItem, meal_plan: MealPlan, *, meal_plan_created: bool) -> dict:
    """Rattache l'envie au repas cuisiné ; renvoie l'état précédent (pour `undo_cooked`)."""
    undo = {'id': item.id, 'meal_plan_id': item.meal_plan_id, 'owns_meal_plan': item.owns_meal_plan}
    if item.meal_plan_id != meal_plan.id:
        item.meal_plan = meal_plan
        item.owns_meal_plan = meal_plan_created
        item.save(update_fields=['meal_plan', 'owns_meal_plan', 'updated_at'])
    return undo


def restore_item_after_undo(state: dict | None) -> None:
    if not state or not state.get('id'):
        return
    BasketItem.objects.filter(id=state['id']).update(
        meal_plan_id=state.get('meal_plan_id'),
        owns_meal_plan=bool(state.get('owns_meal_plan')),
        updated_at=timezone.now(),
    )


def notify_inspiration_cooked(post: Post) -> None:
    """« Léa a refait « Quiche ». » à l'auteur du post d'origine (agrégé, une fois par ami et par post)."""
    original = post.inspired_by_post
    if original is None:
        return
    _notify_author_once('a_mon_tour_cooked', original, actor=post.user, data={'cooked_post_id': post.id})
