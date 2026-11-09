"""
Actions « tout actionnable » de l'agent v2 (sync, testables sans LLM) : import d'une recette depuis un
lien collé, dater / déplacer / retirer un repas ou une envie du panier, inviter un·e ami·e à un repas ou
à une tablée.

Les wrappers async exposés au modèle sont dans `async_tools.py`. Tout passe par les services existants
(jamais par HTTP) :

- import : job Celery `process_recipe_import_from_url` (même pipeline que POST /recipes/import_from_url/) ;
- dater / dé-dater une envie : `basket_service.schedule_item` / `unschedule_item` ;
- déplacer un repas : `meal_plan_service.update_composer_slot` (créneau standard pris → « other ») ;
- inviter à un repas : `invitation_service.propose_meal_invitation` → carte de confirmation (PendingAction) ;
- inviter à une tablée : `tables.services.invite` (l'ami·e accepte ou refuse dans l'app).

Réversible = exécuté sans confirmation, avec un `UndoHint` (appel REST que l'app rejoue tel quel).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AbstractBaseUser
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from chat.services.kitchen_tools import normalize_meal_time, parse_day
from chat.services.tool_schemas import (
    CardAction,
    MealChangeDish,
    MealChangeResult,
    MutationProposal,
    RecipeImportResult,
    TableInvitationResult,
    TableInvitee,
    UndoHint,
)
from recipes.models import BasketItem, MealInvitation, MealPlan, MealPlanRecipeBatch, Post, PostPhoto
from recipes.services import basket_service as basket
from recipes.services.basket_service import BasketError

logger = logging.getLogger(__name__)
User = get_user_model()

IMPORT_STATUS_PATH = '/api/recipes/formalize/status/{request_id}/'
# Même lien recollé pendant que le job tourne : on renvoie ce job au lieu d'en lancer un second.
IMPORT_INFLIGHT_WINDOW = timedelta(minutes=15)
_MEAL_TIME_FR = {'breakfast': 'petit-déj', 'lunch': 'déjeuner', 'dinner': 'dîner', 'other': 'repas'}
_WEEKDAY_FR = ('lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche')


def _when(day, meal_time: str | None) -> str:
    """« jeudi 12/11 (dîner) »."""
    label = f'{_WEEKDAY_FR[day.weekday()]} {day.strftime("%d/%m")}'
    return f'{label} ({_MEAL_TIME_FR.get(meal_time or "", meal_time)})' if meal_time else label


# ---------------------------------------------------------------------------
# import_recipe_from_url : lien collé dans le chat
# ---------------------------------------------------------------------------
def _import_actions(recipe_id: int | None) -> list[CardAction]:
    """« Voir la recette » / « Ajouter à ma semaine » (recipe_id connu une fois le job terminé)."""
    return [
        CardAction(kind='open_recipe', label='Voir la recette', recipe_id=recipe_id),
        CardAction(
            kind='add_to_week',
            label='Ajouter à ma semaine',
            method='POST',
            path='/api/basket/',
            body={'recipe_id': recipe_id} if recipe_id else None,
            recipe_id=recipe_id,
        ),
    ]


def _chat_payload(conversation_id: int | None, base: dict) -> dict:
    if conversation_id:
        base['chat_context'] = {'conversation_id': conversation_id}
    return base


def start_recipe_import(user: AbstractBaseUser, url: str, conversation_id: int | None = None) -> RecipeImportResult:
    """
    Lance l'import (job Celery) d'une recette depuis un lien (Marmiton, blog, Instagram, TikTok…).
    Déjà importée et accessible → carte `success` sans nouveau job ; même lien en cours → même job.
    """
    from recipes.models import Recipe, RecipeImportRequest
    from recipes.tasks import process_recipe_import_from_url
    from recipes.utils import canonicalize_import_url

    url = (url or '').strip()
    if not url:
        raise ValueError('URL requise.')
    if not url.lower().startswith(('http://', 'https://')):
        url = f'https://{url}'

    canonical_url = canonicalize_import_url(url)
    candidate_urls = {canonical_url, url, canonical_url.rstrip('/'), canonical_url.rstrip('/') + '/'}

    existing = (
        Recipe.objects.filter(Q(is_public=True) | Q(created_by=user))
        .filter(import_source_url__in=list(candidate_urls))
        .order_by('-created_at')
        .first()
    )
    if existing:
        return RecipeImportResult(
            request_id='',
            url=url,
            job_type='import',
            status=RecipeImportRequest.STATUS_SUCCESS,
            already_imported=True,
            recipe_id=existing.id,
            title=existing.title,
            image_url=existing.image_url,
            actions=_import_actions(existing.id),
            message=f'« {existing.title} » est déjà dans Tchikook.',
        )

    inflight = (
        RecipeImportRequest.objects.filter(
            user=user,
            payload__url__in=list(candidate_urls),
            status__in=[RecipeImportRequest.STATUS_PENDING, RecipeImportRequest.STATUS_PROCESSING],
            created_at__gte=timezone.now() - IMPORT_INFLIGHT_WINDOW,
        )
        .order_by('-created_at')
        .first()
    )
    if inflight:
        import_request = inflight
    else:
        import_request = RecipeImportRequest.objects.create(
            user=user,
            payload=_chat_payload(conversation_id, {'url': url, 'source_type': 'imported', 'job_type': 'import'}),
            status=RecipeImportRequest.STATUS_PENDING,
        )
        task = process_recipe_import_from_url.delay(str(import_request.id))
        import_request.task_id = task.id
        import_request.save(update_fields=['task_id'])

    request_id = str(import_request.id)
    return RecipeImportResult(
        request_id=request_id,
        url=url,
        job_type='import',
        status=import_request.status,
        status_path=IMPORT_STATUS_PATH.format(request_id=request_id),
        actions=_import_actions(None),
        message='Import lancé : la recette arrive dans quelques secondes.'
        if not inflight
        else 'Import déjà en cours pour ce lien.',
    )


# ---------------------------------------------------------------------------
# Dater / déplacer / retirer
# ---------------------------------------------------------------------------
def _get_item(user: AbstractBaseUser, basket_item_id) -> BasketItem:
    try:
        return basket.get_item(user, basket_item_id)
    except BasketError as exc:
        raise ValueError('Envie introuvable dans ton panier (basket_item_id).') from exc


def _own_meal(user: AbstractBaseUser, meal_plan_id) -> MealPlan:
    try:
        mid = int(meal_plan_id)
    except (TypeError, ValueError) as exc:
        raise ValueError('meal_plan_id doit être un entier.') from exc
    meal = MealPlan.objects.filter(id=mid).first()
    if meal is None:
        raise ValueError('Repas introuvable.')
    if meal.user_id != user.id:
        raise ValueError('Seul·e l’hôte du repas peut le modifier.')
    return meal


def _future_day(user: AbstractBaseUser, value):
    today = basket.user_today(user)
    day = parse_day(value, today)
    if day < today:
        raise ValueError('Date passée : pour un plat déjà fait, utilise log_cooked.')
    return day


def _schedule_undo(item_id: int, *, previous_meal: MealPlan | None, previous: tuple | None) -> UndoHint:
    """Revenir à l'état d'avant : sans date (DELETE …/schedule/) ou à l'ancien créneau (POST …/schedule/)."""
    path = f'/api/basket/{item_id}/schedule/'
    if previous is None:
        return UndoHint(method='DELETE', path=path)
    if previous_meal is not None and MealPlan.objects.filter(id=previous_meal.id).exists():
        return UndoHint(method='POST', path=path, body={'meal_plan_id': previous_meal.id})
    return UndoHint(method='POST', path=path, body={'date': previous[0].isoformat(), 'meal_time': previous[1]})


def schedule_basket_item_for_user(
    user: AbstractBaseUser, *, basket_item_id, date, meal_time: str | None = None
) -> MealChangeResult:
    """Date une envie du panier (ou la change de jour / créneau) : elle devient un repas planifié."""
    item = _get_item(user, basket_item_id)
    if item.meal_plan_id and item.meal_plan.confirmed:
        raise ValueError('Ce plat est déjà cuisiné.')
    day = _future_day(user, date)
    slot = normalize_meal_time(meal_time) or 'dinner'
    previous_meal = item.meal_plan if item.meal_plan_id else None
    previous = (previous_meal.date, previous_meal.meal_time) if previous_meal else None

    try:
        item = basket.schedule_item(user, item, date=day.isoformat(), meal_time=slot)
    except BasketError as exc:
        raise ValueError(exc.message) from exc

    meal = item.meal_plan
    undo = _schedule_undo(item.id, previous_meal=previous_meal, previous=previous)
    verb = 'déplacé' if previous else 'prévu'
    return MealChangeResult(
        card_type='meal_scheduled',
        meal_plan_id=meal.id,
        basket_item_id=item.id,
        display_title=item.display_title,
        date=meal.date.isoformat(),
        meal_time=meal.meal_time,
        previous_date=previous[0].isoformat() if previous else None,
        previous_meal_time=previous[1] if previous else None,
        dishes=[
            MealChangeDish(
                basket_item_id=item.id, display_title=item.display_title, recipe_id=item.recipe_id, undo=undo
            )
        ],
        undo=undo,
        message=f'« {item.display_title} » {verb} {_when(meal.date, meal.meal_time)}.',
    )


def _invitee_names(meal: MealPlan) -> list[str]:
    return list(
        MealInvitation.objects.filter(meal_plan=meal, status__in=['pending', 'accepted'], invitee__isnull=False)
        .order_by('invitee__username')
        .values_list('invitee__username', flat=True)
    )


def _meal_title(meal: MealPlan) -> str:
    titles = [
        m.recipe_batch.recipe.title
        for m in MealPlanRecipeBatch.objects.filter(meal_plan=meal)
        .select_related('recipe_batch__recipe')
        .order_by('order', 'id')
    ]
    if titles:
        return ', '.join(titles[:3])
    label = (meal.custom_label or '').strip()
    return label or _MEAL_TIME_FR.get(meal.meal_time, 'repas')


def move_meal_for_user(
    user: AbstractBaseUser,
    *,
    meal_plan_id=None,
    basket_item_id=None,
    date,
    meal_time: str | None = None,
) -> MealChangeResult:
    """
    Déplace un repas planifié vers un autre jour / créneau. `basket_item_id` : seulement ce plat (envie),
    `meal_plan_id` : le repas entier (plats, convives, invitations). Le créneau standard déjà pris → « Autre ».
    """
    if basket_item_id is not None and meal_plan_id is None:
        return schedule_basket_item_for_user(user, basket_item_id=basket_item_id, date=date, meal_time=meal_time)
    if meal_plan_id is None:
        raise ValueError('Indique le repas (meal_plan_id) ou l’envie (basket_item_id).')

    from recipes.services.meal_plan_service import update_composer_slot

    meal = _own_meal(user, meal_plan_id)
    day = _future_day(user, date)
    slot = normalize_meal_time(meal_time) or meal.meal_time
    previous = (meal.date, meal.meal_time)
    try:
        with transaction.atomic():
            update_composer_slot(user, meal_plan_id=meal.id, date=day.isoformat(), meal_time=slot)
    except ValueError as exc:
        raise ValueError(f'Impossible de déplacer ce repas : {exc}') from exc
    meal.refresh_from_db()

    warnings = []
    guests = _invitee_names(meal)
    if guests:
        warnings.append(f'Invité·es à prévenir : {", ".join(guests)}.')
    title = _meal_title(meal)
    undo = UndoHint(
        method='PATCH',
        path=f'/api/meal-plans/{meal.id}/update-composer-slot/',
        body={'date': previous[0].isoformat(), 'meal_time': previous[1]},
    )
    message = f'« {title} » déplacé {_when(meal.date, meal.meal_time)}.'
    if warnings:
        message += ' ' + warnings[0]
    return MealChangeResult(
        card_type='meal_moved',
        meal_plan_id=meal.id,
        display_title=title,
        date=meal.date.isoformat(),
        meal_time=meal.meal_time,
        previous_date=previous[0].isoformat(),
        previous_meal_time=previous[1],
        warnings=warnings,
        undo=undo,
        message=message,
    )


def _reschedule_undo(item: BasketItem, day, meal_time: str) -> UndoHint:
    return UndoHint(
        method='POST',
        path=f'/api/basket/{item.id}/schedule/',
        body={'date': day.isoformat(), 'meal_time': meal_time},
    )


def _remove_dated_item(user: AbstractBaseUser, item: BasketItem) -> MealChangeResult:
    meal = item.meal_plan if item.meal_plan_id else None
    if meal is None:
        raise ValueError('Cette envie n’est pas datée : pour la retirer du panier, utilise remove_from_basket.')
    if meal.confirmed:
        raise ValueError('Ce plat est déjà cuisiné : pour annuler, utilise undo_log_cooked.')
    day, slot = meal.date, meal.meal_time
    try:
        basket.unschedule_item(item)
    except BasketError as exc:
        raise ValueError(exc.message) from exc
    still_there = MealPlan.objects.filter(id=meal.id).exists()
    undo = (
        UndoHint(method='POST', path=f'/api/basket/{item.id}/schedule/', body={'meal_plan_id': meal.id})
        if still_there
        else _reschedule_undo(item, day, slot)
    )
    return MealChangeResult(
        card_type='meal_removed',
        meal_plan_id=meal.id if still_there else None,
        basket_item_id=item.id,
        display_title=item.display_title,
        previous_date=day.isoformat(),
        previous_meal_time=slot,
        dishes=[
            MealChangeDish(
                basket_item_id=item.id, display_title=item.display_title, recipe_id=item.recipe_id, undo=undo
            )
        ],
        undo=undo,
        message=f'« {item.display_title} » retiré du {_when(day, slot)} : il reste dans ton panier de la semaine.',
    )


def remove_meal_for_user(user: AbstractBaseUser, *, meal_plan_id=None, basket_item_id=None) -> MealChangeResult:
    """
    Retire un repas du planning sans rien perdre : chaque plat redevient une envie sans date du panier
    (même semaine), le repas vide est supprimé. Annulation : re-dater chaque envie (`dishes[].undo`).
    `basket_item_id` : seulement ce plat. Repas avec invité·es, déjà cuisiné ou avec photos : refusé.
    """
    if basket_item_id is not None and meal_plan_id is None:
        return _remove_dated_item(user, _get_item(user, basket_item_id))
    if meal_plan_id is None:
        raise ValueError('Indique le repas (meal_plan_id) ou l’envie (basket_item_id).')

    meal = _own_meal(user, meal_plan_id)
    if meal.confirmed or Post.objects.filter(meal_plan=meal).exists():
        raise ValueError('Ce repas est déjà cuisiné : pour annuler, utilise undo_log_cooked.')
    if PostPhoto.objects.filter(meal_plan=meal).exists():
        raise ValueError('Ce repas a des photos : retire-le depuis l’app.')
    guests = _invitee_names(meal)
    if guests:
        raise ValueError(
            f'Des ami·es sont invité·es à ce repas ({", ".join(guests)}) : retire-le depuis l’app pour les prévenir.'
        )

    day, slot = meal.date, meal.meal_time
    previous_label = (meal.custom_label or '').strip()
    dishes: list[MealChangeDish] = []
    title = _meal_title(meal)
    with transaction.atomic():
        for item in basket.basket_queryset(user).filter(meal_plan=meal).order_by('id'):
            basket.unschedule_item(item)
            dishes.append(
                MealChangeDish(
                    basket_item_id=item.id,
                    display_title=item.display_title,
                    recipe_id=item.recipe_id,
                    undo=_reschedule_undo(item, day, slot),
                )
            )
        if MealPlan.objects.filter(id=meal.id).exists():
            # Plats ajoutés au repas hors panier : ils deviennent des envies (batch gardé = liste de courses).
            for mprb in (
                MealPlanRecipeBatch.objects.filter(meal_plan=meal)
                .select_related('recipe_batch')
                .order_by('order', 'id')
            ):
                batch = mprb.recipe_batch
                item = basket.add_item(
                    user,
                    recipe_id=batch.recipe_id,
                    servings=mprb.portions if mprb.is_portions_overridden else None,
                    week=day.isoformat(),
                )
                item.recipe_batch = batch
                item.save(update_fields=['recipe_batch', 'updated_at'])
                mprb.delete()
                dishes.append(
                    MealChangeDish(
                        basket_item_id=item.id,
                        display_title=item.display_title,
                        recipe_id=item.recipe_id,
                        undo=_reschedule_undo(item, day, slot),
                    )
                )
            meal.refresh_from_db()
            label = (meal.custom_label or '').strip()
            if label and label != 'Repas' and not dishes:
                item = basket.add_item(user, label=label, week=day.isoformat())
                dishes.append(
                    MealChangeDish(
                        basket_item_id=item.id, display_title=item.display_title, undo=_reschedule_undo(item, day, slot)
                    )
                )
            if not BasketItem.objects.filter(meal_plan=meal).exists():
                meal.delete()

    undo = dishes[0].undo if len(dishes) == 1 else None
    if not dishes:
        # Repas sans plat (cantine, à emporter, créneau vide) : on le recrée tel quel.
        undo = UndoHint(
            method='POST',
            path='/api/meal-plans/',
            body={
                'date': day.isoformat(),
                'meal_time': slot,
                'meal_type': meal.meal_type,
                'custom_label': previous_label,
                'guest_count': meal.guest_count,
            },
        )
        message = f'{_when(day, slot).capitalize()} retiré du planning.'
    else:
        names = ', '.join(d.display_title for d in dishes[:4])
        message = f'{_when(day, slot).capitalize()} retiré du planning ; {names} reste(nt) dans ton panier.'
    return MealChangeResult(
        card_type='meal_removed',
        meal_plan_id=None,
        display_title=title,
        previous_date=day.isoformat(),
        previous_meal_time=slot,
        dishes=dishes,
        undo=undo,
        message=message,
    )


# ---------------------------------------------------------------------------
# Invitations : ami·es seulement
# ---------------------------------------------------------------------------
def resolve_friends(user: AbstractBaseUser, *, ids=None, names=None) -> tuple[list[AbstractBaseUser], list[str]]:
    """
    Ami·es (suivi dans un sens ou l'autre, sans blocage) par id ou par nom (username ou prénom).
    Renvoie (ami·es trouvé·es, noms / ids sans correspondance).
    """
    from accounts.blocks import blocked_user_ids_for
    from recipes.services.invitation_service import _complice_ids_for_user

    friend_ids = _complice_ids_for_user(user) - blocked_user_ids_for(user) - {user.id}
    friends = User.objects.filter(id__in=friend_ids, is_active=True)
    found: dict[int, AbstractBaseUser] = {}
    missing: list[str] = []
    for raw in ids or []:
        try:
            uid = int(raw)
        except (TypeError, ValueError):
            missing.append(str(raw))
            continue
        friend = friends.filter(id=uid).first()
        if friend:
            found.setdefault(friend.id, friend)
        else:
            missing.append(str(raw))
    for raw in names or []:
        name = str(raw or '').strip().lstrip('@')
        if not name:
            continue
        exact = list(friends.filter(Q(username__iexact=name) | Q(first_name__iexact=name))[:2])
        matches = exact or list(friends.filter(Q(username__icontains=name) | Q(first_name__icontains=name))[:2])
        if len(matches) == 1:
            found.setdefault(matches[0].id, matches[0])
        else:
            missing.append(name)  # inconnu·e ou ambigu·ë : on n'invite personne au hasard
    return list(found.values()), missing


def _resolve_meal_for_invitation(user: AbstractBaseUser, *, meal_plan_id, basket_item_id, date, meal_time) -> MealPlan:
    if meal_plan_id is not None:
        return _own_meal(user, meal_plan_id)
    if basket_item_id is not None:
        item = _get_item(user, basket_item_id)
        if not item.meal_plan_id:
            raise ValueError('Cette envie n’est pas encore datée : date-la d’abord (schedule_basket_item).')
        return _own_meal(user, item.meal_plan_id)
    if not date:
        raise ValueError('Indique le repas : meal_plan_id, basket_item_id ou date (+ meal_time).')
    day = parse_day(date, basket.user_today(user))
    meals = MealPlan.objects.filter(user=user, date=day, confirmed=False)
    slot = normalize_meal_time(meal_time)
    if slot:
        meals = meals.filter(meal_time=slot)
    meals = list(meals.order_by('meal_time', 'id')[:3])
    if not meals:
        raise ValueError(f'Aucun repas prévu {_when(day, slot)} : planifie-le d’abord (plan_week avec une date).')
    if len(meals) > 1:
        raise ValueError(f'Plusieurs repas {_when(day, None)} : précise meal_time ou meal_plan_id.')
    return meals[0]


def propose_invite_to_meal(
    user: AbstractBaseUser,
    *,
    meal_plan_id=None,
    basket_item_id=None,
    date=None,
    meal_time=None,
    invitee_ids=None,
    invitee_names=None,
) -> MutationProposal:
    """Carte de confirmation `meal_invitation` (rien n'est envoyé avant le « Envoyer » de l'utilisateur)."""
    from recipes.services.invitation_service import propose_meal_invitation

    meal = _resolve_meal_for_invitation(
        user, meal_plan_id=meal_plan_id, basket_item_id=basket_item_id, date=date, meal_time=meal_time
    )
    friends, missing = resolve_friends(user, ids=invitee_ids, names=invitee_names)
    if not friends:
        who = ', '.join(missing) or 'personne'
        raise ValueError(f'Aucun·e ami·e trouvé·e ({who}) : on ne peut inviter que ses ami·es.')
    try:
        proposal = propose_meal_invitation(user, [meal.id], [f.id for f in friends])
    except PermissionError as exc:
        raise ValueError(str(exc)) from exc
    if missing:
        proposal.warnings.append(f'Pas trouvé parmi tes ami·es : {", ".join(missing)}.')
    return proposal


def invite_to_table_for_user(
    user: AbstractBaseUser,
    *,
    table_id=None,
    table_name: str | None = None,
    invitee_ids=None,
    invitee_names=None,
) -> TableInvitationResult:
    """Invite des ami·es à une tablée dont l'utilisateur est membre (consentement : ils acceptent dans l'app)."""
    from tables import services as tables
    from tables.models import TableMember

    memberships = TableMember.objects.select_related('table').filter(user=user, status=TableMember.STATUS_ACCEPTED)
    if table_id is not None:
        m = memberships.filter(table_id=table_id).first()
        if m is None:
            raise ValueError('Tablée introuvable.')
    elif table_name:
        name = table_name.strip()
        candidates = list(memberships.filter(table__name__iexact=name)[:2]) or list(
            memberships.filter(table__name__icontains=name)[:2]
        )
        if len(candidates) != 1:
            raise ValueError(f'Tablée « {name} » introuvable ou ambiguë : précise table_id.')
        m = candidates[0]
    else:
        all_m = list(memberships[:2])
        if len(all_m) != 1:
            raise ValueError('Précise la tablée (table_id ou table_name).' if all_m else 'Tu n’as pas de tablée.')
        m = all_m[0]
    table = m.table

    friends, missing = resolve_friends(user, ids=invitee_ids, names=invitee_names)
    if not friends:
        who = ', '.join(missing) or 'personne'
        raise ValueError(f'Aucun·e ami·e trouvé·e ({who}) : on ne peut inviter que ses ami·es.')
    try:
        results = tables.invite(user, table.id, [f.id for f in friends])
    except tables.TableError as exc:
        raise ValueError(_TABLE_ERRORS_FR.get(exc.code, exc.message)) from exc

    is_owner = m.role == TableMember.ROLE_OWNER
    invitees = []
    for friend in friends:
        status = results.get(friend.id, 'invited')
        undo = None
        if status == 'invited' and is_owner:
            undo = UndoHint(
                method='DELETE', path=f'/api/tables/{table.id}/members/{friend.id}/', label='Annuler l’invitation'
            )
        invitees.append(TableInvitee(user_id=friend.id, username=friend.username, status=status, undo=undo))

    invited = [i.username for i in invitees if i.status == 'invited']
    already = [i.username for i in invitees if i.status != 'invited']
    parts = []
    if invited:
        parts.append(f'Invitation à « {table.name} » envoyée à {", ".join(invited)} (accepte dans l’app).')
    if already:
        parts.append(f'Déjà à la tablée ou invité·e : {", ".join(already)}.')
    if missing:
        parts.append(f'Pas trouvé parmi tes ami·es : {", ".join(missing)}.')
    undos = [i.undo for i in invitees if i.undo]
    return TableInvitationResult(
        table_id=table.id,
        table_name=table.name,
        table_emoji=table.emoji or '',
        invitees=invitees,
        not_found=missing,
        actions=[CardAction(kind='open_table', label='Voir la tablée', table_id=table.id)],
        undo=undos[0] if len(undos) == 1 else None,
        message=' '.join(parts),
    )


_TABLE_ERRORS_FR = {
    'not_friends': 'On ne peut inviter que ses ami·es à une tablée.',
    'blocked': 'Action impossible.',
    'table_full': 'La tablée est complète.',
    'user_not_found': 'Utilisateur introuvable.',
    'self': 'Tu es déjà à cette tablée.',
    'table_not_found': 'Tablée introuvable.',
    'user_ids_required': 'Indique au moins un·e ami·e.',
}
