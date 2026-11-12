"""
« Refaire sa version » (ckd2 lot 3b, fx07) : depuis un post « D'après Léa · Sa version : sans crème… »,
le viewer obtient **sa propre recette** copiée de la recette d'origine, avec les changements de l'auteur
du post appliqués par l'IA, à la demande (au tap, jamais à la publication : pas de coût si personne n'en veut).

Mécanisme d'eq01 (`equipment_adaptation.py`) réutilisé : copie liée (`adapted_from`), publique (prv1),
`created_by` = le viewer, idempotente par (recette d'origine, viewer, `adaptation_signature`) — contrainte
unique `recipe_unique_equipment_adaptation`. Ici `adaptation_kind = post_version` et la signature est
l'empreinte du texte « Ce que tu as changé » (préfixée `pv`, jamais confondue avec celle d'une cuisine) :
retaper renvoie la même copie, sans IA ni plafond.

- **Qui** : quiconque voit le post (`get_visible_post_or_404`, son propre post « rien que pour toi » compris) ;
  sinon 404. Le plat doit avoir des changements (`MealPlanRecipeBatch.changes`), sinon 400 `no_changes`.
- **IA** : `revise_recipe(current_recipe, instruction)`, borné par `POST_VERSION_LLM_TIMEOUT_SECONDS` (20 s).
  Compte dans le plafond mensuel (`ai_recipe_quota.has_quota`, premium exempté) via une `RecipeImportRequest`
  `job_type = post_version` marquée `quota`.
- **Repli** (plafond atteint, IA en erreur ou délai, réponse vide) : copie simple de la recette d'origine ;
  les changements restent en note en tête de la description. Toujours utile, jamais d'erreur.
- La note « D'après la version de Tom : … » est en tête de la description dans les deux cas.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import Http404

from recipes.models import (
    MealPlanRecipeBatch,
    Post,
    Recipe,
    RecipeImportRequest,
    RecipeIngredient,
    Step,
    StepIngredient,
)

logger = logging.getLogger(__name__)

JOB_TYPE = 'post_version'
SIGNATURE_PREFIX = 'pv'


class PostVersionError(Exception):
    def __init__(self, message: str, status_code: int = 400, code: str = 'invalid'):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


def normalized_changes(text: str) -> str:
    return ' '.join(str(text or '').split())


def changes_signature(text: str) -> str:
    """Empreinte du texte des changements (32 car., comme la signature d'une cuisine)."""
    digest = hashlib.sha256(normalized_changes(text).casefold().encode('utf-8')).hexdigest()
    return f'{SIGNATURE_PREFIX}{digest[:30]}'


def existing_version(recipe_id: int, user, changes: str) -> Recipe | None:
    return Recipe.objects.filter(
        adapted_from_id=recipe_id,
        created_by=user,
        adaptation_kind=Recipe.ADAPTATION_POST_VERSION,
        adaptation_signature=changes_signature(changes),
    ).first()


def _first_name(user) -> str:
    return (getattr(user, 'first_name', '') or '').strip() or getattr(user, 'username', '') or ''


def version_note(post: Post, viewer, changes: str) -> str:
    phrase = normalized_changes(changes)
    if post.user_id == viewer.id:
        return f'Ta version : {phrase}'
    name = _first_name(post.user)
    return f'D’après la version de {name} : {phrase}' if name else f'Sa version : {phrase}'


def _instruction(changes: str) -> str:
    return (
        'Un ami a refait cette recette avec ces changements : '
        f'« {normalized_changes(changes)} ». '
        'Applique seulement ces changements (ingrédients, quantités, étapes, temps) et garde tout le reste. '
        'Tutoiement, phrases courtes. Ne parle jamais de calories.'
    )


def _find_dish(post: Post, recipe_batch_id) -> MealPlanRecipeBatch | None:
    if not post.meal_plan_id:
        return None
    rows = (
        MealPlanRecipeBatch.objects.filter(meal_plan_id=post.meal_plan_id)
        .exclude(changes='')
        .select_related('recipe_batch__recipe')
        .order_by('order', 'id')
    )
    if recipe_batch_id not in (None, ''):
        try:
            recipe_batch_id = int(recipe_batch_id)
        except (TypeError, ValueError) as exc:
            raise PostVersionError('Plat introuvable.', 400, 'invalid_dish') from exc
        rows = rows.filter(recipe_batch_id=recipe_batch_id)
    for row in rows:
        if row.changes.strip() and row.recipe_batch_id and row.recipe_batch.recipe_id:
            return row
    return None


def _revise_with_ai(original: Recipe, changes: str):
    """Recette révisée (`RecipeFormalized`) ou lève : délai, IA absente, réponse inutilisable. Point de mock."""
    from asgiref.sync import async_to_sync

    from recipes.services.ai_service import revise_recipe
    from recipes.services.recipe_revision_service import build_recipe_state_dict

    timeout = float(getattr(settings, 'POST_VERSION_LLM_TIMEOUT_SECONDS', 20))

    async def _run():
        return await asyncio.wait_for(revise_recipe(build_recipe_state_dict(original), _instruction(changes)), timeout)

    return async_to_sync(_run)()


def _copy(original: Recipe, user, *, note: str, signature: str) -> Recipe:
    description = (original.description or '').strip()
    description = f'{note}\n\n{description}' if description else note
    copy = Recipe.objects.create(
        title=original.title,
        description=description,
        steps_summary=original.steps_summary,
        meal_type=original.meal_type,
        difficulty=original.difficulty,
        prep_time=original.prep_time,
        cook_time=original.cook_time,
        servings=original.servings,
        image_path=original.image_path,
        cuisine=original.cuisine,
        created_by=user,
        is_public=True,  # prv1 : toutes les recettes sont publiques par défaut
        source_type='user_created',
        # Même plat : l'index de recherche de l'original reste valable jusqu'à la réindexation (IA appliquée).
        embedding=original.embedding,
        search_context_tags=original.search_context_tags,
        search_index_text=original.search_index_text,
        search_index_hash=original.search_index_hash,
        search_indexed_at=original.search_indexed_at,
        search_index_status=original.search_index_status,
        required_equipment_override=original.required_equipment_override,
        adapted_from=original,
        adaptation_kind=Recipe.ADAPTATION_POST_VERSION,
        adaptation_signature=signature,
    )
    for line in original.recipe_ingredients.order_by('id'):
        RecipeIngredient.objects.create(
            recipe=copy,
            ingredient_id=line.ingredient_id,
            quantity=line.quantity,
            unit=line.unit,
            sub_recipe_id=line.sub_recipe_id,
            default_mode=line.default_mode,
            sub_recipe_ratio=line.sub_recipe_ratio,
        )
    for step in original.steps.order_by('order').prefetch_related('step_ingredients'):
        new_step = Step.objects.create(
            recipe=copy,
            order=step.order,
            title=step.title,
            instruction=step.instruction,
            tip=step.tip,
            has_timer=step.has_timer,
            timer_duration=step.timer_duration,
        )
        StepIngredient.objects.bulk_create(
            [
                StepIngredient(step=new_step, ingredient_id=si.ingredient_id, quantity=si.quantity, unit=si.unit)
                for si in step.step_ingredients.all()
            ]
        )
    return copy


def _apply_revision(copy: Recipe, revised, note: str) -> None:
    from recipes.services.ai_service import formalized_to_response_dict
    from recipes.services.recipe_revision_service import _apply_edit_payload, _formalized_to_edit_payload

    payload = _formalized_to_edit_payload(formalized_to_response_dict(revised))
    if not payload.get('steps') or not payload.get('ingredients'):
        raise ValueError('révision vide')
    description = (payload.get('description') or '').strip()
    payload['description'] = f'{note}\n\n{description}' if description else note
    payload['title'] = (payload.get('title') or copy.title).strip()[:200] or copy.title
    _apply_edit_payload(copy, payload)


def redo_post_version(user, post_id, recipe_batch_id=None) -> tuple[Recipe, bool, bool | None]:
    """(ma version, créée maintenant ?, IA appliquée ? — None si elle existait déjà). Lève `PostVersionError`."""
    from recipes.services import ai_recipe_quota
    from recipes.services.equipment_service import deferred_equipment_refresh
    from recipes.services.post_visibility import get_visible_post_or_404
    from recipes.services.recipe_search_index import schedule_recipe_search_reindex

    try:
        post = get_visible_post_or_404(user, post_id, queryset=Post.objects.select_related('user'))
    except (Http404, ValueError, TypeError) as exc:
        raise PostVersionError('Ce plat n’est plus visible.', 404, 'not_found') from exc
    dish = _find_dish(post, recipe_batch_id)
    if dish is None:
        raise PostVersionError('Rien à refaire : ce plat n’a pas de version.', 400, 'no_changes')
    original = dish.recipe_batch.recipe
    changes = dish.changes
    existing = existing_version(original.id, user, changes)
    if existing is not None:
        return existing, False, None

    note = version_note(post, user, changes)
    signature = changes_signature(changes)
    revised = None
    job = None
    if ai_recipe_quota.has_quota(user):
        job = RecipeImportRequest.objects.create(
            user=user,
            payload={
                'job_type': JOB_TYPE,
                'quota': ai_recipe_quota.QUOTA_KEY,
                'post_id': post.id,
                'recipe_id': original.id,
                'title': original.title,
            },
            status=RecipeImportRequest.STATUS_PROCESSING,
        )
        try:
            revised = _revise_with_ai(original, changes)
        except Exception as exc:  # délai, IA absente, JSON : copie simple, changements en note
            logger.warning('post version AI failed for recipe %s: %s', original.id, exc)
            job.status = RecipeImportRequest.STATUS_ERROR
            job.error_message = str(exc)[:500] or exc.__class__.__name__
            job.save(update_fields=['status', 'error_message', 'updated_at'])

    ai_applied = False
    try:
        with transaction.atomic(), deferred_equipment_refresh():
            copy = _copy(original, user, note=note, signature=signature)
            if revised is not None:
                try:
                    with transaction.atomic():
                        _apply_revision(copy, revised, note)
                    ai_applied = True
                except Exception as exc:  # révision inutilisable : la copie simple reste
                    logger.warning('post version revision unusable for recipe %s: %s', original.id, exc)
                    copy.refresh_from_db()
    except IntegrityError:  # doublon concurrent : l'autre requête a gagné
        existing = existing_version(original.id, user, changes)
        if existing is None:
            raise
        if job is not None and job.status == RecipeImportRequest.STATUS_PROCESSING:
            job.status = RecipeImportRequest.STATUS_SUCCESS
            job.recipe = existing
            job.save(update_fields=['status', 'recipe', 'updated_at'])
        return existing, False, None

    if job is not None and revised is not None:
        job.status = RecipeImportRequest.STATUS_SUCCESS if ai_applied else RecipeImportRequest.STATUS_ERROR
        job.recipe = copy
        if not ai_applied:
            job.error_message = 'revision_unusable'
        job.save(update_fields=['status', 'recipe', 'error_message', 'updated_at'])
    if ai_applied:
        schedule_recipe_search_reindex(copy.id)
    return copy, True, ai_applied


def post_version_payload(copy: Recipe, created: bool, ai_applied: bool | None, context: dict | None = None) -> dict:
    from recipes.serializers import RecipeDetailSerializer

    return {
        'created': created,
        'ai_applied': ai_applied,
        'original_recipe_id': copy.adapted_from_id,
        'recipe': RecipeDetailSerializer(copy, context=context or {}).data,
    }
