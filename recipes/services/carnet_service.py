"""
Carnet d'un profil : les plats cuisinés, du plus récent au plus ancien, paginés côté serveur.

Une entrée = un plat cuisiné, **avec ou sans recette** :

- ``post`` : un post de l'utilisateur (son souvenir : photos, note) ;
- ``meal`` : un repas cuisiné (`MealPlan.confirmed`, même définition que les stats) sans post ;
- ``batch`` : ancien batch cuisiné (`RecipeBatch.is_cooked`) rattaché à aucun repas cuisiné ni post
  (données d'avant le « J'ai cuisiné » canonique ; n'existe plus pour les nouveaux plats).

Visibilité :

- **soi** : tout (posts publiés et « rien que pour moi », repas sans post, anciens batches) ;
- **un autre** : profil visible requis (`can_view_profile_content` : suivi approuvé, sans blocage, compte
  actif ; sinon `CarnetNotVisible`), puis seulement ses posts **publiés** visibles par le lecteur
  (`post_visibility.visible_posts`). Jamais ses repas sans post, ses posts privés, ses batches, ni le
  nombre de convives (foyer). Les posts co-cuisinés (reposts) ne sont pas ses plats : absents.

Pagination par curseur (clé : date du plat, heure de saisie, type, id) : stable quand un plat est ajouté
pendant le défilement. `count` est exact (une requête). Nombre de requêtes constant par page.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime

from django.contrib.auth import get_user_model
from django.db.models import (
    DateField,
    Exists,
    F,
    IntegerField,
    OuterRef,
    Prefetch,
    Q,
    QuerySet,
    Subquery,
    Value,
)
from django.db.models.functions import Coalesce, TruncDate
from django.utils.dateparse import parse_date, parse_datetime

from recipes.models import MealPlan, MealPlanRecipeBatch, Post, PostPhoto, RecipeBatch
from recipes.services.post_visibility import visible_posts

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50

# Rang du type dans la clé de tri (départage à date et heure égales, puis id).
_RANK = {'post': 3, 'meal': 2, 'batch': 1}
_KIND_BY_RANK = {v: k for k, v in _RANK.items()}


class CarnetNotVisible(Exception):
    """Le lecteur ne peut pas voir le carnet de ce profil."""


class InvalidCursor(ValueError):
    pass


@dataclass(frozen=True)
class Cursor:
    day: date_type
    ts: datetime
    rank: int
    obj_id: int

    def encode(self) -> str:
        raw = f'{self.day.isoformat()}|{self.ts.isoformat()}|{self.rank}|{self.obj_id}'
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip('=')

    @classmethod
    def decode(cls, token: str) -> Cursor:
        try:
            padded = token + '=' * (-len(token) % 4)
            day_s, ts_s, rank_s, id_s = base64.urlsafe_b64decode(padded.encode()).decode().split('|')
            day, ts = parse_date(day_s), parse_datetime(ts_s)
            rank, obj_id = int(rank_s), int(id_s)
        except (ValueError, TypeError, binascii.Error, UnicodeDecodeError) as exc:
            raise InvalidCursor(str(exc)) from exc
        if day is None or ts is None or rank not in _KIND_BY_RANK:
            raise InvalidCursor(token)
        return cls(day, ts, rank, obj_id)


# ---------------------------------------------------------------------------
# Sources (clés de tri seulement : la page est chargée ensuite, en lot)
# ---------------------------------------------------------------------------
def _keys(qs: QuerySet, *, day, ts, kind: str) -> QuerySet:
    return qs.annotate(
        k_day=day,
        k_ts=ts,
        k_rank=Value(_RANK[kind], output_field=IntegerField()),
        k_id=F('pk'),
    )


def _post_source(owner_id: int, viewer) -> QuerySet:
    base = Post.objects.filter(user_id=owner_id)
    if viewer.id == owner_id:
        # Ses posts publiés ou « rien que pour moi » d'un plat cuisiné (pas un brouillon de repas prévu).
        qs = base.filter(Q(is_published=True) | Q(meal_plan__confirmed=True) | Q(recipe_batch__is_cooked=True))
    else:
        qs = visible_posts(viewer, queryset=base.filter(is_published=True))
    return _keys(
        qs,
        day=Coalesce(F('meal_plan__date'), TruncDate('created_at'), output_field=DateField()),
        ts=F('created_at'),
        kind='post',
    )


def _meal_source(owner_id: int) -> QuerySet:
    own_post = Post.objects.filter(meal_plan_id=OuterRef('pk'), user_id=owner_id)
    qs = MealPlan.objects.filter(user_id=owner_id, confirmed=True).exclude(Exists(own_post))
    return _keys(qs, day=F('date'), ts=F('created_at'), kind='meal')


def _batch_source(owner_id: int) -> QuerySet:
    in_cooked_meal = MealPlanRecipeBatch.objects.filter(recipe_batch_id=OuterRef('pk'), meal_plan__confirmed=True)
    own_post = Post.objects.filter(user_id=owner_id).filter(
        Q(recipe_batch_id=OuterRef('pk')) | Q(meal_plan__meal_plan_recipe_batches__recipe_batch_id=OuterRef('pk'))
    )
    last_meal_day = (
        MealPlanRecipeBatch.objects.filter(recipe_batch_id=OuterRef('pk'), meal_plan__user_id=owner_id)
        .order_by('-meal_plan__date')
        .values('meal_plan__date')[:1]
    )
    qs = (
        RecipeBatch.objects.filter(created_by_id=owner_id, is_cooked=True)
        .exclude(Exists(in_cooked_meal))
        .exclude(Exists(own_post))
    )
    return _keys(
        qs,
        day=Coalesce(
            Subquery(last_meal_day, output_field=DateField()), TruncDate('created_at'), output_field=DateField()
        ),
        ts=F('created_at'),
        kind='batch',
    )


def _sources(owner_id: int, viewer) -> list[QuerySet]:
    sources = [_post_source(owner_id, viewer)]
    if viewer.id == owner_id:
        sources += [_meal_source(owner_id), _batch_source(owner_id)]
    return sources


def _after(qs: QuerySet, cursor: Cursor | None) -> QuerySet:
    if cursor is None:
        return qs
    return qs.filter(
        Q(k_day__lt=cursor.day)
        | Q(k_day=cursor.day, k_ts__lt=cursor.ts)
        | Q(k_day=cursor.day, k_ts=cursor.ts, k_rank__lt=cursor.rank)
        | Q(k_day=cursor.day, k_ts=cursor.ts, k_rank=cursor.rank, k_id__lt=cursor.obj_id)
    )


def _union(querysets: list[QuerySet]) -> QuerySet:
    cols = ('k_day', 'k_ts', 'k_rank', 'k_id')
    rows = [qs.order_by().values(*cols) for qs in querysets]
    head, *rest = rows
    return head.union(*rest, all=True) if rest else head


# ---------------------------------------------------------------------------
# Chargement d'une page
# ---------------------------------------------------------------------------
def _batches_prefetch() -> Prefetch:
    return Prefetch(
        'meal_plan_recipe_batches',
        queryset=MealPlanRecipeBatch.objects.select_related('recipe_batch__recipe').order_by('order', 'id'),
    )


def _photos_prefetch(attr: str = 'photos', **filters) -> Prefetch:
    qs = PostPhoto.objects.filter(is_draft=False, **filters).exclude(image_path='').order_by('order', 'created_at')
    return Prefetch(attr, queryset=qs)


def _load(kind: str, ids: list[int], *, is_self: bool) -> dict:
    if not ids:
        return {}
    if kind == 'post':
        qs = Post.objects.filter(id__in=ids).select_related('recipe_batch__recipe', 'meal_plan')
        prefetch = [
            _photos_prefetch(),
            Prefetch('meal_plan__meal_plan_recipe_batches', queryset=_batches_prefetch().queryset),
        ]
        if is_self:
            prefetch.append('meal_plan__invitations')
        qs = qs.prefetch_related(*prefetch)
    elif kind == 'meal':
        qs = MealPlan.objects.filter(id__in=ids).prefetch_related(
            _batches_prefetch(), _photos_prefetch('draft_photos', post__isnull=True), 'invitations'
        )
    else:
        qs = (
            RecipeBatch.objects.filter(id__in=ids)
            .select_related('recipe')
            .prefetch_related(_photos_prefetch('draft_photos', post__isnull=True))
        )
    return {obj.pk: obj for obj in qs}


def _photo_payload(photos: list[PostPhoto]) -> dict | None:
    for photo in photos:
        url = photo.image_url
        if url:
            return {'url': url, 'width': photo.width, 'height': photo.height}
    return None


def _recipe_payload(recipe) -> dict | None:
    if recipe is None:
        return None
    return {'id': recipe.id, 'title': recipe.title, 'image_url': recipe.image_url}


def _main_mprb(meal_plan: MealPlan | None, batch_id: int | None = None):
    if meal_plan is None:
        return None
    mprbs = list(meal_plan.meal_plan_recipe_batches.all())
    if batch_id:
        match = next((m for m in mprbs if m.recipe_batch_id == batch_id), None)
        if match is not None:
            return match
    return next((m for m in mprbs if m.recipe_batch and m.recipe_batch.recipe_id), mprbs[0] if mprbs else None)


def _servings(meal_plan: MealPlan | None, mprb, cache: dict) -> int | None:
    from recipes.services.diners_service import meal_plan_people_count

    if meal_plan is None:
        return None
    if mprb is not None and mprb.portions:
        return mprb.portions
    return meal_plan_people_count(meal_plan, cache) or None


def _slot(meal_plan: MealPlan | None) -> dict:
    if meal_plan is None:
        return {'meal_time': None, 'meal_time_display': None}
    from recipes.utils import meal_plan_slot_api_fields

    return {
        'meal_time': meal_plan.meal_time,
        'meal_time_display': meal_plan_slot_api_fields(meal_plan)['meal_time_display'],
    }


def _first_time(post: Post) -> bool:
    from stats.services.highlights import is_fresh

    snapshot = post.highlights
    if not is_fresh(snapshot):
        return False  # pas d'instantané : on ne calcule rien ici (le détail du post le fera)
    return any(item.get('kind') == 'first_time' for item in snapshot.get('items') or [])


def _post_entry(post: Post, *, is_self: bool, cache: dict) -> dict:
    from recipes.services.dish_service import post_dish

    meal_plan = post.meal_plan if post.meal_plan_id else None
    batch = post.recipe_batch if post.recipe_batch_id else None
    mprb = _main_mprb(meal_plan, post.recipe_batch_id)
    if batch is None and mprb is not None:
        batch = mprb.recipe_batch
    recipe = batch.recipe if batch is not None and batch.recipe_id else None
    dish = post_dish(post) or {}
    photos = list(post.photos.all())
    entry = {
        'kind': 'post',
        'title': dish.get('title') or (recipe.title if recipe else '') or '',
        'dish_key': dish.get('key'),
        'recipe': _recipe_payload(recipe),
        'post_id': post.id,
        'thumbnail': _photo_payload(photos),
        'photos_count': len(photos),
        'note': (post.comment or '').strip() or None,
        'first_time': _first_time(post),
        'is_private': not post.is_published,
        **_slot(meal_plan),
    }
    if is_self:
        entry['batch_id'] = batch.id if batch is not None else None
        entry['meal_plan_id'] = post.meal_plan_id
        entry['servings'] = _servings(meal_plan, mprb, cache)
        if not entry['note'] and batch is not None:
            entry['note'] = (batch.notes or '').strip() or None
    return entry


def _meal_entry(meal_plan: MealPlan, cache: dict) -> dict:
    from recipes.services.cooked_service import join_titles_fr

    mprbs = [m for m in meal_plan.meal_plan_recipe_batches.all() if m.recipe_batch]
    titles = []
    for m in mprbs:
        recipe = m.recipe_batch.recipe if m.recipe_batch.recipe_id else None
        if recipe is not None and recipe.title and recipe.title not in titles:
            titles.append(recipe.title)
    mprb = _main_mprb(meal_plan)
    batch = mprb.recipe_batch if mprb is not None else None
    recipe = batch.recipe if batch is not None and batch.recipe_id else None
    photos = list(meal_plan.draft_photos.all())
    return {
        'kind': 'meal',
        'title': join_titles_fr(titles) or (meal_plan.custom_label or '').strip(),
        'dish_key': None,
        'recipe': _recipe_payload(recipe),
        'post_id': None,
        'thumbnail': _photo_payload(photos),
        'photos_count': len(photos),
        'note': ((batch.notes if batch is not None else '') or '').strip() or None,
        'first_time': False,
        'is_private': True,
        **_slot(meal_plan),
        'batch_id': batch.id if batch is not None else None,
        'meal_plan_id': meal_plan.id,
        'servings': _servings(meal_plan, mprb, cache),
    }


def _batch_entry(batch: RecipeBatch) -> dict:
    photos = list(batch.draft_photos.all())
    recipe = batch.recipe if batch.recipe_id else None
    return {
        'kind': 'batch',
        'title': (recipe.title if recipe else '') or (batch.name or ''),
        'dish_key': None,
        'recipe': _recipe_payload(recipe),
        'post_id': None,
        'thumbnail': _photo_payload(photos),
        'photos_count': len(photos),
        'note': (batch.notes or '').strip() or None,
        'first_time': False,
        'is_private': True,
        'meal_time': None,
        'meal_time_display': None,
        'batch_id': batch.id,
        'meal_plan_id': None,
        'servings': None,
    }


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def get_owner_or_raise(viewer, owner_id: int):
    from accounts.privacy import can_view_profile_content

    owner = get_user_model().objects.filter(pk=owner_id, is_active=True).first()
    if owner is None:
        raise CarnetNotVisible('not_found')
    if not can_view_profile_content(viewer, owner):
        raise CarnetNotVisible('not_visible')
    return owner


def carnet_count(viewer, owner_id: int) -> int:
    return _union(_sources(owner_id, viewer)).count()


def cooked_total(owner) -> int:
    """
    Tout ce que `owner` a cuisiné (compteur « plats » de son profil, vu par n'importe qui) : le même nombre
    que son propre carnet, plats privés et repas sans post compris. Un nombre seulement : le contenu
    reste soumis à la visibilité (`build_carnet_page`).
    """
    return carnet_count(owner, owner.id)


def build_carnet_page(viewer, owner, *, cursor: str | None = None, page_size: int = DEFAULT_PAGE_SIZE) -> dict:
    """`{count, next_cursor, results}` ; `InvalidCursor` si le curseur est illisible."""
    page_size = max(1, min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    after = Cursor.decode(cursor) if cursor else None
    is_self = viewer.id == owner.id
    sources = _sources(owner.id, viewer)

    count = _union(sources).count()
    rows = list(
        _union([_after(qs, after) for qs in sources]).order_by('-k_day', '-k_ts', '-k_rank', '-k_id')[: page_size + 1]
    )
    has_more = len(rows) > page_size
    rows = rows[:page_size]

    ids: dict[str, list[int]] = {'post': [], 'meal': [], 'batch': []}
    for row in rows:
        ids[_KIND_BY_RANK[row['k_rank']]].append(row['k_id'])
    loaded = {kind: _load(kind, kind_ids, is_self=is_self) for kind, kind_ids in ids.items()}

    cache: dict = {}
    results = []
    for row in rows:
        kind = _KIND_BY_RANK[row['k_rank']]
        obj = loaded[kind].get(row['k_id'])
        if obj is None:  # supprimé entre les deux requêtes
            continue
        if kind == 'post':
            entry = _post_entry(obj, is_self=is_self, cache=cache)
        elif kind == 'meal':
            entry = _meal_entry(obj, cache)
        else:
            entry = _batch_entry(obj)
        day = row['k_day']
        results.append({'key': f'{kind}-{obj.pk}', 'date': day.isoformat() if day else None, **entry})

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = Cursor(last['k_day'], last['k_ts'], last['k_rank'], last['k_id']).encode()
    return {'count': count, 'next_cursor': next_cursor, 'results': results}
