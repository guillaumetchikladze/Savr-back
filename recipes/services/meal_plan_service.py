"""Service sync pour les meal plans — réutilisable par ViewSets et agent chat."""

import uuid
from datetime import date as date_type, datetime

from django.contrib.auth.models import AbstractBaseUser
from django.db import transaction
from django.db.models import Case, IntegerField, Max, Prefetch, Q, When

from chat.services.tool_schemas import (
    AddRecipeResult,
    CreateMealPlanSlotResult,
    MealPlanInviteeSummary,
    MealPlanRecipeSummary,
    MealPlanSummary,
    MutationProposal,
)
from recipes.models import (
    CookingProgress,
    MealInvitation,
    MealPlan,
    MealPlanRecipeBatch,
    Post,
    PostPhoto,
    Recipe,
    RecipeBatch,
)
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


def infer_meal_time_from_hour(hour: int | None = None) -> str:
    """Infère breakfast/lunch/dinner selon l'heure locale."""
    if hour is None:
        hour = datetime.now().hour
    if hour < 11:
        return 'breakfast'
    if hour < 15:
        return 'lunch'
    return 'dinner'


def _resolve_composer_slot_key(user, target_date, meal_time, exclude_meal_plan_id=None):
    """
    Détermine meal_time + slot_key pour un slot composeur.
    Si le créneau standard est pris, bascule sur other + UUID.
    """
    meal_time = (meal_time or '').strip().lower()
    if meal_time not in _STANDARD_MEAL_TIMES and meal_time != 'other':
        meal_time = 'lunch'

    custom_label = ''
    if meal_time in _STANDARD_MEAL_TIMES:
        slot_key = meal_time
        conflict_qs = MealPlan.objects.filter(
            user=user,
            date=target_date,
            slot_key=slot_key,
        )
        if exclude_meal_plan_id:
            conflict_qs = conflict_qs.exclude(id=exclude_meal_plan_id)
        if conflict_qs.exists():
            meal_time = 'other'
            slot_key = str(uuid.uuid4())
            custom_label = 'Repas'
    else:
        slot_key = str(uuid.uuid4())
        custom_label = 'Repas'

    return meal_time, slot_key, custom_label


def _composer_slot_has_published_post(meal_plan_id: int) -> bool:
    return Post.objects.filter(meal_plan_id=meal_plan_id, is_published=True).exists()


def _is_reusable_composer_draft(meal_plan: MealPlan | None) -> bool:
    """
    Slot composeur réutilisable : brouillon non confirmé, sans post publié,
    sans recettes (on ne vole pas un vrai repas planifié).
    """
    if not meal_plan or meal_plan.confirmed:
        return False
    if _composer_slot_has_published_post(meal_plan.id):
        return False
    if MealPlanRecipeBatch.objects.filter(meal_plan_id=meal_plan.id).exists():
        return False
    return True


def _cleanup_empty_sibling_composer_drafts(
    user: AbstractBaseUser,
    target_date: date_type,
    keep_id: int,
) -> int:
    """Supprime les brouillons composeur vides du même jour (sauf keep_id)."""
    deleted = 0
    siblings = MealPlan.objects.filter(
        user=user,
        date=target_date,
        confirmed=False,
    ).exclude(id=keep_id)
    for sibling in siblings:
        if not _is_reusable_composer_draft(sibling):
            continue
        if PostPhoto.objects.filter(meal_plan_id=sibling.id).exists():
            continue
        sibling.delete()
        deleted += 1
    return deleted


def create_composer_slot(
    user: AbstractBaseUser,
    date: str | None = None,
    meal_time: str | None = None,
    scheduled_time: str | None = None,
) -> CreateMealPlanSlotResult:
    """
    Get-or-create d'un slot draft pour le composeur de post.

    Réutilise un brouillon vide / non publié du même jour+créneau au lieu
    d'empiler des meal plans « other » à chaque ouverture / remount.
    """
    if date:
        try:
            target_date = date_type.fromisoformat(date)
        except ValueError as exc:
            raise ValueError('date invalide (attendu YYYY-MM-DD).') from exc
    else:
        target_date = date_type.today()

    desired_meal_time = (meal_time or infer_meal_time_from_hour() or 'lunch').strip().lower()
    if desired_meal_time not in _STANDARD_MEAL_TIMES and desired_meal_time != 'other':
        desired_meal_time = 'lunch'

    scheduled_save = _parse_scheduled_time(scheduled_time) if scheduled_time else None

    def _finish(meal_plan: MealPlan, created: bool) -> CreateMealPlanSlotResult:
        if scheduled_save is not None and meal_plan.scheduled_time != scheduled_save:
            meal_plan.scheduled_time = scheduled_save
            meal_plan.save(update_fields=['scheduled_time', 'updated_at'])
        _cleanup_empty_sibling_composer_drafts(user, target_date, meal_plan.id)
        return CreateMealPlanSlotResult(
            meal_plan_id=meal_plan.id,
            date=meal_plan.date.isoformat(),
            meal_time=meal_plan.meal_time,
            created=created,
            message='Slot composeur créé.' if created else 'Slot composeur réutilisé.',
        )

    # 1) Créneau standard libre ou brouillon réutilisable → get-or-create
    if desired_meal_time in _STANDARD_MEAL_TIMES:
        existing = MealPlan.objects.filter(
            user=user,
            date=target_date,
            slot_key=desired_meal_time,
        ).first()
        if existing and _is_reusable_composer_draft(existing):
            return _finish(existing, created=False)
        if not existing:
            meal_plan = MealPlan.objects.create(
                user=user,
                date=target_date,
                meal_time=desired_meal_time,
                slot_key=desired_meal_time,
                custom_label='',
                scheduled_time=scheduled_save,
                meal_type='unknown',
                confirmed=False,
            )
            return _finish(meal_plan, created=True)
        # Sinon : créneau déjà pris par un vrai repas → bascule sur other

    # 2) Réutiliser un brouillon « other » vide du jour (évite la prolifération)
    other_drafts = MealPlan.objects.filter(
        user=user,
        date=target_date,
        meal_time='other',
        confirmed=False,
    ).order_by('-updated_at')
    for candidate in other_drafts:
        if _is_reusable_composer_draft(candidate):
            return _finish(candidate, created=False)

    # 3) Nouveau slot other
    meal_plan = MealPlan.objects.create(
        user=user,
        date=target_date,
        meal_time='other',
        slot_key=str(uuid.uuid4()),
        custom_label='Repas',
        scheduled_time=scheduled_save,
        meal_type='unknown',
        confirmed=False,
    )
    return _finish(meal_plan, created=True)


def discard_composer_slot(user: AbstractBaseUser, meal_plan_id: int) -> dict:
    """Supprime un brouillon composeur abandonné (sans recettes ni post publié)."""
    meal_plan = MealPlan.objects.filter(user=user, id=meal_plan_id).first()
    if not meal_plan:
        raise ValueError('Meal plan introuvable.')
    if meal_plan.confirmed or _composer_slot_has_published_post(meal_plan.id):
        raise ValueError('Impossible de supprimer un repas confirmé ou publié.')
    if MealPlanRecipeBatch.objects.filter(meal_plan_id=meal_plan.id).exists():
        raise ValueError('Ce repas contient des recettes.')

    PostPhoto.objects.filter(
        meal_plan_id=meal_plan.id,
        post__isnull=True,
    ).delete()
    meal_plan.delete()
    return {'deleted': True, 'meal_plan_id': meal_plan_id}


def _last_step_index_for_batch(batch: RecipeBatch) -> int:
    recipe = getattr(batch, 'recipe', None)
    if not recipe:
        return 0
    total_steps = recipe.steps.count()
    return max(total_steps - 1, 0)


def complete_recipe_batch_workflow(user: AbstractBaseUser, batch: RecipeBatch) -> RecipeBatch:
    """
    Marque un batch comme entièrement terminé lors de la publication d'un post :
    courses faites, progression au dernier step, batch cuisiné.
    """
    last_index = _last_step_index_for_batch(batch)
    batch_updates = []

    if not batch.shopping_done:
        batch.shopping_done = True
        batch_updates.append('shopping_done')

    progress = CookingProgress.objects.filter(
        user=user,
        recipe_batch=batch,
        status='in_progress',
    ).first()

    if progress:
        if progress.current_step_index < last_index:
            progress.current_step_index = last_index
            progress.save(update_fields=['current_step_index', 'updated_at'])
        progress.complete()
    else:
        progress = (
            CookingProgress.objects.filter(
                user=user,
                recipe_batch=batch,
                status='completed',
            )
            .order_by('-updated_at')
            .first()
        )
        if progress:
            if progress.current_step_index < last_index:
                progress.current_step_index = last_index
                progress.save(update_fields=['current_step_index', 'updated_at'])
            if not batch.is_cooked:
                batch.is_cooked = True
                batch_updates.append('is_cooked')
        else:
            progress = CookingProgress.objects.create(
                user=user,
                recipe_batch=batch,
                current_step_index=last_index,
                status='in_progress',
            )
            progress.complete()

    if batch_updates:
        batch.save(update_fields=batch_updates + ['updated_at'])
        batch.refresh_from_db()
    return batch


def complete_meal_plan_batches_for_publish(user: AbstractBaseUser, meal_plan: MealPlan) -> None:
    """Finalise tous les batches d'un meal plan (publication du repas)."""
    batch_ids = list(
        meal_plan.meal_plan_recipe_batches.values_list('recipe_batch_id', flat=True)
    )
    batch_ids = [bid for bid in batch_ids if bid]
    if not batch_ids:
        return
    batches = RecipeBatch.objects.filter(id__in=batch_ids).select_related('recipe')
    for batch in batches:
        complete_recipe_batch_workflow(user, batch)


def relink_composer_photos_to_meal_plan(
    user: AbstractBaseUser,
    meal_plan: MealPlan,
    photo_ids: list[int],
) -> int:
    """
    Rattache les photos du composeur (brouillon) au meal plan courant.

    Couvre le cas où le front a changé de slot draft ou uploadé sur un ancien
    meal_plan_id — les photos restent en « ambiance générale » (recipe_batch null).
    """
    if not photo_ids:
        return 0
    return PostPhoto.objects.filter(
        id__in=photo_ids,
        post__isnull=True,
        is_draft=False,
        uploaded_by=user,
    ).filter(
        Q(meal_plan__isnull=True)
        | Q(meal_plan__user=user, meal_plan__confirmed=False)
    ).exclude(
        meal_plan_id=meal_plan.id
    ).update(meal_plan_id=meal_plan.id)


def update_composer_slot(
    user: AbstractBaseUser,
    meal_plan_id: int,
    date: str,
    meal_time: str,
    scheduled_time: str | None = None,
    guest_count: int | None = None,
    update_scheduled_time: bool = False,
    slot_label: str | None = None,
) -> CreateMealPlanSlotResult:
    """Met à jour date/créneau d'un slot draft composeur (photos conservées sur le même meal_plan)."""
    meal_plan = MealPlan.objects.filter(user=user, id=meal_plan_id).first()
    if not meal_plan:
        raise ValueError('Meal plan introuvable.')
    if meal_plan.confirmed:
        raise ValueError('Ce repas est déjà confirmé.')
    if Post.objects.filter(meal_plan=meal_plan, is_published=True).exists():
        raise ValueError('Un post est déjà publié pour ce repas.')

    try:
        target_date = date_type.fromisoformat(date)
    except ValueError as exc:
        raise ValueError('date invalide (attendu YYYY-MM-DD).') from exc

    resolved_meal_time, slot_key, custom_label = _resolve_composer_slot_key(
        user, target_date, meal_time, exclude_meal_plan_id=meal_plan.id
    )

    meal_plan.date = target_date
    meal_plan.meal_time = resolved_meal_time
    meal_plan.slot_key = slot_key
    slot_label_clean = (slot_label or '').strip()
    if resolved_meal_time == 'other':
        if slot_label_clean:
            meal_plan.custom_label = slot_label_clean[:80]
        elif not (meal_plan.custom_label or '').strip():
            meal_plan.custom_label = custom_label
    elif slot_label_clean:
        meal_plan.custom_label = ''

    update_fields = ['date', 'meal_time', 'slot_key', 'custom_label', 'updated_at']
    if update_scheduled_time:
        meal_plan.scheduled_time = (
            _parse_scheduled_time(scheduled_time) if scheduled_time else None
        )
        update_fields.append('scheduled_time')
    if guest_count is not None:
        meal_plan.guest_count = max(0, int(guest_count))
        update_fields.append('guest_count')

    meal_plan.save(update_fields=update_fields)

    return CreateMealPlanSlotResult(
        meal_plan_id=meal_plan.id,
        date=meal_plan.date.isoformat(),
        meal_time=meal_plan.meal_time,
        created=False,
        message='Slot composeur mis à jour.',
    )
