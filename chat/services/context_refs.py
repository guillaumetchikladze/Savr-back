"""Formatage et résolution du contexte attaché (@ recette, repas, liste)."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AbstractBaseUser
from django.db.models import Prefetch, Q

from recipes.models import (
    MealPlan,
    MealPlanRecipeBatch,
    Recipe,
    RecipeIngredient,
    ShoppingList,
    ShoppingListMember,
    Step,
)
from recipes.services.invitation_service import _complice_ids_for_user
from recipes.services.shopping_list_service import get_shopping_list_items_for_user
from recipes.utils import get_accessible_meal_plan_filter

User = get_user_model()

TYPE_LABELS = {
    'recipe': 'Recette',
    'meal_plan': 'Repas planifié',
    'shopping_list': 'Liste de courses',
    'friend': 'Ami',
}

META_LABELS = {
    'view_mode': 'Vue',
    'view_mode_label': 'Contexte écran',
    'batch_id': 'Batch recette',
    'is_cooked': 'Déjà cuisiné',
    'is_guest': 'Invité',
    'invitation_status': 'Statut invitation',
    'item_count': 'Lignes liste',
    'to_buy_count': 'Articles à acheter',
    'current_step_index': 'Index étape (0-based)',
    'current_step_number': 'Étape n°',
    'step_id': 'ID étape',
    'step_title': 'Titre étape',
}

_DIFFICULTY_LABELS = {
    'easy': 'Facile',
    'medium': 'Moyen',
    'hard': 'Difficile',
}

_MEAL_TIME_LABELS = {
    'breakfast': 'Petit-déjeuner',
    'lunch': 'Déjeuner',
    'dinner': 'Dîner',
    'other': 'Autre',
}


def _format_meta(meta: dict) -> list[str]:
    lines = []
    for key, value in meta.items():
        if value is None or value == '':
            continue
        label = META_LABELS.get(key, key)
        if isinstance(value, bool):
            value = 'oui' if value else 'non'
        lines.append(f'  · {label} : {value}')
    return lines


def _truncate(text: str | None, limit: int = 600) -> str:
    raw = (text or '').strip()
    if len(raw) <= limit:
        return raw
    return raw[: limit - 1].rstrip() + '…'


def _recipe_queryset_for_user(user: AbstractBaseUser):
    return Recipe.objects.filter(Q(is_public=True) | Q(created_by=user))


def _resolve_recipe_details(user: AbstractBaseUser, ref: dict) -> list[str]:
    ref_id = ref.get('id')
    if ref_id is None:
        return []
    recipe = (
        _recipe_queryset_for_user(user)
        .filter(pk=ref_id)
        .only(
            'id',
            'title',
            'description',
            'steps_summary',
            'prep_time',
            'cook_time',
            'difficulty',
            'servings',
        )
        .first()
    )
    if not recipe:
        return ['  (recette introuvable ou accès refusé)']

    lines = [
        f'  Titre : {recipe.title}',
        f'  Durée : {recipe.prep_time + recipe.cook_time} min '
        f'(prépa {recipe.prep_time}, cuisson {recipe.cook_time})',
        f'  Difficulté : {_DIFFICULTY_LABELS.get(recipe.difficulty, recipe.difficulty)}',
        f'  Portions : {recipe.servings}',
    ]
    if recipe.description:
        lines.append(f'  Description : {_truncate(recipe.description, 400)}')
    if recipe.steps_summary:
        lines.append(f'  Résumé des étapes : {_truncate(recipe.steps_summary, 500)}')

    ingredients = list(
        RecipeIngredient.objects.filter(recipe_id=recipe.id)
        .select_related('ingredient')
        .order_by('id')[:20]
    )
    if ingredients:
        ing_lines = []
        for ri in ingredients:
            name = ri.ingredient.name if ri.ingredient_id else '?'
            unit = ri.get_unit_display() if hasattr(ri, 'get_unit_display') else ri.unit
            qty = format(ri.quantity, 'g').rstrip('0').rstrip('.') if ri.quantity is not None else ''
            ing_lines.append(f'{qty} {unit} {name}'.strip())
        lines.append('  Ingrédients : ' + '; '.join(ing_lines))

    meta = ref.get('meta') if isinstance(ref.get('meta'), dict) else {}
    step_id = meta.get('step_id')
    if step_id is not None:
        step = Step.objects.filter(recipe_id=recipe.id, pk=step_id).first()
        if step:
            title = (step.title or meta.get('step_title') or f'Étape {step.order}').strip()
            lines.append(f'  Étape ouverte ({title}) : {_truncate(step.instruction, 500)}')
            if step.tip:
                lines.append(f'  Astuce étape : {_truncate(step.tip, 200)}')
    elif meta.get('current_step_number'):
        step_num = meta.get('current_step_number')
        step = Step.objects.filter(recipe_id=recipe.id, order=step_num).first()
        if step:
            title = (step.title or meta.get('step_title') or f'Étape {step_num}').strip()
            lines.append(f'  Étape ouverte ({title}) : {_truncate(step.instruction, 500)}')

    return lines


def _resolve_meal_plan_details(user: AbstractBaseUser, ref: dict) -> list[str]:
    ref_id = ref.get('id')
    if ref_id is None:
        return []
    mp = (
        MealPlan.objects.filter(get_accessible_meal_plan_filter(user))
        .filter(pk=ref_id)
        .prefetch_related(
            Prefetch(
                'meal_plan_recipe_batches',
                queryset=MealPlanRecipeBatch.objects.select_related(
                    'recipe_batch__recipe'
                ).order_by('order'),
            )
        )
        .first()
    )
    if not mp:
        return ['  (repas introuvable ou accès refusé)']

    meal_label = _MEAL_TIME_LABELS.get(mp.meal_time, mp.meal_time)
    lines = [
        f'  Date : {mp.date.isoformat()}',
        f'  Créneau : {meal_label}',
        f'  Invités (hors comptes) : {mp.guest_count}',
        f'  Confirmé : {"oui" if mp.confirmed else "non"}',
    ]
    recipes = []
    for mprb in mp.meal_plan_recipe_batches.all():
        batch = mprb.recipe_batch
        if batch and batch.recipe_id:
            recipes.append(batch.recipe.title)
    if recipes:
        lines.append('  Recettes du repas : ' + ', '.join(recipes))
    return lines


def _resolve_friend_details(user: AbstractBaseUser, ref: dict) -> list[str]:
    ref_id = ref.get('id')
    if ref_id is None:
        return []
    friend_ids = _complice_ids_for_user(user)
    if int(ref_id) not in friend_ids:
        return ['  (ami introuvable ou hors de votre réseau)']
    friend = User.objects.filter(pk=ref_id).only('id', 'username').first()
    if not friend:
        return ['  (ami introuvable)']
    return [
        f'  Username : {friend.username}',
        f'  ID utilisateur : {friend.id} (utilise cet id dans invitee_ids)',
    ]


def _resolve_shopping_list_details(user: AbstractBaseUser, ref: dict) -> list[str]:
    ref_id = ref.get('id')
    if ref_id is None:
        return []
    is_member = ShoppingListMember.objects.filter(
        shopping_list_id=ref_id,
        user=user,
    ).exists()
    if not is_member:
        return ['  (liste introuvable ou accès refusé)']

    shopping_list = ShoppingList.objects.filter(pk=ref_id).first()
    if not shopping_list:
        return ['  (liste introuvable ou accès refusé)']

    lines = [f'  Nom : {shopping_list.name or "Liste de courses"}']
    snapshot = get_shopping_list_items_for_user(user, ref_id, include_purchased=False)
    if snapshot.items:
        lines.append(f'  À acheter ({snapshot.count}) — aperçu (peut être périmé) :')
        for item in snapshot.items[:15]:
            qty = item.remaining_quantity
            unit = item.unit or 'piece'
            lines.append(f'    · {item.ingredient_name} : {qty} {unit}')
        if snapshot.count > 15:
            lines.append(f'    … et {snapshot.count - 15} autre(s)')
        lines.append(
            '  Important : pour la liste à jour, appelle get_shopping_list_items (ne te fie pas à cet aperçu seul).'
        )
    else:
        lines.append('  À acheter : (rien — liste vide ou tout est déjà acheté)')
    return lines


def format_context_refs_prompt(context_refs: list[dict] | None) -> str:
    if not context_refs:
        return ''
    lines = ['[Contexte utilisateur attaché]']
    for ref in context_refs:
        if not isinstance(ref, dict):
            continue
        kind = ref.get('type') or ''
        if kind == 'complice':
            kind = 'friend'
        label = (ref.get('label') or '').strip()
        ref_id = ref.get('id')
        type_label = TYPE_LABELS.get(kind, kind)
        if ref_id is not None:
            lines.append(f'- {type_label} « {label} » (id={ref_id})')
        else:
            lines.append(f'- {type_label} « {label} »')
        meta = ref.get('meta')
        if isinstance(meta, dict):
            lines.extend(_format_meta(meta))
    lines.append(
        "Utilise ces éléments comme point de départ : l'utilisateur parle probablement de ce contenu."
    )
    return '\n'.join(lines)


def build_context_prompt_for_agent(
    user: AbstractBaseUser,
    context_refs: list[dict] | None,
) -> str:
    """
    Bloc système injecté avant le message utilisateur : métadonnées + contenu résolu depuis la BDD.
    """
    if not context_refs:
        return ''

    header = format_context_refs_prompt(context_refs)
    if not header:
        return ''

    detail_sections: list[str] = []
    for ref in context_refs:
        if not isinstance(ref, dict):
            continue
        kind = ref.get('type')
        if kind == 'complice':
            kind = 'friend'
        label = (ref.get('label') or TYPE_LABELS.get(kind, kind)).strip()
        ref_id = ref.get('id')
        if ref_id is None:
            continue

        if kind == 'recipe':
            details = _resolve_recipe_details(user, ref)
        elif kind == 'meal_plan':
            details = _resolve_meal_plan_details(user, ref)
        elif kind == 'shopping_list':
            details = _resolve_shopping_list_details(user, ref)
        elif kind == 'friend':
            details = _resolve_friend_details(user, ref)
        else:
            continue

        if details:
            detail_sections.append(f'Détails — {label} (id={ref_id}) :\n' + '\n'.join(details))

    friend_ids: list[int] = []
    for ref in context_refs:
        if not isinstance(ref, dict):
            continue
        kind = ref.get('type')
        if kind == 'complice':
            kind = 'friend'
        if kind != 'friend' or ref.get('id') is None:
            continue
        try:
            friend_ids.append(int(ref['id']))
        except (TypeError, ValueError):
            continue

    friend_hint = ''
    if friend_ids:
        ids_literal = ', '.join(str(uid) for uid in friend_ids)
        friend_hint = (
            '\n\n[Instruction ami @]\n'
            f'Ami(s) attaché(s) via @ (ids={ids_literal}). '
            "Si l'utilisateur demande d'inviter : invitee_ids dans send_invitation_proposal. "
            "Si l'utilisateur demande qui est invité / qui vient : get_meal_plans + champ invitees — "
            "pas send_invitation_proposal."
        )

    if detail_sections:
        return (
            header
            + '\n\n[Contenu résolu depuis Tchikook Agent]\n'
            + '\n\n'.join(detail_sections)
            + friend_hint
            + '\n\nRéponds en tenant compte de ce contexte sans redemander ces informations de base.'
        )
    if friend_hint:
        return header + friend_hint
    return header
