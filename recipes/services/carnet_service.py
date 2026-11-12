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

Filtre « Avec moi » (`filter=with_me`, ckd2 lot 3 suite) : les plats **des autres** où le propriétaire du
carnet a été mis (invitation acceptée au repas du post : convive direct, foyer, invité d'avant ; « Me
retirer » = refusée → absent). Ce ne sont **pas** ses plats : jamais comptés dans `count` du carnet, les stats
ni les badges. Seulement des posts publiés, visibles par le lecteur (`visible_posts`), hôte actif, sans blocage
entre l'hôte et le lecteur ni (carnet d'un autre) entre l'hôte et le propriétaire. Un post y figure une fois
(un repost n'est pas un post). Chaque entrée porte `host`. `with_me_available` (une requête `exists`) dit si
le filtre a quelque chose à montrer : l'app ne l'affiche qu'alors.

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

from recipes.models import MealInvitation, MealPlan, MealPlanRecipeBatch, Post, PostPhoto, RecipeBatch
from recipes.services.post_visibility import visible_posts

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50

# Rang du type dans la clé de tri (départage à date et heure égales, puis id).
_RANK = {'post': 3, 'meal': 2, 'batch': 1}
_KIND_BY_RANK = {v: k for k, v in _RANK.items()}


FILTER_WITH_ME = 'with_me'
FILTERS = (FILTER_WITH_ME,)


class InvalidFilter(ValueError):
    pass


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


def _with_me_posts(owner_id: int, viewer) -> QuerySet:
    """Posts publiés des autres où `owner_id` est convive (invitation acceptée), visibles par `viewer`."""
    from accounts.blocks import blocked_user_ids_for

    my_meals = MealInvitation.objects.filter(invitee_id=owner_id, status='accepted').values('meal_plan_id')
    qs = Post.objects.filter(is_published=True, meal_plan_id__in=my_meals).exclude(user_id=owner_id)
    qs = visible_posts(viewer, queryset=qs)
    if viewer.id != owner_id:
        owner_blocks = blocked_user_ids_for(get_user_model()(pk=owner_id))
        if owner_blocks:
            qs = qs.exclude(user_id__in=owner_blocks)
    return qs


def _with_me_source(owner_id: int, viewer) -> QuerySet:
    return _keys(
        _with_me_posts(owner_id, viewer),
        day=Coalesce(F('meal_plan__date'), TruncDate('created_at'), output_field=DateField()),
        ts=F('created_at'),
        kind='post',
    )


def with_me_available(viewer, owner_id: int) -> bool:
    return _with_me_posts(owner_id, viewer).exists()


def _sources(owner_id: int, viewer, filter_: str | None = None) -> list[QuerySet]:
    if filter_ == FILTER_WITH_ME:
        return [_with_me_source(owner_id, viewer)]
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


def _invitations_prefetch(attr: str = 'invitations') -> Prefetch:
    qs = (
        MealInvitation.objects.filter(status='accepted')
        .select_related('invitee')
        .only(
            'id',
            'meal_plan_id',
            'status',
            'created_at',
            'invitee__id',
            'invitee__first_name',
            'invitee__username',
            'invitee__avatar_url',
        )
        .order_by('created_at', 'id')
    )
    return Prefetch(attr, queryset=qs)


def _load(kind: str, ids: list[int], *, is_self: bool, with_host: bool = False) -> dict:
    if not ids:
        return {}
    if kind == 'post':
        qs = Post.objects.filter(id__in=ids).select_related('recipe_batch__recipe', 'meal_plan')
        if with_host:
            qs = qs.select_related('user')
        prefetch = [
            _photos_prefetch(),
            Prefetch('meal_plan__meal_plan_recipe_batches', queryset=_batches_prefetch().queryset),
            _invitations_prefetch('meal_plan__invitations'),
        ]
        qs = qs.prefetch_related(*prefetch)
    elif kind == 'meal':
        qs = MealPlan.objects.filter(id__in=ids).prefetch_related(
            _batches_prefetch(),
            _photos_prefetch('draft_photos', post__isnull=True),
            _invitations_prefetch('invitations'),
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


def _guests_payload(
    meal_plan: MealPlan | None,
    *,
    is_self: bool,
    visible_user_ids: set[int] | None,
) -> tuple[list[dict], int]:
    if meal_plan is None:
        return [], 0
    accepted = [inv for inv in meal_plan.invitations.all() if inv.status == 'accepted']
    total_count = len(accepted)
    guests = []
    for inv in accepted:
        user = getattr(inv, 'invitee', None)
        if not user:
            continue
        is_visible = is_self or (visible_user_ids is not None and user.id in visible_user_ids)
        if is_visible:
            guests.append(
                {
                    'id': user.id,
                    'first_name': user.first_name or user.username,
                    'avatar_url': user.avatar_url or None,
                }
            )
            if len(guests) == 3:
                break
    return guests, total_count


def _post_entry(post: Post, *, is_self: bool, cache: dict, visible_user_ids: set[int] | None = None) -> dict:
    from recipes.services.dish_service import post_dish

    meal_plan = post.meal_plan if post.meal_plan_id else None
    batch = post.recipe_batch if post.recipe_batch_id else None
    mprb = _main_mprb(meal_plan, post.recipe_batch_id)
    if batch is None and mprb is not None:
        batch = mprb.recipe_batch
    recipe = batch.recipe if batch is not None and batch.recipe_id else None
    dish = post_dish(post) or {}
    photos = list(post.photos.all())
    guests, guests_count = _guests_payload(
        meal_plan,
        is_self=is_self,
        visible_user_ids=visible_user_ids,
    )
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
        'guests': guests,
        'guests_count': guests_count,
        **_slot(meal_plan),
    }
    if is_self:
        entry['batch_id'] = batch.id if batch is not None else None
        entry['meal_plan_id'] = post.meal_plan_id
        entry['servings'] = _servings(meal_plan, mprb, cache)
        if not entry['note'] and batch is not None:
            entry['note'] = (batch.notes or '').strip() or None
    return entry


def _meal_entry(
    meal_plan: MealPlan, cache: dict, *, is_self: bool = True, visible_user_ids: set[int] | None = None
) -> dict:
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
    guests, guests_count = _guests_payload(
        meal_plan,
        is_self=is_self,
        visible_user_ids=visible_user_ids,
    )
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
        'guests': guests,
        'guests_count': guests_count,
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
        'guests': [],
        'guests_count': 0,
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


def _host_payload(user) -> dict:
    return {
        'id': user.id,
        'username': user.username,
        'first_name': user.first_name or user.username,
        'avatar_url': user.avatar_url or None,
    }


def build_carnet_page(
    viewer,
    owner,
    *,
    cursor: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    filter_: str | None = None,
) -> dict:
    """
    `{count, next_cursor, results, filter, with_me_available}` ; `InvalidCursor` si le curseur est illisible,
    `InvalidFilter` si le filtre est inconnu. `filter_='with_me'` : les plats des autres où `owner` est convive.
    """
    if filter_ and filter_ not in FILTERS:
        raise InvalidFilter(filter_)
    with_me = filter_ == FILTER_WITH_ME
    page_size = max(1, min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    after = Cursor.decode(cursor) if cursor else None
    # Le plat d'un autre : jamais ses batches, repas ni portions, même sur mon carnet.
    is_self = viewer.id == owner.id and not with_me
    sources = _sources(owner.id, viewer, filter_)

    count = _union(sources).count()
    rows = list(
        _union([_after(qs, after) for qs in sources]).order_by('-k_day', '-k_ts', '-k_rank', '-k_id')[: page_size + 1]
    )
    has_more = len(rows) > page_size
    rows = rows[:page_size]

    ids: dict[str, list[int]] = {'post': [], 'meal': [], 'batch': []}
    for row in rows:
        ids[_KIND_BY_RANK[row['k_rank']]].append(row['k_id'])
    loaded = {kind: _load(kind, kind_ids, is_self=is_self, with_host=with_me) for kind, kind_ids in ids.items()}

    visible_user_ids: set[int] | None = None
    if not is_self:
        from accounts.blocks import blocked_user_ids_for
        from accounts.models import Follow

        following_ids = set(Follow.objects.filter(follower_id=viewer.id).values_list('following_id', flat=True))
        blocked_ids = blocked_user_ids_for(viewer)
        visible_user_ids = (following_ids | {viewer.id}) - blocked_ids

    cache: dict = {}
    results = []
    for row in rows:
        kind = _KIND_BY_RANK[row['k_rank']]
        obj = loaded[kind].get(row['k_id'])
        if obj is None:  # supprimé entre les deux requêtes
            continue
        if kind == 'post':
            entry = _post_entry(obj, is_self=is_self, cache=cache, visible_user_ids=visible_user_ids)
            if with_me:
                entry['host'] = _host_payload(obj.user)
        elif kind == 'meal':
            entry = _meal_entry(obj, cache, is_self=is_self, visible_user_ids=visible_user_ids)
        else:
            entry = _batch_entry(obj)
        day = row['k_day']
        results.append({'key': f'{kind}-{obj.pk}', 'date': day.isoformat() if day else None, **entry})

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = Cursor(last['k_day'], last['k_ts'], last['k_rank'], last['k_id']).encode()
    # Le filtre n'apparaît que s'il a quelque chose à montrer (une requête `exists`, 1re page seulement).
    if with_me:
        available = count > 0
    elif after is None:
        available = with_me_available(viewer, owner.id)
    else:
        available = None
    return {
        'count': count,
        'next_cursor': next_cursor,
        'results': results,
        'filter': filter_ or None,
        'with_me_available': available,
    }
