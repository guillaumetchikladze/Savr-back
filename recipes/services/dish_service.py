"""
« Le carnet du plat » : identité d'un plat (source unique), page du plat et « Classiques de tes amis ».
Contrat front : .claude-work/specs/dish-page.md.

Identité d'un plat — ``dish_key`` (une seule fonction : `dish_key()`, utilisée par les posts, la page,
les classiques et `top_dishes` des stats) :

- **recette** → ``r-<recipe_id>``. Le modèle `Recipe` n'a pas de relation de copie / fork : importer une URL
  déjà importée (recette publique) renvoie la recette existante, et « À mon tour » réutilise la recette de
  l'ami ; l'id de recette est donc déjà partagé entre amis. Limite connue : deux imports *privés* de la même
  URL restent deux plats (une future racine `Recipe.root` se brancherait ici, sans changer les clés).
- **plat libre** (aucune recette) → ``l-<slug>`` où ``slug = normalize_label(libellé)`` : minuscules, sans
  accents ni ponctuation (`stats.text.fold`), articles de tête retirés (« les pâtes » = « pâtes »), pluriels
  simples ramenés au singulier (« aux » → « au », -s / -x final des mots de plus de 3 lettres) :
  « Les pâtes au pesto » = « pâte au pesto » = « PÂTES AU PESTO ! » → ``l-pate-au-pesto``.
- rien (ni recette ni libellé) → pas de plat (``None``).

Un repas avec plusieurs recettes appartient à **chacune** (la page du risotto liste aussi « risotto +
salade »). Le plat **principal** d'un post (lien `dish` des serializers) = recette de couverture
(`Post.recipe_batch`), sinon 1re recette du repas (même règle que « À mon tour »), sinon libellé du post,
sinon libellé du repas. Une recette l'emporte toujours sur le libellé (comme les stats).

« Cuisiné » (mes versions) = `cooked_service.cooked_meal_plans` ; « chez tes amis » = posts **visibles**
(`post_visibility.visible_posts`, blocages inclus) des comptes que l'utilisateur suit (`density_service.friend_ids`).
Nombre de requêtes constant (indépendant du nombre de versions, d'amis ou de posts).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date as date_type
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.models import Count, Exists, OuterRef, Q, Subquery
from django.utils import timezone

from stats.text import fold

from ..models import BasketItem, CookingProgress, MealPlanRecipeBatch, Post, PostPhoto, Recipe
from .cooked_service import cooked_meal_plans
from .post_visibility import visible_posts

RECIPE_PREFIX = 'r-'
LABEL_PREFIX = 'l-'
LABEL_SLUG_MAX = 120
_ARTICLES = frozenset(
    {
        'le',
        'la',
        'les',
        'l',
        'un',
        'une',
        'des',
        'du',
        'de',
        'd',
        'mon',
        'ma',
        'mes',
        'ton',
        'ta',
        'tes',
        'nos',
        'notre',
    }
)
_WORD_MAP = {'aux': 'au'}
_SLUG_RE = re.compile(r'^[a-z0-9]+(-[a-z0-9]+)*$')

VERSIONS_PAGE_SIZE = 10
VERSIONS_PAGE_SIZE_MAX = 30
FRIENDS_POSTS_MAX = 10
FRIENDS_USERS_MAX = 12
CLASSIC_AVATARS_MAX = 5
MAX_MEASURED_MINUTES = 360


def classics_window_days() -> int:
    return int(getattr(settings, 'DISH_CLASSICS_WINDOW_DAYS', 30))


def classics_min_friends() -> int:
    return int(getattr(settings, 'DISH_CLASSICS_MIN_FRIENDS', 3))


def classics_max() -> int:
    return int(getattr(settings, 'DISH_CLASSICS_MAX', 5))


def classics_cache_seconds() -> int:
    return int(getattr(settings, 'DISH_CLASSICS_CACHE_SECONDS', 300))


class DishNotFound(Exception):
    pass


# ---------------------------------------------------------------------------
# Identité
# ---------------------------------------------------------------------------
def _singular(word: str) -> str:
    if word in _WORD_MAP:
        return _WORD_MAP[word]
    if len(word) > 3 and word[-1] in 'sx' and not word.endswith('ss'):
        return word[:-1]
    return word


def normalize_label(label: str | None) -> str:
    """Slug d'un plat libre (« Les Pâtes au pesto » → ``pate-au-pesto``) ; '' si vide."""
    words = fold(label).split()
    while len(words) > 1 and words[0] in _ARTICLES:
        words = words[1:]
    return '-'.join(_singular(w) for w in words)[:LABEL_SLUG_MAX].strip('-')


def dish_key(recipe_id=None, label: str | None = None) -> str | None:
    """**Source unique** de l'identité d'un plat : ``r-<id>`` (recette) sinon ``l-<slug>`` (libellé), sinon None."""
    if recipe_id:
        return f'{RECIPE_PREFIX}{int(recipe_id)}'
    slug = normalize_label(label)
    return f'{LABEL_PREFIX}{slug}' if slug else None


@dataclass(frozen=True)
class DishRef:
    recipe_id: int | None = None
    slug: str = ''

    @property
    def key(self) -> str:
        return dish_key(self.recipe_id) if self.recipe_id else f'{LABEL_PREFIX}{self.slug}'  # type: ignore[return-value]


def parse_dish_key(key: str | None) -> DishRef:
    key = (key or '').strip().lower()
    if key.startswith(RECIPE_PREFIX) and key[len(RECIPE_PREFIX) :].isdigit():
        return DishRef(recipe_id=int(key[len(RECIPE_PREFIX) :]))
    if key.startswith(LABEL_PREFIX) and _SLUG_RE.match(key[len(LABEL_PREFIX) :]):
        return label_ref(key[len(LABEL_PREFIX) :])
    raise DishNotFound(key)


def label_ref(text: str) -> DishRef:
    """Slug d'URL ou texte libre (« Risotto aux champignons ») → plat libre normalisé."""
    slug = normalize_label(str(text).replace('-', ' '))
    if not slug:
        raise DishNotFound(text)
    return DishRef(slug=slug)


def post_primary_recipe(post: Post) -> Recipe | None:
    """Recette de couverture, sinon 1re recette du repas (préchargements du feed : 0 requête)."""
    batch = post.recipe_batch if post.recipe_batch_id else None
    if batch is not None and batch.recipe_id:
        return batch.recipe
    meal_plan = post.meal_plan if post.meal_plan_id else None
    if meal_plan is not None:
        for mprb in meal_plan.meal_plan_recipe_batches.all():
            recipe = getattr(getattr(mprb, 'recipe_batch', None), 'recipe', None)
            if recipe is not None:
                return recipe
    return None


def post_dish(post: Post) -> dict | None:
    """``{key, title, recipe_id}`` du plat principal d'un post (lien vers la page du plat), ou None."""
    recipe = post_primary_recipe(post)
    if recipe is not None:
        return {'key': dish_key(recipe.id), 'title': recipe.title, 'recipe_id': recipe.id}
    label = (post.label or '').strip()
    if not label and post.meal_plan_id and post.meal_plan is not None:
        label = (post.meal_plan.custom_label or '').strip()
    key = dish_key(label=label)
    return {'key': key, 'title': label, 'recipe_id': None} if key else None


def _row_keys(recipe_ids: list[int], label: str) -> list[str]:
    """Plats d'un repas / post : chacune de ses recettes, sinon son libellé."""
    if recipe_ids:
        return [dish_key(rid) for rid in dict.fromkeys(recipe_ids)]  # type: ignore[misc]
    key = dish_key(label=label)
    return [key] if key else []


# ---------------------------------------------------------------------------
# Données
# ---------------------------------------------------------------------------
def _photo_url(photo: PostPhoto | None) -> str | None:
    return photo.image_url if photo is not None else None


def _first_photos(post_ids) -> dict[int, PostPhoto]:
    photos: dict[int, PostPhoto] = {}
    ids = [pid for pid in post_ids if pid]
    if ids:
        for photo in PostPhoto.objects.filter(post_id__in=ids).order_by('post_id', 'order', 'created_at'):
            photos.setdefault(photo.post_id, photo)
    return photos


def _local_date(row: dict) -> date_type:
    return row.get('meal_plan__date') or timezone.localdate(row['created_at'])


def _my_meals(user, ref: DishRef) -> list[dict]:
    """Mes repas cuisinés de ce plat, du plus récent au plus ancien : [{id, date, label}]."""
    plans = cooked_meal_plans(user)
    first_label = (
        Post.objects.filter(meal_plan_id=OuterRef('pk'), user_id=user.id)
        .exclude(label='')
        .order_by('id')
        .values('label')
    )
    if ref.recipe_id:
        cover = Post.objects.filter(meal_plan_id=OuterRef('pk'), user_id=user.id, recipe_batch__recipe_id=ref.recipe_id)
        rows = (
            plans.annotate(_cover=Exists(cover), _label=Subquery(first_label[:1]))
            .filter(Q(meal_plan_recipe_batches__recipe_batch__recipe_id=ref.recipe_id) | Q(_cover=True))
            .distinct()
            .order_by('-date', '-id')
            .values('id', 'date', '_label', 'custom_label')
        )
        return [{'id': r['id'], 'date': r['date'], 'label': (r['_label'] or '').strip()} for r in rows]
    with_recipe = MealPlanRecipeBatch.objects.filter(meal_plan_id=OuterRef('pk'))
    cover = Post.objects.filter(meal_plan_id=OuterRef('pk'), user_id=user.id, recipe_batch__isnull=False)
    rows = (
        plans.annotate(_r=Exists(with_recipe), _cover=Exists(cover), _label=Subquery(first_label[:1]))
        .filter(_r=False, _cover=False)
        .order_by('-date', '-id')
        .values('id', 'date', '_label', 'custom_label')
    )
    meals = []
    for r in rows:
        label = (r['_label'] or '').strip() or (r['custom_label'] or '').strip()
        if normalize_label(label) == ref.slug:
            meals.append({'id': r['id'], 'date': r['date'], 'label': label})
    return meals


def _friend_post_rows(user, ref: DishRef, friends: set[int]) -> list[dict]:
    """Posts visibles des amis pour ce plat (tout l'historique), du plus récent au plus ancien."""
    if not friends:
        return []
    qs = visible_posts(user).filter(user_id__in=friends)
    fields = ('id', 'user_id', 'created_at', 'meal_plan__date', 'label', 'meal_plan__custom_label')
    if ref.recipe_id:
        qs = qs.filter(
            Q(recipe_batch__recipe_id=ref.recipe_id)
            | Q(meal_plan__meal_plan_recipe_batches__recipe_batch__recipe_id=ref.recipe_id)
        ).distinct()
        rows = list(qs.values(*fields))
    else:
        with_recipe = MealPlanRecipeBatch.objects.filter(meal_plan_id=OuterRef('meal_plan_id'))
        qs = qs.filter(recipe_batch__isnull=True).annotate(_r=Exists(with_recipe)).filter(_r=False)
        rows = []
        for r in qs.values(*fields):
            label = (r['label'] or '').strip() or (r['meal_plan__custom_label'] or '').strip()
            if normalize_label(label) == ref.slug:
                r['label'] = label
                rows.append(r)
    for r in rows:
        r['date'] = _local_date(r)
    rows.sort(key=lambda r: (r['date'], r['id']), reverse=True)
    return rows


def _measured_minutes(user, ref: DishRef, meal_ids: list[int]) -> dict[int, int]:
    """Durée mesurée par le mode cuisine (max par batch de la recette), par repas."""
    if not ref.recipe_id or not meal_ids:
        return {}
    progress = (
        CookingProgress.objects.filter(
            user_id=user.id, status='completed', total_time_minutes__gt=0, recipe_batch_id=OuterRef('recipe_batch_id')
        )
        .order_by('-total_time_minutes')
        .values('total_time_minutes')[:1]
    )
    minutes: dict[int, int] = {}
    for row in (
        MealPlanRecipeBatch.objects.filter(meal_plan_id__in=meal_ids, recipe_batch__recipe_id=ref.recipe_id)
        .annotate(m=Subquery(progress))
        .values('meal_plan_id', 'm')
    ):
        if row['m']:
            minutes[row['meal_plan_id']] = max(minutes.get(row['meal_plan_id'], 0), min(row['m'], MAX_MEASURED_MINUTES))
    return minutes


def _user_light(users, request) -> dict[int, dict]:
    from ..serializers import UserLightSerializer

    context = {'request': request}
    return {u.id: UserLightSerializer(u, context=context).data for u in users}


def _post_payload(post: Post, *, user_payload: dict | None = None, changes: dict | None = None) -> dict:
    from stats.services.highlights import post_ardoise

    photos = list(post.photos.all())
    ardoise = post_ardoise(post)
    data = {
        'id': post.id,
        'label': (post.label or '').strip(),
        'comment': post.comment or '',
        'photo_url': _photo_url(photos[0]) if photos else None,
        'photos': [{'id': p.id, 'url': p.image_url} for p in photos],
        'cuisine': ardoise['cuisine'],
        'highlights': ardoise['highlights'],
        'cookies_count': getattr(post, '_miam', 0),
        'is_published': post.is_published,
        'created_at': post.created_at.isoformat() if post.created_at else None,
        # « Ce que tu as changé » sur cette recette dans ce repas (ckd2 lot 3b) : post déjà visible du lecteur.
        'changes': (changes or {}).get(post.meal_plan_id, '') if post.meal_plan_id else '',
    }
    if user_payload is not None:
        data['user'] = user_payload
    return data


def _recipe_changes(recipe_id, posts: dict[int, Post]) -> dict[int, str]:
    """« Ce que tu as changé » de la recette par repas des posts chargés (1 requête, 0 sans recette)."""
    meal_ids = {p.meal_plan_id for p in posts.values() if p.meal_plan_id}
    if not recipe_id or not meal_ids:
        return {}
    rows = (
        MealPlanRecipeBatch.objects.filter(meal_plan_id__in=meal_ids, recipe_batch__recipe_id=recipe_id)
        .exclude(changes='')
        .order_by('order', 'id')
        .values_list('meal_plan_id', 'changes')
    )
    out: dict[int, str] = {}
    for meal_id, text in rows:
        out.setdefault(meal_id, text)
    return out


def _load_posts(ids) -> dict[int, Post]:
    from stats.services.highlights import ensure_post_highlights

    ids = [i for i in ids if i]
    if not ids:
        return {}
    posts = list(
        Post.objects.filter(id__in=ids)
        .select_related('user')
        .annotate(_miam=Count('cookies', distinct=True))
        .prefetch_related('photos')
    )
    ensure_post_highlights(posts)  # instantanés manquants en lot (≤ 7 requêtes à froid, 0 à chaud)
    return {p.id: p for p in posts}


def _basket_action(user, ref: DishRef, title: str) -> dict:
    from .basket_service import current_week_start

    week = current_week_start(user)
    open_item = None
    for row in (
        BasketItem.objects.filter(user_id=user.id, week_start=week, carried_over_to__isnull=True)
        .filter(Q(meal_plan__isnull=True) | Q(meal_plan__confirmed=False))
        .order_by('position', 'id')
        .values('id', 'recipe_id', 'label')
    ):
        if dish_key(row['recipe_id'], row['label']) == ref.key:
            open_item = row['id']
            break
    body = {'recipe_id': ref.recipe_id} if ref.recipe_id else {'label': title}
    return {
        'week_start': week.isoformat(),
        'in_basket': open_item is not None,
        'basket_item_id': open_item,
        'method': 'POST',
        'path': '/api/basket/',
        'body': body,
    }


# ---------------------------------------------------------------------------
# Page du plat
# ---------------------------------------------------------------------------
def _nth_label(n: int) -> str:
    from stats.services.highlights import ordinal_fr

    return f'{ordinal_fr(n)} fois'


def build_dish_page(user, ref: DishRef, *, request=None, page: int = 1, page_size: int = VERSIONS_PAGE_SIZE) -> dict:
    from .density_service import friend_ids

    page = max(int(page or 1), 1)
    page_size = min(max(int(page_size or VERSIONS_PAGE_SIZE), 1), VERSIONS_PAGE_SIZE_MAX)
    recipe = None
    if ref.recipe_id:
        recipe = (
            Recipe.objects.filter(id=ref.recipe_id)
            .only('id', 'title', 'image_path', 'is_public', 'created_by_id')
            .first()
        )
        if recipe is None:
            raise DishNotFound(ref.key)

    meals = _my_meals(user, ref)
    friends = friend_ids(user)
    friend_rows = _friend_post_rows(user, ref, friends)
    if recipe is not None and not (recipe.is_public or recipe.created_by_id == user.id or meals or friend_rows):
        raise DishNotFound(ref.key)  # recette privée d'un autre, jamais croisée : on ne révèle rien
    if recipe is None and not meals and not friend_rows:
        raise DishNotFound(ref.key)

    # --- Mes versions (tous les repas : compteurs ; la page seulement : contenu des posts) ---
    meal_ids = [m['id'] for m in meals]
    my_posts: dict[int, dict] = {}  # repas → dernier post
    if meal_ids:
        for row in (
            Post.objects.filter(user_id=user.id, meal_plan_id__in=meal_ids)
            .annotate(miam=Count('cookies', distinct=True), n_photos=Count('photos', distinct=True))
            .order_by('id')
            .values('id', 'meal_plan_id', 'miam', 'n_photos')
        ):
            my_posts[row['meal_plan_id']] = row
    days = sorted({m['date'] for m in meals})
    day_rank = {d: i + 1 for i, d in enumerate(days)}
    minutes = _measured_minutes(user, ref, meal_ids)
    favorite = None
    if len(meals) >= 2:
        scored = [
            (p['miam'], p['n_photos'] > 0, m['date'], m['id'])
            for m in meals
            if (p := my_posts.get(m['id'])) and p['miam'] > 0
        ]
        if scored:
            favorite = max(scored)[3]

    start = (page - 1) * page_size
    page_meals = meals[start : start + page_size]
    photo_meal = next((m for m in meals if (my_posts.get(m['id']) or {}).get('n_photos')), None)
    friend_page = friend_rows[:FRIENDS_POSTS_MAX]
    posts = _load_posts(
        [(my_posts.get(m['id']) or {}).get('id') for m in page_meals]
        + [r['id'] for r in friend_page]
        + ([my_posts[photo_meal['id']]['id']] if photo_meal else [])
    )
    changes = _recipe_changes(ref.recipe_id, posts)

    versions = []
    for m in page_meals:
        p = my_posts.get(m['id'])
        post = posts.get(p['id']) if p else None
        nth = day_rank[m['date']]
        versions.append(
            {
                'meal_plan_id': m['id'],
                'cooked_on': m['date'].isoformat(),
                'nth': nth,
                'nth_label': _nth_label(nth),
                'label': m['label'],
                'cooking_minutes': minutes.get(m['id']),
                'is_favorite': m['id'] == favorite,
                'post': _post_payload(post, changes=changes) if post is not None else None,
            }
        )
    measured = [minutes[i] for i in meal_ids if i in minutes]

    # --- Chez tes amis ---
    today = timezone.localdate()
    window_start = today - timedelta(days=classics_window_days() - 1)
    per_friend: dict[int, dict] = {}
    for r in friend_rows:  # du plus récent au plus ancien
        entry = per_friend.setdefault(r['user_id'], {'count': 0, 'last': r['date']})
        entry['count'] += 1
    recent_friends = {r['user_id'] for r in friend_rows if window_start <= r['date'] <= today}
    shown_users = list(per_friend)[:FRIENDS_USERS_MAX]
    users_by_id = {p.user_id: p.user for p in posts.values()}
    missing = [uid for uid in shown_users if uid not in users_by_id]
    if missing:
        users_by_id.update({u.id: u for u in get_user_model().objects.filter(id__in=missing)})
    light = _user_light([users_by_id[uid] for uid in shown_users if uid in users_by_id], request)
    friend_posts = []
    for r in friend_page:
        post = posts.get(r['id'])
        if post is not None:
            friend_posts.append(
                {
                    **_post_payload(post, user_payload=light.get(post.user_id), changes=changes),
                    'cooked_on': r['date'].isoformat(),
                }
            )

    # --- Titre, image ---
    if recipe is not None:
        title = recipe.title
    else:
        title = meals[0]['label'] if meals else friend_rows[0]['label']
    image = None
    if photo_meal:
        photo_post = posts.get(my_posts[photo_meal['id']]['id'])
        photos = list(photo_post.photos.all()) if photo_post else []
        image = _photo_url(photos[0]) if photos else None
    if not image and recipe is not None:
        image = recipe.image_url
    if not image:
        image = next((fp['photo_url'] for fp in friend_posts if fp['photo_url']), None)

    min_friends = classics_min_friends()
    return {
        'dish': {'key': ref.key, 'title': title, 'recipe_id': ref.recipe_id},
        'image_url': image,
        'mine': {
            'times_cooked': len(days),
            'versions_count': len(meals),
            'first_cooked_on': days[0].isoformat() if days else None,
            'last_cooked_on': days[-1].isoformat() if days else None,
            'avg_cooking_minutes': round(sum(measured) / len(measured)) if measured else None,
            'measured_count': len(measured),
            'favorite_meal_plan_id': favorite,
        },
        'versions': {
            'count': len(meals),
            'page': page,
            'page_size': page_size,
            'has_next': start + page_size < len(meals),
            'results': versions,
        },
        'friends': {
            'count': len(per_friend),
            'posts_count': len(friend_rows),
            'users': [
                {
                    'user': light[uid],
                    'times': per_friend[uid]['count'],
                    'last_cooked_on': per_friend[uid]['last'].isoformat(),
                }
                for uid in shown_users
                if uid in light
            ],
            'posts': friend_posts,
        },
        'classic': {
            'is_friends_classic': len(recent_friends) >= min_friends,
            'friends_count': len(recent_friends),
            'min_friends': min_friends,
            'window_days': classics_window_days(),
        },
        'basket': _basket_action(user, ref, title),
    }


# ---------------------------------------------------------------------------
# « Classiques de tes amis »
# ---------------------------------------------------------------------------
def classics_window(user, week=None) -> tuple[date_type, date_type, date_type]:
    """(lundi de la semaine, début, fin) : 30 jours finissant le dimanche de la semaine (au plus aujourd'hui)."""
    from .basket_service import parse_week, user_today

    monday = parse_week(week, user)
    end = min(monday + timedelta(days=6), user_today(user))
    return monday, end - timedelta(days=classics_window_days() - 1), end


def friends_classics(user, *, week=None, request=None) -> dict:
    """Plats cuisinés par ≥ N amis distincts (posts visibles) sur la fenêtre ; mis en cache brièvement par utilisateur."""
    monday, start, end = classics_window(user, week)
    cache_key = f'dish_classics:v1:{user.id}:{monday.isoformat()}:{end.isoformat()}'
    try:
        cached = cache.get(cache_key)
    except Exception:  # pragma: no cover - cache indisponible
        cached = None
    if cached is not None:
        return cached
    payload = {
        'week_start': monday.isoformat(),
        'window': {'start': start.isoformat(), 'end': end.isoformat(), 'days': classics_window_days()},
        'min_friends': classics_min_friends(),
        'results': _compute_classics(user, start, end, request),
    }
    try:
        cache.set(cache_key, payload, classics_cache_seconds())
    except Exception:  # pragma: no cover
        pass
    return payload


def _compute_classics(user, start: date_type, end: date_type, request) -> list[dict]:
    from .density_service import friend_ids

    friends = friend_ids(user)
    if not friends:
        return []
    rows = list(
        visible_posts(user)
        .filter(user_id__in=friends)
        .filter(
            Q(meal_plan__date__gte=start, meal_plan__date__lte=end)
            | Q(meal_plan__isnull=True, created_at__date__gte=start, created_at__date__lte=end)
        )
        .values(
            'id',
            'user_id',
            'created_at',
            'label',
            'meal_plan_id',
            'meal_plan__date',
            'meal_plan__custom_label',
            'recipe_batch__recipe_id',
            'recipe_batch__recipe__title',
            'recipe_batch__recipe__image_path',
        )
    )
    if not rows:
        return []
    recipes: dict[int, dict] = {}
    meal_recipes: dict[int, list[int]] = defaultdict(list)
    plan_ids = {r['meal_plan_id'] for r in rows if r['meal_plan_id']}
    if plan_ids:
        for m in (
            MealPlanRecipeBatch.objects.filter(meal_plan_id__in=plan_ids, recipe_batch__recipe__isnull=False)
            .order_by('order', 'id')
            .values(
                'meal_plan_id',
                'recipe_batch__recipe_id',
                'recipe_batch__recipe__title',
                'recipe_batch__recipe__image_path',
            )
        ):
            rid = m['recipe_batch__recipe_id']
            meal_recipes[m['meal_plan_id']].append(rid)
            recipes[rid] = {
                'title': m['recipe_batch__recipe__title'],
                'image_path': m['recipe_batch__recipe__image_path'],
            }

    dishes: dict[str, dict] = {}
    for r in rows:
        r['date'] = _local_date(r)
        recipe_ids = []
        if r['recipe_batch__recipe_id']:
            rid = r['recipe_batch__recipe_id']
            recipe_ids.append(rid)
            recipes[rid] = {
                'title': r['recipe_batch__recipe__title'],
                'image_path': r['recipe_batch__recipe__image_path'],
            }
        recipe_ids += meal_recipes.get(r['meal_plan_id'], [])
        label = (r['label'] or '').strip() or (r['meal_plan__custom_label'] or '').strip()
        for key in _row_keys(recipe_ids, label):
            d = dishes.setdefault(key, {'users': {}, 'posts': [], 'label': None, 'last': None})
            stamp = (r['date'], r['id'])
            d['users'][r['user_id']] = max(d['users'].get(r['user_id'], stamp), stamp)
            d['posts'].append(r['id'])
            if d['last'] is None or stamp > d['last']:
                d['last'] = stamp
                d['label'] = label
    min_friends = classics_min_friends()
    ranked = sorted(
        ((k, d) for k, d in dishes.items() if len(d['users']) >= min_friends),
        key=lambda kd: (-len(kd[1]['users']), -kd[1]['last'][0].toordinal(), -kd[1]['last'][1], kd[0]),
    )[: classics_max()]
    if not ranked:
        return []

    avatar_ids = []
    for _, d in ranked:
        d['avatars'] = [uid for uid, _ in sorted(d['users'].items(), key=lambda kv: kv[1], reverse=True)][
            :CLASSIC_AVATARS_MAX
        ]
        avatar_ids += d['avatars']
    light = _user_light(get_user_model().objects.filter(id__in=set(avatar_ids)), request)
    photos = _first_photos({pid for _, d in ranked for pid in d['posts']})

    results = []
    for key, d in ranked:
        ref = parse_dish_key(key)
        recipe = recipes.get(ref.recipe_id) if ref.recipe_id else None
        title = (recipe or {}).get('title') or d['label'] or ''
        latest_photo_post = max((pid for pid in d['posts'] if pid in photos), default=None)
        image = _photo_url(photos.get(latest_photo_post)) if latest_photo_post else None
        if not image and recipe and recipe.get('image_path'):
            image = Recipe(image_path=recipe['image_path']).image_url
        results.append(
            {
                'dish': {'key': key, 'title': title, 'recipe_id': ref.recipe_id},
                'image_url': image,
                'friends_count': len(d['users']),
                'friends': [light[uid] for uid in d['avatars'] if uid in light],
                'posts_count': len(d['posts']),
                'last_cooked_on': d['last'][0].isoformat(),
            }
        )
    return results
