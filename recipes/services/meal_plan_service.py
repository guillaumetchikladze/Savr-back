"""Service sync pour les meal plans — réutilisable par ViewSets et agent chat."""

import uuid
from datetime import date as date_type, datetime

from django.contrib.auth.models import AbstractBaseUser
from django.db import transaction
from django.db.models import Case, IntegerField, Max, Prefetch, When

from chat.services.tool_schemas import (
    AddRecipeResult,
    CreateMealPlanSlotResult,
    MealPlanInviteeSummary,
    MealPlanRecipeSummary,
    MealPlanSummary,
    MutationProposal,
)
from recipes.models import MealInvitation, MealPlan, MealPlanRecipeBatch, Recipe, RecipeBatch
from recipes.utils import get_accessible_meal_plan_filter


def _meal_time_order():
    return Case(
        When(meal_time='lunch', then=0),
        When(meal_time='dinner', then=1),
        default=2,
        output_field=IntegerField(),
    )


def get_meal_plans_for_user(
    user: AbstractBaseUser,
    start_date: str,
    end_date: str,
) -> list[MealPlanSummary]:
    accessible_filter = get_accessible_meal_plan_filter(user)
    qs = (
        MealPlan.objects.filter(accessible_filter)
        .filter(date__gte=start_date, date__lte=end_date)
        .select_related('user')
        .prefetch_related(
            Prefetch(
                'meal_plan_recipe_batches',
                queryset=MealPlanRecipeBatch.objects.select_related(
                    'recipe_batch__recipe'
                ).order_by('order'),
            ),
            Prefetch(
                'invitations',
                queryset=MealInvitation.objects.select_related('invitee').order_by('invitee__username'),
            ),
        )
        .order_by('date', _meal_time_order())
        .distinct()
    )

    summaries = []
    for mp in qs:
        recipes = []
        for mprb in mp.meal_plan_recipe_batches.all():
            recipe = mprb.recipe_batch.recipe
            recipes.append(
                MealPlanRecipeSummary(
                    recipe_id=recipe.id,
                    recipe_title=recipe.title,
                    recipe_batch_id=mprb.recipe_batch_id,
                )
            )
        invitees = []
        if mp.user_id == user.id:
            for invitation in mp.invitations.all():
                if not invitation.invitee_id:
                    continue
                invitees.append(
                    MealPlanInviteeSummary(
                        username=invitation.invitee.username,
                        status=invitation.status,
                    )
                )
        summaries.append(
            MealPlanSummary(
                id=mp.id,
                date=mp.date.isoformat(),
                meal_time=mp.meal_time,
                meal_type=mp.meal_type,
                confirmed=mp.confirmed,
                is_owner=mp.user_id == user.id,
                recipes=recipes,
                invitees=invitees,
            )
        )
    return summaries


_STANDARD_MEAL_TIMES = frozenset({'breakfast', 'lunch', 'dinner'})


def _parse_scheduled_time(value: str | None):
    if not value:
        return None
    for fmt in ('%H:%M:%S', '%H:%M'):
        try:
            return datetime.strptime(value.strip(), fmt).time()
        except ValueError:
            continue
    raise ValueError('scheduled_time invalide (attendu HH:MM ou HH:MM:SS).')


def create_meal_plan_slot(
    user: AbstractBaseUser,
    date: str,
    meal_time: str,
    custom_label: str | None = None,
    scheduled_time: str | None = None,
) -> CreateMealPlanSlotResult:
    """Crée un créneau de planning vide, ou renvoie l'existant (user + date + slot_key)."""
    try:
        target_date = date_type.fromisoformat(date)
    except ValueError as exc:
        raise ValueError('date invalide (attendu YYYY-MM-DD).') from exc

    meal_time = (meal_time or '').strip().lower()
    if meal_time not in _STANDARD_MEAL_TIMES and meal_time != 'other':
        raise ValueError(
            'meal_time invalide (breakfast, lunch, dinner ou other).'
        )

    custom_label_save = ''
    scheduled_save = None
    if meal_time in _STANDARD_MEAL_TIMES:
        slot_key = meal_time
    else:
        custom_label_save = (custom_label or '').strip() or 'Repas'
        slot_key = str(uuid.uuid4())
        scheduled_save = _parse_scheduled_time(scheduled_time)

    existing = MealPlan.objects.filter(
        user=user,
        date=target_date,
        slot_key=slot_key,
    ).first()
    if existing:
        return CreateMealPlanSlotResult(
            meal_plan_id=existing.id,
            date=existing.date.isoformat(),
            meal_time=existing.meal_time,
            created=False,
            message='Créneau déjà existant.',
        )

    meal_plan = MealPlan.objects.create(
        user=user,
        date=target_date,
        meal_time=meal_time,
        slot_key=slot_key,
        custom_label=custom_label_save,
        scheduled_time=scheduled_save,
        meal_type='recipe',
        confirmed=False,
    )
    return CreateMealPlanSlotResult(
        meal_plan_id=meal_plan.id,
        date=meal_plan.date.isoformat(),
        meal_time=meal_plan.meal_time,
        created=True,
        message='Créneau créé.',
    )


def add_recipes_to_meal_plan(
    user: AbstractBaseUser,
    meal_plan_id: int,
    recipe_ids: list[int],
) -> AddRecipeResult:
    accessible_filter = get_accessible_meal_plan_filter(user)
    meal_plan = MealPlan.objects.filter(accessible_filter, id=meal_plan_id).first()
    if not meal_plan:
        raise ValueError(f'Meal plan {meal_plan_id} introuvable ou inaccessible.')

    if not isinstance(recipe_ids, list) or not recipe_ids:
        raise ValueError('recipe_ids doit être une liste non vide.')

    existing_recipe_ids = set(
        MealPlanRecipeBatch.objects.filter(meal_plan=meal_plan).values_list(
            'recipe_batch__recipe_id', flat=True
        )
    )
    current_max_order = (
        MealPlanRecipeBatch.objects.filter(meal_plan=meal_plan).aggregate(Max('order'))['order__max']
        or 0
    )
    already_present = [rid for rid in recipe_ids if rid in existing_recipe_ids]
    to_add = [rid for rid in recipe_ids if rid not in existing_recipe_ids]

    if to_add:
        existing_in_db = set(Recipe.objects.filter(id__in=to_add).values_list('id', flat=True))
        missing = [rid for rid in to_add if rid not in existing_in_db]
        if missing:
            raise ValueError(f'Recettes introuvables: {missing}')

    with transaction.atomic():
        if to_add:
            batches = [RecipeBatch(recipe_id=rid, created_by=user) for rid in to_add]
            RecipeBatch.objects.bulk_create(batches)
            mprs = [
                MealPlanRecipeBatch(
                    meal_plan=meal_plan,
                    recipe_batch=batches[i],
                    portions=None,
                    is_portions_overridden=False,
                    order=current_max_order + i + 1,
                )
                for i in range(len(batches))
            ]
            MealPlanRecipeBatch.objects.bulk_create(mprs)

    msg_parts = []
    if to_add:
        msg_parts.append(f'{len(to_add)} recette(s) ajoutée(s).')
    if already_present:
        msg_parts.append(f'{len(already_present)} déjà présente(s).')

    return AddRecipeResult(
        meal_plan_id=meal_plan.id,
        added_recipe_ids=to_add,
        already_present_recipe_ids=already_present,
        message=' '.join(msg_parts) or 'Aucun changement.',
    )


def remove_recipe_from_meal_plan(
    user: AbstractBaseUser,
    meal_plan_id: int,
    recipe_batch_id: int,
) -> dict:
    meal_plan = MealPlan.objects.filter(id=meal_plan_id).first()
    if not meal_plan:
        raise ValueError('Meal plan introuvable.')
    if meal_plan.user_id != user.id:
        raise PermissionError('Seul le propriétaire peut retirer une recette.')

    mprb = (
        MealPlanRecipeBatch.objects.filter(meal_plan=meal_plan, recipe_batch_id=recipe_batch_id)
        .select_related('recipe_batch')
        .first()
    )
    if not mprb:
        raise ValueError('Association introuvable.')

    batch_deleted = False
    with transaction.atomic():
        batch = mprb.recipe_batch
        mprb.delete()
        if not MealPlanRecipeBatch.objects.filter(recipe_batch_id=batch.id).exists():
            batch.delete()
            batch_deleted = True

    return {
        'batch_deleted': batch_deleted,
        'recipe_batch_id': recipe_batch_id,
        'meal_plan_id': meal_plan_id,
    }


def propose_meal_deletion_data(
    user: AbstractBaseUser,
    meal_plan_id: int,
    recipe_batch_id: int,
) -> MutationProposal:
    accessible_filter = get_accessible_meal_plan_filter(user)
    meal_plan = (
        MealPlan.objects.filter(accessible_filter, id=meal_plan_id)
        .select_related('user')
        .prefetch_related(
            Prefetch(
                'meal_plan_recipe_batches',
                queryset=MealPlanRecipeBatch.objects.select_related('recipe_batch__recipe'),
            )
        )
        .first()
    )
    if not meal_plan:
        raise ValueError('Meal plan introuvable ou inaccessible.')
    if meal_plan.user_id != user.id:
        raise PermissionError('Seul le propriétaire peut supprimer une recette du créneau.')

    mprb = next(
        (m for m in meal_plan.meal_plan_recipe_batches.all() if m.recipe_batch_id == recipe_batch_id),
        None,
    )
    if not mprb:
        raise ValueError('Recette introuvable dans ce créneau.')

    recipe = mprb.recipe_batch.recipe
    meal_label = meal_plan.get_meal_time_display()
    date_str = meal_plan.date.strftime('%d/%m/%Y')

    return MutationProposal(
        card_type='meal_deletion',
        title='Retirer une recette',
        subtitle=f'{recipe.title} — {meal_label} du {date_str}',
        details={
            'meal_plan_id': meal_plan.id,
            'recipe_batch_id': recipe_batch_id,
            'recipe_id': recipe.id,
            'recipe_title': recipe.title,
            'date': meal_plan.date.isoformat(),
            'meal_time': meal_plan.meal_time,
        },
        warnings=[],
    )


def execute_meal_deletion(user: AbstractBaseUser, payload: dict) -> dict:
    return remove_recipe_from_meal_plan(
        user,
        meal_plan_id=payload['meal_plan_id'],
        recipe_batch_id=payload['recipe_batch_id'],
    )
