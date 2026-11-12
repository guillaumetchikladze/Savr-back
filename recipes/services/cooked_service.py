"""
« J'ai cuisiné » : log canonique d'un plat cuisiné (fondation du feed, des stats et du Récap).

Définition unique de « cuisiné » (stats, badges, Récap) — voir `cooked_meal_plans()` :
    un MealPlan **possédé** par l'utilisateur avec `confirmed=True`.
    - la date du « cuisiné » est `MealPlan.date` ;
    - le contenu est `MealPlan.meal_plan_recipe_batches` (recettes) et/ou `custom_label`
      / `Post.label` (plat libre) ;
    - le Post (publié ou non) est optionnel : on peut cuisiner sans publier.
    Les invités d'un repas ne « cuisinent » pas le repas de l'hôte (ils loguent le leur).

Tous les chemins qui signifient « j'ai cuisiné » posent `confirmed=True` :
    - POST /posts/cooked/ (ce module) ;
    - POST /meal-plans/{id}/publish-post/ ;
    - POST /recipe-batches/{id}/publish-post/ (legacy) et POST /posts/{id}/publish/ ;
    - fin du mode cuisine : POST /recipe-batches/{id}/complete_cooking/ et
      POST /cooking-progress/{id}/complete/ (repas choisi par `pick_meal_plan_for_batch`) ;
    - POST /meal-plans/{id}/confirm/.

Panier « Cette semaine » (`basket_service`) : une envie n'est pas un repas. `POST /posts/cooked/` avec
`basket_item_id` (ou un plat qui correspond à une envie non datée de la semaine) passe par ce même
flux ; l'envie est ensuite rattachée au repas confirmé et le post porte `inspired_by_post`
(« À mon tour »).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, timedelta

from django.contrib.auth.models import AbstractBaseUser
from django.db import transaction
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from recipes.models import (
    CookingProgress,
    MealPlan,
    MealPlanRecipeBatch,
    Post,
    PostPhoto,
    Recipe,
    RecipeBatch,
)
from recipes.services.meal_plan_service import (
    add_recipes_to_meal_plan,
    complete_recipe_batch_workflow,
    infer_meal_time_from_hour,
)
from recipes.utils import get_accessible_meal_plan_filter

MAX_POST_PHOTOS = 20
LABEL_MAX_LENGTH = 120
_STANDARD_SLOTS = ('breakfast', 'lunch', 'dinner')
_ALLOWED_MEAL_TIMES = (*_STANDARD_SLOTS, 'other')


# ---------------------------------------------------------------------------
# Définition « cuisiné » (stats)
# ---------------------------------------------------------------------------
def cooked_meal_plans(
    user: AbstractBaseUser,
    start: date_type | None = None,
    end: date_type | None = None,
) -> QuerySet[MealPlan]:
    """Repas cuisinés par `user` (source unique pour stats / badges / Récap). Bornes incluses."""
    return _cooked_filter(MealPlan.objects.filter(user=user), start, end)


def cooked_meal_plans_of_users(
    user_ids,
    start: date_type | None = None,
    end: date_type | None = None,
) -> QuerySet[MealPlan]:
    """Même définition que `cooked_meal_plans`, pour plusieurs utilisateurs en une requête (feed)."""
    return _cooked_filter(MealPlan.objects.filter(user_id__in=list(user_ids)), start, end)


def _cooked_filter(qs: QuerySet[MealPlan], start, end) -> QuerySet[MealPlan]:
    qs = qs.filter(confirmed=True)
    if start:
        qs = qs.filter(date__gte=start)
    if end:
        qs = qs.filter(date__lte=end)
    return qs


def pick_meal_plan_for_batch(user_id: int, batch_id: int) -> MealPlan | None:
    """
    Repas « propriétaire » de l'utilisateur auquel rattacher une cuisson de batch :
    le plus proche à partir d'aujourd'hui, sinon le plus récent passé.
    """
    base = MealPlan.objects.filter(user_id=user_id, meal_plan_recipe_batches__recipe_batch_id=batch_id).distinct()
    today = timezone.localdate()
    return base.filter(date__gte=today).order_by('date', 'meal_time').first() or base.order_by('-date', '-id').first()


def confirm_meal_plan_for_cooked_batch(user: AbstractBaseUser, batch: RecipeBatch) -> MealPlan | None:
    """Fin du mode cuisine / publication legacy d'un batch → le repas choisi devient « cuisiné »."""
    meal_plan = pick_meal_plan_for_batch(user.id, batch.id)
    if meal_plan and not meal_plan.confirmed:
        MealPlan.objects.filter(pk=meal_plan.pk).update(confirmed=True, updated_at=timezone.now())
        meal_plan.confirmed = True
    return meal_plan


def unconfirm_meal_plans_for_uncooked_batch(user: AbstractBaseUser, batch: RecipeBatch) -> int:
    """
    Inverse de `confirm_meal_plan_for_cooked_batch` (« Annuler la cuisson ») : les repas de
    l'utilisateur contenant ce batch redeviennent « non cuisinés » s'ils n'ont ni post ni
    autre batch cuisiné.
    """
    other_cooked = MealPlanRecipeBatch.objects.filter(
        meal_plan_id=OuterRef('pk'), recipe_batch__is_cooked=True
    ).exclude(recipe_batch_id=batch.id)
    qs = (
        MealPlan.objects.filter(user=user, confirmed=True, meal_plan_recipe_batches__recipe_batch_id=batch.id)
        .annotate(_has_post=Exists(Post.objects.filter(meal_plan_id=OuterRef('pk'))))
        .annotate(_other_cooked=Exists(other_cooked))
        .filter(_has_post=False, _other_cooked=False)
    )
    ids = list(qs.values_list('id', flat=True))
    if ids:
        MealPlan.objects.filter(id__in=ids).update(confirmed=False, updated_at=timezone.now())
    return len(ids)


# ---------------------------------------------------------------------------
# Titre affichable « X a cuisiné {titre} »
# ---------------------------------------------------------------------------
def join_titles_fr(titles: list[str]) -> str:
    titles = [t for t in titles if t]
    if len(titles) <= 1:
        return titles[0] if titles else ''
    return f'{", ".join(titles[:-1])} et {titles[-1]}'


def meal_plan_recipe_titles(meal_plan: MealPlan | None) -> list[str]:
    if not meal_plan:
        return []
    titles = []
    for mprb in meal_plan.meal_plan_recipe_batches.all():
        recipe = getattr(getattr(mprb, 'recipe_batch', None), 'recipe', None)
        if recipe and recipe.title and recipe.title not in titles:
            titles.append(recipe.title)
    return titles


def meal_plan_display_title(meal_plan: MealPlan | None) -> str | None:
    if not meal_plan:
        return None
    titles = meal_plan_recipe_titles(meal_plan)
    if titles:
        return join_titles_fr(titles)
    return (meal_plan.custom_label or '').strip() or None


def post_display_title(post: Post) -> str | None:
    """Libellé du plat : label libre > recettes du repas > recette du batch > libellé du repas."""
    label = (post.label or '').strip()
    if label:
        return label
    meal_plan = post.meal_plan if post.meal_plan_id else None
    titles = meal_plan_recipe_titles(meal_plan)
    if not titles and post.recipe_batch_id and post.recipe_batch and post.recipe_batch.recipe:
        titles = [post.recipe_batch.recipe.title]
    if titles:
        return join_titles_fr(titles)
    if meal_plan:
        return (meal_plan.custom_label or '').strip() or None
    return None


# ---------------------------------------------------------------------------
# POST /posts/cooked/
# ---------------------------------------------------------------------------
class CookedError(Exception):
    def __init__(self, message: str, status_code: int = 400, code: str = 'invalid'):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


@dataclass
class CookedResult:
    post: Post
    created: bool
    # Mise en forme / import de la recette en arrière-plan (ckd2 lot 2) : {request_id, kind, status[, code]}.
    recipe_job: dict | None = None


def _parse_int(value, field: str) -> int | None:
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        raise CookedError(f'{field} must be an integer')
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise CookedError(f'{field} must be an integer') from exc


def _resolve_local_datetime(cooked_at) -> datetime:
    """
    Heure « murale » du cuisiné. Un `cooked_at` ISO avec offset garde l'heure locale de l'appareil
    (pas de conversion vers le fuseau serveur) ; sans offset = heure locale ; absent = maintenant.
    """
    now = timezone.localtime()
    if cooked_at in (None, ''):
        return now
    parsed = parse_datetime(str(cooked_at)) if not isinstance(cooked_at, datetime) else cooked_at
    if parsed is None:
        raise CookedError('cooked_at must be an ISO 8601 datetime')
    aware = parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)
    if aware > timezone.now() + timedelta(hours=12):
        raise CookedError('cooked_at cannot be in the future')
    return parsed if timezone.is_aware(parsed) else timezone.localtime(aware)


def validate_attachable_photos(user: AbstractBaseUser, photo_ids, extra_allowed: Q | None = None) -> list[PostPhoto]:
    """Photos uploadées par l'utilisateur, non brouillon, pas encore rattachées à un post (ordre conservé)."""
    if photo_ids in (None, ''):
        return []
    if not isinstance(photo_ids, list):
        raise CookedError('photo_ids must be a list')
    try:
        ids = [int(pid) for pid in photo_ids]
    except (TypeError, ValueError) as exc:
        raise CookedError('photo_ids must contain integers') from exc
    if len(ids) > MAX_POST_PHOTOS:
        raise CookedError(f'You can select up to {MAX_POST_PHOTOS} photos')
    if len(set(ids)) != len(ids):
        raise CookedError('photo_ids must not contain duplicates')
    allowed = Q(uploaded_by=user) & (Q(meal_plan__isnull=True) | Q(meal_plan__user=user))
    if extra_allowed is not None:
        allowed |= extra_allowed
    photos = {p.id: p for p in PostPhoto.objects.filter(id__in=ids, is_draft=False, post__isnull=True).filter(allowed)}
    if len(photos) != len(ids):
        raise CookedError('Some photos are invalid or already attached to a post')
    return [photos[pid] for pid in ids]


def attach_photos(post: Post, photos: list[PostPhoto]) -> None:
    """Rattache des photos au post (à la suite des existantes) et au repas du post."""
    if not photos:
        return
    start = (post.photos.order_by('-order').values_list('order', flat=True).first() or 0) + 1
    for offset, photo in enumerate(photos):
        photo.post = post
        photo.order = start + offset
        update_fields = ['post', 'order']
        if post.meal_plan_id and photo.meal_plan_id != post.meal_plan_id and not photo.recipe_batch_id:
            photo.meal_plan_id = post.meal_plan_id
            update_fields.append('meal_plan')
        photo.save(update_fields=update_fields)
    # Upload direct S3 : la taille d'affichage est lue en tâche de fond (cadre du post côté app).
    from .photo_dimensions import schedule_photo_dimensions

    schedule_photo_dimensions(photos)


def _meal_plan_batches(meal_plan: MealPlan) -> list[RecipeBatch]:
    return [
        mprb.recipe_batch
        for mprb in meal_plan.meal_plan_recipe_batches.select_related('recipe_batch__recipe').order_by('order')
        if mprb.recipe_batch_id
    ]


def _find_same_day_cooked_post(user, target_date, recipe: Recipe | None, label: str) -> Post | None:
    """Idempotence naturelle : un même plat ne se logue qu'une fois par jour (double tap, retry)."""
    qs = Post.objects.filter(user=user, meal_plan__user=user, meal_plan__date=target_date)
    if recipe:
        return qs.filter(meal_plan__meal_plan_recipe_batches__recipe_batch__recipe=recipe).order_by('id').first()
    if label:
        return (
            qs.filter(Q(label__iexact=label) | Q(meal_plan__custom_label__iexact=label[:80]))
            .filter(meal_plan__meal_plan_recipe_batches__isnull=True)
            .order_by('id')
            .first()
        )
    return None


def _resolve_or_create_meal_plan(user, target_date, meal_time: str, local_dt: datetime, recipe, label):
    """Réutilise un repas existant pertinent, sinon crée le créneau (standard si libre, sinon « other »)."""
    has_post = Exists(Post.objects.filter(meal_plan_id=OuterRef('pk')))
    owned_today = MealPlan.objects.filter(user=user, date=target_date).annotate(_has_post=has_post)

    # 1) Repas planifié ce jour-là qui contient déjà la recette (créneau inféré en priorité).
    if recipe:
        candidates = list(
            owned_today.filter(_has_post=False, meal_plan_recipe_batches__recipe_batch__recipe=recipe).distinct()
        )
        if candidates:
            candidates.sort(key=lambda mp: (mp.slot_key != meal_time, mp.id))
            return candidates[0], False

    # 2) Créneau standard inféré : réutilisé s'il est vide (pas de recette, pas de post, pas d'autre plat).
    if meal_time in _STANDARD_SLOTS:
        existing = owned_today.filter(slot_key=meal_time).first()
        if existing is None:
            return (
                MealPlan.objects.create(
                    user=user,
                    date=target_date,
                    meal_time=meal_time,
                    slot_key=meal_time,
                    custom_label=(label or '')[:80],
                    meal_type='recipe',
                    confirmed=False,
                ),
                True,
            )
        # Sur un créneau standard, custom_label non vide = plat libre déjà prévu (pas un nom de créneau).
        existing_label = (existing.custom_label or '').strip().lower()
        same_dish = not existing_label or (bool(label) and existing_label == label[:80].lower())
        if (
            not existing._has_post
            and same_dish
            and not MealPlanRecipeBatch.objects.filter(meal_plan_id=existing.id).exists()
        ):
            return existing, False

    # 3) Nouveau créneau « other » (le créneau standard est déjà pris par un autre repas).
    return (
        MealPlan.objects.create(
            user=user,
            date=target_date,
            meal_time='other',
            slot_key=str(uuid.uuid4()),
            custom_label=(label or 'Repas')[:80],
            scheduled_time=local_dt.time().replace(second=0, microsecond=0),
            meal_type='recipe',
            confirmed=False,
        ),
        True,
    )


def _publish_if_requested(post: Post, publish: bool) -> Post:
    if publish and not post.is_published:
        post.is_published = True
        post.save(update_fields=['is_published', 'updated_at'])
    return post


def log_cooked(
    user: AbstractBaseUser,
    *,
    recipe_id=None,
    label: str | None = None,
    meal_plan_id=None,
    cooked_at=None,
    meal_time: str | None = None,
    comment: str | None = None,
    photo_ids=None,
    cooking_time_minutes=None,
    publish: bool = True,
    basket_item_id=None,
    recipe_text: str | None = None,
    import_url: str | None = None,
) -> CookedResult:
    """
    « J'ai cuisiné X » en 1 appel : repas (réutilisé ou créé, confirmé), recette liée et batches
    cuisinés, Post (publié si `publish`). Idempotent par repas et par (jour, plat).
    Valide tout avant d'écrire ; les écritures sont atomiques.

    Toujours une recette (ckd2 lot 2, `cooked_recipe`) : sans `recipe_id`, le plat devient une recette
    (sa propre recette du même nom, une recette déjà importée depuis `import_url`, sinon une nouvelle
    recette au nom du plat) ; `recipe_text` / `import_url` la complètent en arrière-plan.
    """
    from . import cooked_recipe

    # ---- Validation (aucune écriture) ----
    label = (label or '').strip()[:LABEL_MAX_LENGTH]
    comment = (comment or '').strip()

    recipe = None
    rid = _parse_int(recipe_id, 'recipe_id')
    if rid is not None:
        from .recipe_access import usable_recipe

        recipe = usable_recipe(user, rid)  # jamais la recette privée d'un autre
        if not recipe:
            raise CookedError(f'Recipe {rid} not found', 404, 'recipe_not_found')

    meal_plan = None
    mid = _parse_int(meal_plan_id, 'meal_plan_id')
    if mid is not None:
        meal_plan = MealPlan.objects.filter(get_accessible_meal_plan_filter(user), id=mid).first()
        if not meal_plan:
            raise CookedError('Meal plan not found', 404, 'meal_plan_not_found')
        if meal_plan.user_id != user.id:
            # Invité : on ne modifie pas le repas de l'hôte ; l'app logue sans meal_plan_id.
            raise CookedError('Only the meal plan owner can log it as cooked', 403, 'not_meal_plan_owner')

    basket_item = None
    if basket_item_id not in (None, ''):
        basket_item = _basket_item_or_error(user, basket_item_id)
        if meal_plan is None and basket_item.meal_plan_id:
            meal_plan = basket_item.meal_plan  # envie déjà datée : on cuisine son repas
        elif meal_plan is None and recipe is None and not label:
            recipe = basket_item.recipe
            label = '' if recipe else basket_item.label[:LABEL_MAX_LENGTH]

    # ---- Recette du plat (ckd2 lot 2) ----
    recipe_text = '' if recipe else cooked_recipe.clean_recipe_text(recipe_text)
    url = '' if recipe else cooked_recipe.clean_import_url(import_url)
    if url is None:
        raise CookedError('import_url must be a valid http(s) URL', 400, 'invalid_import_url')
    host = cooked_recipe.link_host(url) if url else ''
    if url:
        recipe_text = ''  # le lien prime : une seule recette par plat
        if cooked_recipe.is_default_link_label(label, host):
            label = ''  # « Recette de marmiton.org » posé par l'app : pas un nom choisi
        recipe = cooked_recipe.find_imported_recipe(user, url)  # déjà importée : liée tout de suite
        if recipe:
            url = ''

    # Recette posée au collage (lot 2 bis, `POST /recipes/import-link/`) : publiée avec le post ; son
    # import encore en cours est renvoyé en `recipe_job` (le fil prendra le titre importé).
    link_recipe = recipe if cooked_recipe.is_unposted_link_recipe(user, recipe) else None
    link_job = cooked_recipe.running_link_job(user, recipe)
    if recipe is not None and recipe.import_source_url and label:
        if cooked_recipe.is_default_link_label(label, cooked_recipe.link_host(recipe.import_source_url)):
            label = ''  # « Recette de marmiton.org » : pas un nom choisi, le titre importé prime

    if not (recipe or label or meal_plan or recipe_text or url):
        raise CookedError('recipe_id, label or meal_plan_id is required', 400, 'missing_dish')

    new_recipe = None  # (titre, plan, nom choisi ?) de la recette à créer dans la transaction
    meal_has_recipes = bool(meal_plan) and MealPlanRecipeBatch.objects.filter(meal_plan=meal_plan).exists()
    if recipe is None and not meal_has_recipes:
        dish_name = label or (meal_plan.custom_label.strip() if meal_plan else '')
        plan = cooked_recipe.CookedRecipePlan(recipe_text=recipe_text, import_url=url, host=host)
        if not (recipe_text or url):
            recipe = cooked_recipe.find_own_recipe_named(user, dish_name)  # un plat refait = même recette
        if recipe is None and (dish_name or recipe_text or url):
            title = dish_name or (
                cooked_recipe.default_link_title(host) if url else cooked_recipe.WRITTEN_RECIPE_DEFAULT_TITLE
            )
            new_recipe = (title, plan, bool(dish_name))

    if meal_time not in (None, ''):
        meal_time = str(meal_time).strip().lower()
        if meal_time not in _ALLOWED_MEAL_TIMES:
            raise CookedError(f'meal_time must be one of: {", ".join(_ALLOWED_MEAL_TIMES)}')
    local_dt = _resolve_local_datetime(cooked_at)
    if not meal_time:
        meal_time = infer_meal_time_from_hour(local_dt.hour)

    extra_photos = Q(meal_plan_id=meal_plan.id) if meal_plan else None
    photos = validate_attachable_photos(user, photo_ids, extra_allowed=extra_photos)

    cooking_time = _parse_int(cooking_time_minutes, 'cooking_time_minutes')
    if cooking_time is not None:
        cooking_time = max(0, cooking_time)

    if basket_item is None:
        basket_item = _basket_item_or_error(
            user, None, meal_plan=meal_plan, cooked_on=local_dt.date(), recipe=recipe, label=label
        )

    # ---- Idempotence ----
    if meal_plan:
        existing = Post.objects.filter(meal_plan=meal_plan).order_by('-is_published', 'id').first()
        if existing:
            _link_basket_item_to_existing(basket_item, existing)
            return CookedResult(_publish_if_requested(existing, publish), False)
        has_dish = bool(recipe or label or new_recipe or meal_plan.custom_label.strip())
        if not has_dish and not MealPlanRecipeBatch.objects.filter(meal_plan=meal_plan).exists():
            raise CookedError('label or recipe_id is required for an empty meal', 400, 'missing_dish')
    else:
        existing = _find_same_day_cooked_post(user, local_dt.date(), recipe, label)
        if existing:
            _link_basket_item_to_existing(basket_item, existing)
            return CookedResult(_publish_if_requested(existing, publish), False)

    # ---- Écritures ----
    recipe_job = link_job
    created_recipe = None
    with transaction.atomic():
        if link_recipe is not None:
            cooked_recipe.publish_link_recipe(link_recipe)
        if new_recipe is not None:
            title, plan, named = new_recipe
            recipe = created_recipe = cooked_recipe.create_cooked_recipe(user, title=title, plan=plan)
            recipe_job = cooked_recipe.enqueue_cooked_recipe_job(user, recipe, plan=plan, keep_title=named)
        if meal_plan:
            meal_plan_created = False
        else:
            meal_plan, meal_plan_created = _resolve_or_create_meal_plan(
                user, local_dt.date(), meal_time, local_dt, recipe, label
            )
        undo = {
            'meal_plan_created': meal_plan_created,
            'meal_plan_was_confirmed': False if meal_plan_created else meal_plan.confirmed,
            'meal_type': meal_plan.meal_type,
            'custom_label': meal_plan.custom_label,
            'added_recipe_ids': [],
            'uncooked_batch_ids': [],
        }
        if created_recipe is not None:
            undo['created_recipe_id'] = created_recipe.id
        if link_recipe is not None:
            undo['link_recipe_id'] = link_recipe.id

        if recipe and basket_item and basket_item.recipe_id == recipe.id:
            # Envie du panier : sa recette entre dans le repas avec son batch (liste de courses conservée).
            from recipes.services.basket_service import attach_batch_to_meal_plan

            if attach_batch_to_meal_plan(basket_item, meal_plan):
                undo['added_recipe_ids'].append(recipe.id)
        if recipe:
            result = add_recipes_to_meal_plan(user, meal_plan.id, [recipe.id])
            undo['added_recipe_ids'] += list(result.added_recipe_ids)

        batches = _meal_plan_batches(meal_plan)
        undo['uncooked_batch_ids'] = [b.id for b in batches if not b.is_cooked]
        for batch in batches:
            complete_recipe_batch_workflow(user, batch)

        update_fields = ['confirmed', 'updated_at']
        meal_plan.confirmed = True
        if batches and meal_plan.meal_type != 'recipe':
            meal_plan.meal_type = 'recipe'
            update_fields.append('meal_type')
        if label and not batches and meal_plan.custom_label != label[:80]:
            # Les clients ≤ 1.0.8 affichent `meal_plan.custom_label` pour un post sans recette.
            meal_plan.custom_label = label[:80]
            update_fields.append('custom_label')
        meal_plan.save(update_fields=update_fields)

        inspired_by = None
        if basket_item is not None:
            from recipes.services.basket_service import mark_item_cooked

            undo['basket_item'] = mark_item_cooked(basket_item, meal_plan, meal_plan_created=meal_plan_created)
            inspired_by = basket_item.inspired_by_post if basket_item.inspired_by_post_id else None

        cover = next((b for b in batches if recipe and b.recipe_id == recipe.id), batches[0] if batches else None)
        post = Post.objects.create(
            user=user,
            meal_plan=meal_plan,
            recipe_batch=cover,
            label=label,
            comment=comment,
            cooking_time_minutes=cooking_time,
            is_published=bool(publish),
            cooked_undo=undo,
            inspired_by_post=inspired_by,
        )
        attach_photos(post, photos)

    if inspired_by is not None:
        from recipes.services.basket_service import notify_inspiration_cooked

        notify_inspiration_cooked(post)
    return CookedResult(post, True, recipe_job)


def _basket_item_or_error(user, basket_item_id, **match):
    from recipes.services.basket_service import BasketError, resolve_basket_item_for_cooked

    try:
        return resolve_basket_item_for_cooked(user, basket_item_id=basket_item_id, **match)
    except BasketError as exc:
        raise CookedError(exc.message, exc.status_code, exc.code) from exc


def _link_basket_item_to_existing(basket_item, post: Post) -> None:
    """Plat déjà logué aujourd'hui (double tap, autre chemin) : l'envie est cochée sur ce repas."""
    if basket_item is None or not post.meal_plan_id or post.meal_plan.user_id != basket_item.user_id:
        return
    if basket_item.meal_plan_id is None:
        from recipes.services.basket_service import mark_item_cooked

        mark_item_cooked(basket_item, post.meal_plan, meal_plan_created=False)
    if basket_item.inspired_by_post_id and not post.inspired_by_post_id:
        Post.objects.filter(pk=post.pk).update(inspired_by_post_id=basket_item.inspired_by_post_id)
        post.inspired_by_post_id = basket_item.inspired_by_post_id


def undo_cooked(post: Post) -> None:
    """
    « Annuler » juste après un log : supprime le post et restaure l'état du repas noté dans
    `cooked_undo` (repas créé → supprimé ; repas existant → confirmé/recettes/batches restaurés).
    Sans `cooked_undo` (autres flux) : suppression simple du post.
    """
    undo = post.cooked_undo or {}
    meal_plan = post.meal_plan
    user = post.user
    with transaction.atomic():
        post.delete()
        if not undo or not meal_plan or Post.objects.filter(meal_plan=meal_plan).exists():
            return

        if undo.get('basket_item'):
            from recipes.services.basket_service import restore_item_after_undo

            restore_item_after_undo(undo['basket_item'])

        if undo.get('meal_plan_created'):
            batch_ids = list(meal_plan.meal_plan_recipe_batches.values_list('recipe_batch_id', flat=True))
            meal_plan.delete()
            RecipeBatch.objects.filter(
                id__in=batch_ids, created_by=user, meal_plan_recipe_batches__isnull=True, basket_items__isnull=True
            ).delete()
            _delete_created_recipe(user, undo.get('created_recipe_id'))
            _delete_created_recipe(user, undo.get('link_recipe_id'))  # recette du collage (lot 2 bis)
            return

        added = undo.get('added_recipe_ids') or []
        if added:
            mprbs = MealPlanRecipeBatch.objects.filter(meal_plan=meal_plan, recipe_batch__recipe_id__in=added)
            batch_ids = list(mprbs.values_list('recipe_batch_id', flat=True))
            mprbs.delete()
            # Le batch d'une envie du panier reste (liste de courses) : l'envie redevient « à faire ».
            RecipeBatch.objects.filter(
                id__in=batch_ids, created_by=user, meal_plan_recipe_batches__isnull=True, basket_items__isnull=True
            ).delete()

        uncooked = undo.get('uncooked_batch_ids') or []
        if uncooked:
            RecipeBatch.objects.filter(id__in=uncooked).update(is_cooked=False)
            CookingProgress.objects.filter(user=user, recipe_batch_id__in=uncooked, status='completed').update(
                status='abandoned'
            )

        meal_plan.confirmed = bool(undo.get('meal_plan_was_confirmed'))
        meal_plan.meal_type = undo.get('meal_type') or meal_plan.meal_type
        meal_plan.custom_label = undo.get('custom_label', meal_plan.custom_label) or ''
        meal_plan.save(update_fields=['confirmed', 'meal_type', 'custom_label', 'updated_at'])
        _delete_created_recipe(user, undo.get('created_recipe_id'))
        _delete_created_recipe(user, undo.get('link_recipe_id'))  # recette du collage (lot 2 bis)


def _delete_created_recipe(user, recipe_id) -> None:
    """Recette posée par ce log (ckd2 lot 2) : supprimée à l'annulation si plus rien ne l'utilise
    (sa mise en forme / son import en cours s'arrête : la demande part avec elle)."""
    if not recipe_id:
        return
    Recipe.objects.filter(id=recipe_id, created_by=user).exclude(
        Exists(RecipeBatch.objects.filter(recipe_id=OuterRef('pk')))
    ).delete()
