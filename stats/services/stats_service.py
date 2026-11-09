"""
Stats « cuisiné » d'un utilisateur, calculées à la volée (volumes faibles) avec cache par utilisateur.

Source unique du « cuisiné » : `recipes.services.cooked_service.cooked_meal_plans` (MealPlan possédé,
`confirmed=True`, date = MealPlan.date, fuseau Europe/Paris = TIME_ZONE).

Plafonds doux (spec gamification §3.3) :
- compteurs : au plus MAX_COOKED_PER_DAY repas cuisinés comptés par jour ;
- collections / badges / plat star : un même plat compte une fois par jour (« plat-jour ») ;
- invités anonymes : au plus MAX_ANON_GUESTS_PER_MEAL par repas ; temps : au plus MAX_MINUTES_PER_MEAL.

Aucun champ de jugement nutritionnel : groupes alimentaires et végétaux sont des compteurs de curiosité.
Requêtes : nombre constant (~9) quel que soit l'historique (values() + agrégation Python).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from django.core.cache import cache
from django.db.models import Count, Max, OuterRef, Subquery
from django.utils import timezone

from recipes.models import CookingProgress, MealInvitation, MealPlan, MealPlanRecipeBatch, Post, RecipeIngredient
from recipes.services.cooked_service import cooked_meal_plans, join_titles_fr
from recipes.services.dish_service import dish_key, normalize_label

from ..cuisines import CUISINES_BY_CODE, NON_COLLECTIBLE, cuisine_payload
from ..food_groups import FOOD_GROUPS, seed_classify
from ..text import fold

MAX_COOKED_PER_DAY = 4
MAX_ANON_GUESTS_PER_MEAL = 12
MAX_MINUTES_PER_MEAL = 360
PERIODS = ('week', 'month', 'year', 'all')
CACHE_TTL_SECONDS = 600
TOP_DISHES_MAX = 5
MONTHS_FR = (
    'janvier', 'février', 'mars', 'avril', 'mai', 'juin',
    'juillet', 'août', 'septembre', 'octobre', 'novembre', 'décembre',
)  # fmt: skip


# ---------------------------------------------------------------------------
# Période
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Period:
    kind: str
    start: date | None
    end: date | None
    year: int | None = None

    def as_dict(self) -> dict:
        return {
            'type': self.kind,
            'start': self.start.isoformat() if self.start else None,
            'end': self.end.isoformat() if self.end else None,
            'year': self.year,
        }


class PeriodError(ValueError):
    pass


def resolve_period(kind: str | None = 'month', year=None, today: date | None = None) -> Period:
    today = today or timezone.localdate()
    kind = (kind or 'month').strip().lower()
    if kind not in PERIODS:
        raise PeriodError(f'period must be one of: {", ".join(PERIODS)}')
    if kind == 'week':
        start = today - timedelta(days=today.weekday())
        return Period('week', start, start + timedelta(days=6))
    if kind == 'month':
        start = today.replace(day=1)
        nxt = (start + timedelta(days=32)).replace(day=1)
        return Period('month', start, nxt - timedelta(days=1))
    if kind == 'year':
        try:
            y = int(year) if year not in (None, '') else today.year
        except (TypeError, ValueError):
            raise PeriodError('year must be an integer')
        if y < 2000 or y > today.year:
            raise PeriodError('year out of range')
        return Period('year', date(y, 1, 1), date(y, 12, 31), y)
    return Period('all', None, None)


# ---------------------------------------------------------------------------
# Foyer (invitations-rethink) : ses membres ne comptent pas comme « régalés ».
# ---------------------------------------------------------------------------
def household_member_ids(user) -> set[int]:
    """Comptes du foyer de `user` (liens acceptés, dans les deux sens) : jamais comptés comme convives."""
    from accounts.services.household_service import household_member_ids_map

    uid = getattr(user, 'id', None)
    return household_member_ids_map([uid]).get(uid, set()) if uid else set()


# ---------------------------------------------------------------------------
# Chargement
# ---------------------------------------------------------------------------
@dataclass
class Meal:
    id: int
    date: date
    custom_label: str
    guest_count: int
    recipes: list[dict] = field(default_factory=list)  # {id, title, cuisine, image_path}
    batch_ids: list[int] = field(default_factory=list)
    posts: list[dict] = field(default_factory=list)

    @property
    def recipe_ids(self) -> tuple[int, ...]:
        return tuple(sorted({r['id'] for r in self.recipes}))

    @property
    def label(self) -> str:
        titles = []
        for r in self.recipes:
            if r['title'] and r['title'] not in titles:
                titles.append(r['title'])
        if titles:
            return join_titles_fr(titles)
        for p in self.posts:
            if (p['label'] or '').strip():
                return p['label'].strip()
        return (self.custom_label or '').strip()

    @property
    def dish_key(self) -> tuple:
        if self.recipes:
            return ('r', self.recipe_ids)
        label = normalize_label(self.label)  # même normalisation que la page du plat
        return ('l', label) if label else ('m', self.id)


@dataclass
class StatsResult:
    """Résultat interne (dates de première occurrence incluses pour les badges)."""

    period: Period
    payload: dict
    # Timelines pour badges (premières occurrences, triées par date)
    first_cooked_on: date | None = None
    first_photo_on: date | None = None
    ingredient_first_seen: dict[int, tuple[str, date]] = field(default_factory=dict)  # id → (nom plié, date)
    distinct_ingredient_dates: list[date] = field(default_factory=list)
    plant_dates: list[date] = field(default_factory=list)
    cuisine_dates: list[date] = field(default_factory=list)
    streak_runs: list[tuple[int, date]] = field(default_factory=list)  # (longueur de série, lundi)
    people_fed_timeline: list[tuple[date, int]] = field(default_factory=list)  # (date, cumul)
    # Pour le Récap
    months: list[int] = field(default_factory=list)
    biggest_fan: dict | None = None


def _week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _streaks(mondays: set[date], today: date) -> tuple[int, int, list[tuple[int, date]]]:
    """(série courante, meilleure série, [(longueur, lundi)] chronologique). Semaines ISO consécutives."""
    runs: list[tuple[int, date]] = []
    best = run = 0
    prev = None
    for monday in sorted(mondays):
        run = run + 1 if prev is not None and (monday - prev).days == 7 else 1
        best = max(best, run)
        runs.append((run, monday))
        prev = monday
    this_week = _week_monday(today)
    # La semaine en cours sans cuisiné ne casse pas la série (elle n'est pas finie).
    anchor = this_week if this_week in mondays else this_week - timedelta(days=7)
    current = 0
    while anchor in mondays:
        current += 1
        anchor -= timedelta(days=7)
    return current, best, runs


def _load_meals(user, start, end) -> list[Meal]:
    plans = cooked_meal_plans(user, start, end)
    meals = {
        row['id']: Meal(row['id'], row['date'], row['custom_label'] or '', row['guest_count'] or 0)
        for row in plans.order_by('date', 'id').values('id', 'date', 'custom_label', 'guest_count')
    }
    if not meals:
        return []
    plan_ids = plans.values('id')
    for row in (
        MealPlanRecipeBatch.objects.filter(meal_plan_id__in=plan_ids)
        .order_by('order', 'id')
        .values(
            'meal_plan_id',
            'recipe_batch_id',
            'recipe_batch__recipe_id',
            'recipe_batch__recipe__title',
            'recipe_batch__recipe__cuisine',
            'recipe_batch__recipe__image_path',
        )
    ):
        meal = meals.get(row['meal_plan_id'])
        if meal is None:
            continue
        meal.batch_ids.append(row['recipe_batch_id'])
        if row['recipe_batch__recipe_id']:
            meal.recipes.append(
                {
                    'id': row['recipe_batch__recipe_id'],
                    'title': row['recipe_batch__recipe__title'] or '',
                    'cuisine': row['recipe_batch__recipe__cuisine'],
                    'image_path': row['recipe_batch__recipe__image_path'],
                }
            )
    for row in (
        Post.objects.filter(meal_plan_id__in=plan_ids, user=user)
        .annotate(miam=Count('cookies', distinct=True), n_photos=Count('photos', distinct=True))
        .order_by('id')
        .values('id', 'meal_plan_id', 'label', 'cooking_time_minutes', 'is_published', 'created_at', 'miam', 'n_photos')
    ):
        meals[row['meal_plan_id']].posts.append(row)
    return list(meals.values())


def _ingredient_info(row: dict) -> dict:
    """Profil de l'ingrédient ; sans profil (pas encore classé) : seed déterministe, sinon non classé."""
    name = row['ingredient__name']
    if row['ingredient__profile__food_group'] is not None:
        return {
            'name': name,
            'group': row['ingredient__profile__food_group'],
            'plant': bool(row['ingredient__profile__is_plant']),
            'staple': bool(row['ingredient__profile__is_basic_staple']),
        }
    seed = seed_classify(name)
    if seed:
        return {'name': name, 'group': seed.food_group, 'plant': seed.is_plant, 'staple': seed.is_basic_staple}
    return {'name': name, 'group': None, 'plant': False, 'staple': False}


def _cap_per_day(meals: list[Meal]) -> list[Meal]:
    per_day: Counter = Counter()
    kept = []
    for meal in meals:  # triés par (date, id)
        if per_day[meal.date] < MAX_COOKED_PER_DAY:
            per_day[meal.date] += 1
            kept.append(meal)
    return kept


def _dish_days(meals: list[Meal]) -> list[Meal]:
    seen, kept = set(), []
    for meal in meals:
        key = (meal.date, meal.dish_key)
        if key not in seen:
            seen.add(key)
            kept.append(meal)
    return kept


def week_cooking_summary(user, start: date, end: date) -> dict:
    """
    Récap hebdo (push du dimanche) : plats cuisinés sur [start, end] (plafond doux par jour) et
    plats « nouveaux » (jamais cuisinés avant `start` ; un plat sans recette ni libellé ne compte pas).
    """
    meals = _cap_per_day(_load_meals(user, start, end))
    if not meals:
        return {'cooked': 0, 'new_dishes': 0}
    week_keys = {m.dish_key for m in meals if m.dish_key[0] != 'm'}
    before = {m.dish_key for m in _load_meals(user, None, start - timedelta(days=1))} if week_keys else set()
    return {'cooked': len(meals), 'new_dishes': len(week_keys - before)}


def _cooking_minutes(user, meals: list[Meal]) -> int:
    batch_ids = {bid for m in meals if not any(p['cooking_time_minutes'] for p in m.posts) for bid in m.batch_ids}
    progress = {}
    if batch_ids:
        progress = dict(
            CookingProgress.objects.filter(
                user=user, status='completed', recipe_batch_id__in=batch_ids, total_time_minutes__gt=0
            )
            .values('recipe_batch_id')
            .annotate(m=Max('total_time_minutes'))
            .values_list('recipe_batch_id', 'm')
        )
    total = 0
    for meal in meals:
        post_minutes = max((p['cooking_time_minutes'] or 0 for p in meal.posts), default=0)
        minutes = post_minutes or sum(progress.get(bid, 0) for bid in meal.batch_ids)
        total += min(max(minutes, 0), MAX_MINUTES_PER_MEAL)
    return total


def _photo_url(post_id: int) -> str | None:
    from recipes.models import PostPhoto

    photo = PostPhoto.objects.filter(post_id=post_id).order_by('order', 'created_at').first()
    return photo.image_url if photo else None


def _recipe_image_url(image_path: str | None) -> str | None:
    if not image_path:
        return None
    from recipes.models import Recipe

    return Recipe(image_path=image_path).image_url


# ---------------------------------------------------------------------------
# Calcul
# ---------------------------------------------------------------------------
def compute_stats(user, period: Period, *, today: date | None = None, with_recap_extras: bool = False) -> StatsResult:
    today = today or timezone.localdate()
    meals = _load_meals(user, period.start, period.end)
    counted = _cap_per_day(meals)
    dish_days = _dish_days(meals)
    excluded_guests = household_member_ids(user) | {user.id}

    # --- Ingrédients (plats-jours) ---
    recipe_ids = {r['id'] for m in dish_days for r in m.recipes}
    ingredients_by_recipe: dict[int, list[dict]] = defaultdict(list)
    if recipe_ids:
        for row in RecipeIngredient.objects.filter(recipe_id__in=recipe_ids).values(
            'recipe_id',
            'ingredient_id',
            'ingredient__name',
            'ingredient__profile__food_group',
            'ingredient__profile__is_plant',
            'ingredient__profile__is_basic_staple',
        ):
            ingredients_by_recipe[row['recipe_id']].append(row)

    ing_info: dict[int, dict] = {}
    ing_first: dict[int, date] = {}
    ing_uses: Counter = Counter()
    for meal in dish_days:
        seen_in_meal = set()
        for recipe in meal.recipes:
            for row in ingredients_by_recipe.get(recipe['id'], ()):
                iid = row['ingredient_id']
                if iid not in ing_info:
                    ing_info[iid] = _ingredient_info(row)
                    ing_first[iid] = meal.date
                if iid not in seen_in_meal:
                    seen_in_meal.add(iid)
                    ing_uses[iid] += 1
    diverse = {iid for iid, info in ing_info.items() if not info['staple']}

    previously = set()
    if period.start and diverse:
        previously = set(
            RecipeIngredient.objects.filter(
                ingredient_id__in=diverse,
                recipe__batches__meal_plan_recipe_batches__meal_plan__in=cooked_meal_plans(
                    user, end=period.start - timedelta(days=1)
                ).values('id'),
            )
            .values_list('ingredient_id', flat=True)
            .distinct()
        )
    new_ids = sorted(diverse - previously, key=lambda i: (ing_first[i], ing_info[i]['name']))
    plants = {i for i in diverse if ing_info[i]['plant']}
    groups = Counter(ing_info[i]['group'] for i in sorted(diverse) if ing_info[i]['group'])  # ex æquo : ordre stable
    fav_id = max(diverse, key=lambda i: (ing_uses[i], -ing_first[i].toordinal()), default=None)

    # --- Cuisines (plats-jours) ---
    cuisine_counts: Counter = Counter()
    cuisine_first: dict[str, date] = {}
    for meal in dish_days:
        # Code hors liste fermée (ancien texte libre) = « autre » : affiché comme tel, jamais compté comme exploré.
        for code in {
            r['cuisine'] if r['cuisine'] in CUISINES_BY_CODE else 'autre' for r in meal.recipes if r['cuisine']
        }:
            cuisine_counts[code] += 1
            cuisine_first.setdefault(code, meal.date)
    explored = [c for c in cuisine_counts if c not in NON_COLLECTIBLE]

    # --- Séries hebdomadaires (historique complet pour la série courante) ---
    if period.kind == 'all':
        all_mondays = {_week_monday(m.date) for m in meals}
    else:
        all_mondays = {_week_monday(d) for d in cooked_meal_plans(user).dates('date', 'day')}
    current, _, _ = _streaks(all_mondays, today)
    period_mondays = {_week_monday(m.date) for m in meals}
    _, best, runs = _streaks(period_mondays, today)
    week_counts = Counter(_week_monday(m.date) for m in counted)
    record_week = max(week_counts.items(), key=lambda kv: (kv[1], kv[0]), default=None)

    # --- Mois ---
    month_counts = Counter((m.date.year, m.date.month) for m in counted)
    top_month = max(month_counts.items(), key=lambda kv: (kv[1], kv[0]), default=None)

    # --- Plat star (plats-jours) ---
    dish_count: Counter = Counter(m.dish_key for m in dish_days)
    dish_last: dict[tuple, Meal] = {m.dish_key: m for m in dish_days}  # dernier = plus récent
    star = None
    if dish_count:
        key, n = max(dish_count.items(), key=lambda kv: (kv[1], dish_last[kv[0]].date))
        meal = dish_last[key]
        recipe_ids_star = list(meal.recipe_ids)
        star_post = next((p for m in reversed(dish_days) if m.dish_key == key for p in m.posts if p['n_photos']), None)
        image = _photo_url(star_post['id']) if star_post else None
        if not image and meal.recipes:
            image = _recipe_image_url(meal.recipes[0]['image_path'])
        star = {
            'label': meal.label or 'Plat maison',
            'recipe_id': recipe_ids_star[0] if len(recipe_ids_star) == 1 else None,
            'recipe_ids': recipe_ids_star,
            'count': n,
            'image_url': image,
        }

    top_dishes = _top_dishes(dish_days, dish_count, dish_last)

    # --- Miam ---
    posts = [p for m in meals for p in m.posts]
    published = [p for p in posts if p['is_published']]
    miam_total = sum(p['miam'] for p in published)
    top_post = max(published, key=lambda p: (p['miam'], p['id']), default=None)
    top_miam = None
    if top_post and top_post['miam'] > 0:
        meal = next(m for m in meals if m.id == top_post['meal_plan_id'])
        top_miam = {
            'post_id': top_post['id'],
            'title': (top_post['label'] or '').strip() or meal.label or 'Plat maison',
            'miam_count': top_post['miam'],
            'date': meal.date.isoformat(),
            'photo_url': _photo_url(top_post['id']),
        }
    photo_dates = [m.date for m in meals for p in m.posts if p['n_photos']]

    # --- Régalés (hôte) / invité ---
    counted_ids = {m.id for m in counted}
    guests_by_meal: dict[int, set[int]] = defaultdict(set)
    guest_names: dict[int, str] = {}
    if counted_ids:
        for row in MealInvitation.objects.filter(meal_plan_id__in=counted_ids, status='accepted').values(
            'meal_plan_id', 'invitee_id', 'invitee__username'
        ):
            if row['invitee_id'] in excluded_guests:
                continue
            guests_by_meal[row['meal_plan_id']].add(row['invitee_id'])
            guest_names[row['invitee_id']] = row['invitee__username']
    seen_guests: set[int] = set()
    guest_meals: Counter = Counter()
    anonymous = meals_hosted = 0
    fed_timeline: list[tuple[date, int]] = []
    for meal in counted:
        guests = guests_by_meal.get(meal.id, set())
        anon = min(max(meal.guest_count, 0), MAX_ANON_GUESTS_PER_MEAL)
        if guests or anon:
            meals_hosted += 1
            before = len(seen_guests) + anonymous
            seen_guests |= guests
            anonymous += anon
            guest_meals.update(guests)
            if len(seen_guests) + anonymous > before:
                fed_timeline.append((meal.date, len(seen_guests) + anonymous))
    top_guest = None
    if guest_meals:
        gid, n = max(guest_meals.items(), key=lambda kv: (kv[1], -kv[0]))
        top_guest = {'user_id': gid, 'username': guest_names.get(gid), 'meals': n}
    # « Invité N fois » : les repas cuisinés par le foyer ne comptent pas (excluded_guests ⊇ foyer).
    invited_qs = MealInvitation.objects.filter(invitee=user, status='accepted', meal_plan__date__lte=today).exclude(
        meal_plan__user_id__in=excluded_guests
    )
    if period.start:
        invited_qs = invited_qs.filter(meal_plan__date__gte=period.start)
    if period.end:
        invited_qs = invited_qs.filter(meal_plan__date__lte=period.end)
    invited_count = invited_qs.count()

    payload = {
        'period': period.as_dict(),
        'cooked_count': len(counted),
        'cooked_days': len({m.date for m in meals}),
        'total_cooking_minutes': _cooking_minutes(user, counted),
        'distinct_ingredients': len(diverse),
        'new_ingredients_in_period': len(new_ids),
        'new_ingredients_sample': [ing_info[i]['name'] for i in new_ids[:5]],
        'favorite_ingredient': (
            {'id': fav_id, 'name': ing_info[fav_id]['name'], 'count': ing_uses[fav_id]} if fav_id else None
        ),
        'distinct_plants': len(plants),
        'food_groups': [
            {'code': code, 'label': FOOD_GROUPS.get(code, code), 'count': n} for code, n in groups.most_common()
        ],
        'cuisines': [cuisine_payload(code, n) for code, n in cuisine_counts.most_common()],
        'cuisines_count': len(explored),
        'weekly_streak_current': current,
        'weekly_streak_best': best,
        'record_week': (
            {
                'week_start': record_week[0].isoformat(),
                'iso_week': '%d-W%02d' % record_week[0].isocalendar()[:2],
                'count': record_week[1],
            }
            if record_week
            else None
        ),
        'most_active_month': (
            {
                'month': '%04d-%02d' % top_month[0],
                'label': MONTHS_FR[top_month[0][1] - 1],
                'count': top_month[1],
            }
            if top_month
            else None
        ),
        'star_dish': star,
        'top_dishes': top_dishes,
        'top_miam_post': top_miam,
        'miam_received': miam_total,
        'hosted': {
            'people_fed_count': len(seen_guests) + anonymous,
            'guests_with_account': len(seen_guests),
            'anonymous_guests': anonymous,
            'meals_hosted': meals_hosted,
            'top_guest': top_guest,
            'invited_count': invited_count,
        },
    }

    result = StatsResult(
        period=period,
        payload=payload,
        first_cooked_on=meals[0].date if meals else None,
        first_photo_on=min(photo_dates) if photo_dates else None,
        ingredient_first_seen={i: (fold(info['name']), ing_first[i]) for i, info in ing_info.items()},
        distinct_ingredient_dates=sorted(ing_first[i] for i in diverse),
        plant_dates=sorted(ing_first[i] for i in plants),
        cuisine_dates=sorted(cuisine_first[c] for c in explored),
        streak_runs=runs,
        people_fed_timeline=fed_timeline,
    )
    if with_recap_extras:
        result.months = [month_counts.get((period.year or today.year, m), 0) for m in range(1, 13)]
        result.biggest_fan = _biggest_fan(user, [p['id'] for p in published])
    return result


def _top_dishes(dish_days: list[Meal], dish_count: Counter, dish_last: dict[tuple, Meal]) -> list[dict]:
    """
    Plats les plus cuisinés (plats-jours, ≤ TOP_DISHES_MAX) : nombre, dernière date, photo (dernier post avec
    photo, sinon image de la recette). Plats sans recette ni libellé exclus. Une requête au plus (photos).
    """
    from recipes.models import PostPhoto

    ranked = sorted(
        (key for key in dish_count if key[0] != 'm'),
        key=lambda key: (-dish_count[key], -dish_last[key].date.toordinal(), str(key)),
    )[:TOP_DISHES_MAX]
    if not ranked:
        return []
    wanted = set(ranked)
    photo_post: dict[tuple, int] = {}
    for meal in reversed(dish_days):
        if meal.dish_key in wanted and meal.dish_key not in photo_post:
            post = next((p for p in meal.posts if p['n_photos']), None)
            if post:
                photo_post[meal.dish_key] = post['id']
    photo_urls: dict[int, str] = {}
    if photo_post:
        for photo in PostPhoto.objects.filter(post_id__in=photo_post.values()).order_by(
            'post_id', 'order', 'created_at'
        ):
            photo_urls.setdefault(photo.post_id, photo.image_url)
    dishes = []
    for key in ranked:
        meal = dish_last[key]
        recipe_ids = list(meal.recipe_ids)
        image = photo_urls.get(photo_post.get(key))
        if not image and meal.recipes:
            image = _recipe_image_url(meal.recipes[0]['image_path'])
        dishes.append(
            {
                'dish_key': dish_key(meal.recipes[0]['id'] if meal.recipes else None, meal.label),
                'label': meal.label or 'Plat maison',
                'recipe_id': recipe_ids[0] if len(recipe_ids) == 1 else None,
                'recipe_ids': recipe_ids,
                'count': dish_count[key],
                'last_cooked_on': meal.date.isoformat(),
                'image_url': image,
            }
        )
    return dishes


def _biggest_fan(user, post_ids: list[int]) -> dict | None:
    if not post_ids:
        return None
    from recipes.models import PostCookie

    row = (
        PostCookie.objects.filter(post_id__in=post_ids)
        .exclude(user=user)
        .values('user_id', 'user__username')
        .annotate(n=Count('id'))
        .order_by('-n', 'user_id')
        .first()
    )
    return {'user_id': row['user_id'], 'username': row['user__username'], 'miam_count': row['n']} if row else None


# ---------------------------------------------------------------------------
# Vues publiques (autre utilisateur) + cache
# ---------------------------------------------------------------------------
PUBLIC_KEYS = (
    'period',
    'cooked_count',
    'cooked_days',
    'distinct_ingredients',
    'distinct_plants',
    'cuisines',
    'cuisines_count',
    'weekly_streak_current',
    'weekly_streak_best',
)


def public_subset(payload: dict) -> dict:
    data = {k: payload[k] for k in PUBLIC_KEYS}
    star = payload.get('star_dish')
    data['star_dish'] = {'label': star['label'], 'count': star['count']} if star else None
    # Pas de photo ni de recette (posts privés possibles) : libellé, nombre, dernière date.
    data['top_dishes'] = [
        {'label': d['label'], 'count': d['count'], 'last_cooked_on': d['last_cooked_on']}
        for d in payload.get('top_dishes') or ()
    ]
    return data


def _version_key(user_id: int) -> str:
    return f'stats:ver:{user_id}'


def invalidate_user_stats(user_id: int | None) -> None:
    """Invalide le cache stats d'un utilisateur (miam, invitations… ; le « cuisiné » est couvert par l'empreinte)."""
    if not user_id:
        return
    key = _version_key(user_id)
    try:
        if not cache.add(key, 1, None):
            cache.incr(key)
    except Exception:  # cache indisponible : l'empreinte et le TTL suffisent
        pass


def _fingerprint(user_id: int) -> tuple:
    """Empreinte DB des données « cuisiné » (couvre aussi les `.update()` qui court-circuitent les signaux)."""
    from django.contrib.auth import get_user_model

    def agg(qs, expr):
        return Subquery(qs.order_by().values('user_id').annotate(v=expr).values('v')[:1])

    mp = MealPlan.objects.filter(user_id=OuterRef('pk'), confirmed=True)
    po = Post.objects.filter(user_id=OuterRef('pk'))
    row = (
        get_user_model()
        .objects.filter(pk=user_id)
        .annotate(
            a=agg(mp, Count('id')),
            b=agg(mp, Max('updated_at')),
            c=agg(po, Count('id')),
            d=agg(po, Max('updated_at')),
        )
        .values_list('a', 'b', 'c', 'd')
        .first()
    )
    return tuple(v.timestamp() if hasattr(v, 'timestamp') else v for v in (row or ()))


def stats_cache_token(user_id: int) -> str:
    """Jeton de fraîcheur du cache stats : version (miam, invitations…) + empreinte DB du « cuisiné »."""
    try:
        version = cache.get(_version_key(user_id), 0)
    except Exception:
        version = 0
    return '%s:%s' % (version, '-'.join(str(v) for v in _fingerprint(user_id)))


def get_stats_payload(user, period: Period, *, public: bool = False, today: date | None = None) -> dict:
    """Payload stats (mis en cache par utilisateur, période et empreinte des données)."""
    today = today or timezone.localdate()
    key = 'stats:v3:%s:%s:%s:%s:%s:%s' % (
        user.id,
        period.kind,
        period.start,
        period.end,
        today.isoformat(),
        stats_cache_token(user.id),
    )
    payload = cache.get(key)
    if payload is None:
        payload = compute_stats(user, period, today=today).payload
        cache.set(key, payload, CACHE_TTL_SECONDS)
    return public_subset(payload) if public else payload
