"""
« L'ardoise » : puces automatiques d'un post « J'ai cuisiné » (contrat front : .claude-work/specs/ardoise.md).

Chaque post expose `cuisine` ({code, label, flag, country} ou null) et `highlights` (≤ 3 puces
`{kind, value, label, icon}`), choisies parmi des faits **positifs** : 1re fois / ne fois (ce trimestre),
nouveaux ingrédients, semaine d'affilée, végétaux, convives, durée mesurée par le mode cuisine.
Jamais de calories ni de score santé (liste fermée `KINDS`).

Sémantique « à la date du post » : l'historique pris en compte est celui des repas cuisinés
(`cooked_meal_plans`) strictement antérieurs au repas du post (date, puis id) : un plat cuisiné plus
tard ne change pas la « 1re fois » d'un post plus ancien ; la série est celle de la semaine du post.

Stockage : instantané JSON `Post.highlights` ({v, cuisine, items}) calculé paresseusement à la première
lecture (feed : en lot, nombre de requêtes constant quel que soit le nombre d'auteurs), puis lu tel quel
(0 requête). Remis à null quand le contenu du repas change (invités, recettes, mode cuisine) ; le
recalcul reste « à la date du post ». `SNAPSHOT_VERSION` force le recalcul après un changement de règles.
"""

from __future__ import annotations

import logging
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.db.models import Max, Q
from django.utils import timezone

from recipes.models import (
    CookingProgress,
    MealInvitation,
    MealPlan,
    MealPlanRecipeBatch,
    Post,
    Recipe,
    RecipeBatch,
    RecipeIngredient,
)
from recipes.services.cooked_service import cooked_meal_plans_of_users, join_titles_fr

from ..cuisines import cuisine_payload
from .stats_service import (
    MAX_ANON_GUESTS_PER_MEAL,
    MAX_MINUTES_PER_MEAL,
    Meal,
    _ingredient_info,
    _week_monday,
    household_member_ids,
)

logger = logging.getLogger(__name__)

SNAPSHOT_VERSION = 1
MAX_HIGHLIGHTS = 3
MIN_PLANTS = 3  # « 2 végétaux » n'est pas un fait marquant : seuil pour rester positif
MIN_STREAK_WEEKS = 2
_NO_ID = float('inf')  # repas pas encore créé (aperçu) / post sans repas : après tous ceux du même jour

# Liste fermée : (icône, rang de base). L'ordre de déclaration départage les ex æquo.
KINDS: dict[str, tuple[str, int]] = {
    'first_time': ('sparkles', 100),
    'new_ingredients': ('sprout', 90),
    'streak': ('flame', 50),  # 80 quand la série est un palier (cf. is_streak_milestone)
    'nth_time_quarter': ('repeat', 60),
    'nth_time': ('repeat', 45),
    'plants': ('leaf', 40),
    'guests': ('users', 30),
    'duration': ('timer', 20),
}
_KIND_ORDER = {kind: i for i, kind in enumerate(KINDS)}
STREAK_MILESTONE_RANK = 80


# ---------------------------------------------------------------------------
# Libellés (français : ordinaux et pluriels)
# ---------------------------------------------------------------------------
def ordinal_fr(n: int, *, feminine: bool = True) -> str:
    """1re / 1er, 2e, 3e… (abréviations recommandées : « 1re fois », « 2e semaine »)."""
    if n == 1:
        return '1re' if feminine else '1er'
    return f'{n}e'


def count_fr(n: int, singular: str, plural: str) -> str:
    return f'{n} {singular if n <= 1 else plural}'


def duration_fr(minutes: int) -> str:
    if minutes < 60:
        return f'{minutes} min'
    hours, rest = divmod(minutes, 60)
    return f'{hours} h' if not rest else f'{hours} h {rest:02d}'


_LABELS = {
    'first_time': lambda n: '1re fois',
    'nth_time': lambda n: f'{ordinal_fr(n)} fois',
    'nth_time_quarter': lambda n: f'{ordinal_fr(n)} fois ce trimestre',
    'new_ingredients': lambda n: count_fr(n, 'nouvel ingrédient', 'nouveaux ingrédients'),
    'plants': lambda n: count_fr(n, 'végétal', 'végétaux'),
    'guests': lambda n: count_fr(n, 'convive', 'convives'),
    'duration': duration_fr,
    'streak': lambda n: f"{ordinal_fr(n)} semaine d'affilée",
}


def make_highlight(kind: str, value: int) -> dict:
    return {'kind': kind, 'value': int(value), 'label': _LABELS[kind](int(value)), 'icon': KINDS[kind][0]}


# ---------------------------------------------------------------------------
# Classement (déterministe) : rareté d'abord, puis végétaux / convives / durée
# ---------------------------------------------------------------------------
def is_streak_milestone(weeks: int) -> bool:
    return weeks in (3, 5) or weeks == 52 or (weeks >= 10 and weeks % 5 == 0)


def _rank(highlight: dict) -> int:
    if highlight['kind'] == 'streak' and is_streak_milestone(highlight['value']):
        return STREAK_MILESTONE_RANK
    return KINDS[highlight['kind']][1]


def rank_highlights(candidates: list[dict]) -> list[dict]:
    """Tous les candidats, du plus marquant au moins marquant (l'API n'en montre que MAX_HIGHLIGHTS)."""
    return sorted(candidates, key=lambda h: (-_rank(h), _KIND_ORDER[h['kind']]))


# ---------------------------------------------------------------------------
# Calcul « à la date du repas »
# ---------------------------------------------------------------------------
def _quarter_start(d: date) -> date:
    return date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)


def _main_cuisine(meal: Meal) -> dict | None:
    codes = [r['cuisine'] for r in meal.recipes if r.get('cuisine') and r['cuisine'] != 'autre']
    if not codes:
        return None
    counts = Counter(codes)
    code = max(counts, key=lambda c: (counts[c], -codes.index(c)))
    return cuisine_payload(code)


@dataclass
class _Target:
    user_id: int
    meal: Meal
    order: tuple  # (date, id) : l'historique pris en compte est strictement avant


class _Context:
    """Données chargées en lot (nombre de requêtes constant) pour calculer plusieurs ardoises."""

    def __init__(self, user_ids, end: date | None = None, plan_ids=(), recipe_ids=()):
        self.user_ids = set(user_ids)
        plan_ids = {pid for pid in plan_ids if pid}
        plans = cooked_meal_plans_of_users(self.user_ids, end=end)
        if plan_ids:
            plans = plans | MealPlan.objects.filter(id__in=plan_ids)
        self.meals: dict[int, Meal] = {}
        self.owner: dict[int, int] = {}
        self.history: dict[int, list[Meal]] = defaultdict(list)  # confirmés, triés (date, id)
        for row in plans.order_by('date', 'id').values(
            'id', 'user_id', 'date', 'custom_label', 'guest_count', 'confirmed'
        ):
            meal = Meal(row['id'], row['date'], row['custom_label'] or '', row['guest_count'] or 0)
            self.meals[meal.id] = meal
            self.owner[meal.id] = row['user_id']
            if row['confirmed'] and row['user_id'] in self.user_ids and (end is None or row['date'] <= end):
                self.history[row['user_id']].append(meal)
        ids = list(self.meals)
        if ids:
            for row in (
                MealPlanRecipeBatch.objects.filter(meal_plan_id__in=ids)
                .order_by('order', 'id')
                .values(
                    'meal_plan_id',
                    'recipe_batch_id',
                    'recipe_batch__recipe_id',
                    'recipe_batch__recipe__title',
                    'recipe_batch__recipe__cuisine',
                )
            ):
                meal = self.meals[row['meal_plan_id']]
                meal.batch_ids.append(row['recipe_batch_id'])
                if row['recipe_batch__recipe_id']:
                    meal.recipes.append(
                        {
                            'id': row['recipe_batch__recipe_id'],
                            'title': row['recipe_batch__recipe__title'] or '',
                            'cuisine': row['recipe_batch__recipe__cuisine'],
                        }
                    )
            for row in (
                Post.objects.filter(meal_plan_id__in=ids).order_by('id').values('meal_plan_id', 'user_id', 'label')
            ):
                if row['user_id'] == self.owner[row['meal_plan_id']]:
                    self.meals[row['meal_plan_id']].posts.append({'label': row['label']})
        self._orders = {uid: [(m.date, m.id) for m in meals] for uid, meals in self.history.items()}
        self._dish_keys = {m.id: m.dish_key for m in self.meals.values()}

        all_recipe_ids = {r['id'] for m in self.meals.values() for r in m.recipes} | set(recipe_ids)
        self.ingredients: dict[int, list[tuple[int, dict]]] = defaultdict(list)
        if all_recipe_ids:
            for row in RecipeIngredient.objects.filter(recipe_id__in=all_recipe_ids).values(
                'recipe_id',
                'ingredient_id',
                'ingredient__name',
                'ingredient__profile__food_group',
                'ingredient__profile__is_plant',
                'ingredient__profile__is_basic_staple',
            ):
                self.ingredients[row['recipe_id']].append((row['ingredient_id'], _ingredient_info(row)))

        self.invitees: dict[int, set[int]] = defaultdict(set)
        if plan_ids:
            for row in MealInvitation.objects.filter(meal_plan_id__in=plan_ids, status='accepted').values(
                'meal_plan_id', 'invitee_id'
            ):
                self.invitees[row['meal_plan_id']].add(row['invitee_id'])
        target_batches = {bid for pid in plan_ids if pid in self.meals for bid in self.meals[pid].batch_ids}
        self.minutes: dict[tuple[int, int], int] = {}
        if target_batches:
            self.minutes = {
                (row['user_id'], row['recipe_batch_id']): row['m']
                for row in CookingProgress.objects.filter(
                    user_id__in=self.user_ids,
                    status='completed',
                    recipe_batch_id__in=target_batches,
                    total_time_minutes__gt=0,
                )
                .values('user_id', 'recipe_batch_id')
                .annotate(m=Max('total_time_minutes'))
            }

    def snapshot(self, target: _Target) -> dict:
        meal, d = target.meal, target.order[0]
        history = self.history.get(target.user_id, [])
        prior = [
            m for m in history[: bisect_left(self._orders.get(target.user_id, []), target.order)] if m.id != meal.id
        ]
        candidates: list[dict] = []

        # 1re fois / ne fois (plats-jours, même clé de plat que le plat star)
        key = meal.dish_key
        if key[0] != 'm':
            dates = {m.date for m in prior if self._dish_keys[m.id] == key}
            if not dates - {d}:  # même plat déjà cuisiné le même jour : même « plat-jour »
                candidates.append(make_highlight('first_time', 1))
            else:
                dates.add(d)
                in_quarter = sum(1 for x in dates if _quarter_start(d) <= x <= d)
                if in_quarter >= 2:
                    candidates.append(make_highlight('nth_time_quarter', in_quarter))
                else:
                    candidates.append(make_highlight('nth_time', len(dates)))

        # Ingrédients (hors ingrédients de base) : nouveaux et végétaux
        own: dict[int, dict] = {}
        for recipe in meal.recipes:
            for iid, info in self.ingredients.get(recipe['id'], ()):
                if not info['staple']:
                    own[iid] = info
        if own:
            seen = {iid for m in prior for r in m.recipes for iid, _ in self.ingredients.get(r['id'], ())}
            new = len(set(own) - seen)
            if new:
                candidates.append(make_highlight('new_ingredients', new))
            plants = sum(1 for info in own.values() if info['plant'])
            if plants >= MIN_PLANTS:
                candidates.append(make_highlight('plants', plants))

        # Semaines d'affilée jusqu'à la semaine du repas (incluse)
        mondays = {_week_monday(m.date) for m in prior}
        anchor, weeks = _week_monday(d), 1
        while anchor - timedelta(days=7) in mondays:
            weeks += 1
            anchor -= timedelta(days=7)
        if weeks >= MIN_STREAK_WEEKS:
            candidates.append(make_highlight('streak', weeks))

        # Convives (hors cuisinier et foyer) et durée mesurée par le mode cuisine
        if meal.id in self.meals:
            excluded = household_member_ids(get_user_model()(id=target.user_id)) | {target.user_id}
            guests = len(self.invitees.get(meal.id, set()) - excluded)
            guests += min(max(meal.guest_count, 0), MAX_ANON_GUESTS_PER_MEAL)
            if guests:
                candidates.append(make_highlight('guests', guests))
            minutes = sum(self.minutes.get((target.user_id, bid), 0) for bid in meal.batch_ids)
            minutes = min(minutes, MAX_MINUTES_PER_MEAL)
            if minutes > 0:
                candidates.append(make_highlight('duration', minutes))

        return {'v': SNAPSHOT_VERSION, 'cuisine': _main_cuisine(meal), 'items': rank_highlights(candidates)}


# ---------------------------------------------------------------------------
# Posts : instantané paresseux
# ---------------------------------------------------------------------------
def is_fresh(snapshot) -> bool:
    return isinstance(snapshot, dict) and snapshot.get('v') == SNAPSHOT_VERSION


def compute_post_snapshots(posts: list[Post]) -> dict[int, dict]:
    """Ardoises de plusieurs posts (tous auteurs confondus) en un nombre constant de requêtes (≤ 7)."""
    posts = [p for p in posts if p is not None and p.pk]
    if not posts:
        return {}
    legacy_batch_ids = {p.recipe_batch_id for p in posts if not p.meal_plan_id and p.recipe_batch_id}
    legacy_recipes = {}
    if legacy_batch_ids:
        legacy_recipes = {
            row['id']: {'id': row['recipe_id'], 'title': row['recipe__title'] or '', 'cuisine': row['recipe__cuisine']}
            for row in RecipeBatch.objects.filter(id__in=legacy_batch_ids).values(
                'id', 'recipe_id', 'recipe__title', 'recipe__cuisine'
            )
        }
    # Pas de borne de fin : l'historique de chaque post est coupé à sa date en mémoire (bisect).
    ctx = _Context(
        {p.user_id for p in posts},
        plan_ids={p.meal_plan_id for p in posts if p.meal_plan_id},
        recipe_ids={r['id'] for r in legacy_recipes.values()},
    )
    result = {}
    for post in posts:
        meal = ctx.meals.get(post.meal_plan_id) if post.meal_plan_id else None
        if meal is not None:
            target = _Target(post.user_id, meal, (meal.date, meal.id))
        else:  # post legacy (batch sans repas) : date de création, après les repas du même jour
            recipe = legacy_recipes.get(post.recipe_batch_id)
            meal = Meal(
                None,  # type: ignore[arg-type]
                timezone.localdate(post.created_at),
                '',
                0,
                recipes=[recipe] if recipe else [],
                posts=[{'label': post.label}],
            )
            target = _Target(post.user_id, meal, (meal.date, _NO_ID))
        result[post.id] = ctx.snapshot(target)
    return result


def ensure_post_highlights(posts) -> None:
    """Calcule (en lot) et enregistre les ardoises manquantes ; ne fait jamais échouer la lecture."""
    missing = [
        p for p in posts if p is not None and not is_fresh(p.highlights) and not getattr(p, '_ardoise_failed', False)
    ]
    if not missing:
        return
    try:
        snapshots = compute_post_snapshots(missing)
        for post in missing:
            post.highlights = snapshots.get(post.id)
        Post.objects.bulk_update([p for p in missing if p.highlights is not None], ['highlights'])
    except Exception:  # pragma: no cover - garde-fou : le feed s'affiche sans ardoise
        logger.exception('ardoise: calcul impossible pour %s posts', len(missing))
        for post in missing:
            post._ardoise_failed = True


def post_ardoise(post: Post) -> dict:
    """{'cuisine', 'highlights'} pour l'API (≤ MAX_HIGHLIGHTS puces)."""
    ensure_post_highlights([post])
    snapshot = post.highlights if is_fresh(post.highlights) else {}
    return {
        'cuisine': snapshot.get('cuisine'),
        'highlights': list((snapshot.get('items') or [])[:MAX_HIGHLIGHTS]),
    }


def invalidate_post_highlights(*, meal_plan_ids=(), recipe_batch_id: int | None = None) -> int:
    """Le contenu du repas a changé (invités, recettes, mode cuisine) : recalcul à la prochaine lecture."""
    q = Q()
    ids = [i for i in meal_plan_ids if i]
    if ids:
        q |= Q(meal_plan_id__in=ids)
    if recipe_batch_id:
        q |= Q(recipe_batch_id=recipe_batch_id) | Q(
            meal_plan_id__in=MealPlanRecipeBatch.objects.filter(recipe_batch_id=recipe_batch_id).values('meal_plan_id')
        )
    if not q:
        return 0
    return Post.objects.filter(q, highlights__isnull=False).update(highlights=None)


# ---------------------------------------------------------------------------
# Aperçu « Ça donnera ça » (feuille « J'ai cuisiné », rien n'est créé)
# ---------------------------------------------------------------------------
def preview_cooked(user, data) -> dict:
    """
    Même corps que POST /posts/cooked/ ; renvoie {display_title, cuisine, highlights} sans rien écrire.
    Lève `CookedError` comme `log_cooked` (recette/repas introuvable, repas d'un autre, plat manquant).
    """
    from recipes.services.cooked_service import (
        LABEL_MAX_LENGTH,
        CookedError,
        _parse_int,
        _resolve_local_datetime,
    )
    from recipes.utils import get_accessible_meal_plan_filter

    label = (data.get('label') or '').strip()[:LABEL_MAX_LENGTH]
    recipe = None
    rid = _parse_int(data.get('recipe_id'), 'recipe_id')
    if rid is not None:
        recipe = Recipe.objects.filter(id=rid).only('id', 'title', 'cuisine').first()
        if not recipe:
            raise CookedError(f'Recipe {rid} not found', 404, 'recipe_not_found')
    meal_plan = None
    mid = _parse_int(data.get('meal_plan_id'), 'meal_plan_id')
    if mid is not None:
        meal_plan = MealPlan.objects.filter(get_accessible_meal_plan_filter(user), id=mid).first()
        if not meal_plan:
            raise CookedError('Meal plan not found', 404, 'meal_plan_not_found')
        if meal_plan.user_id != user.id:
            raise CookedError('Only the meal plan owner can log it as cooked', 403, 'not_meal_plan_owner')
    if not (recipe or label or meal_plan):
        raise CookedError('recipe_id, label or meal_plan_id is required', 400, 'missing_dish')
    day = meal_plan.date if meal_plan else _resolve_local_datetime(data.get('cooked_at')).date()

    ctx = _Context(
        {user.id}, day, plan_ids={meal_plan.id} if meal_plan else (), recipe_ids={recipe.id} if recipe else ()
    )
    if meal_plan:
        base = ctx.meals[meal_plan.id]
        meal = replace(base, recipes=list(base.recipes), posts=[{'label': label}] if label else list(base.posts))
        order = (day, meal_plan.id)
    else:
        meal = Meal(None, day, label, 0, posts=[{'label': label}])  # type: ignore[arg-type]
        order = (day, _NO_ID)
    if recipe and recipe.id not in {r['id'] for r in meal.recipes}:
        meal.recipes.append({'id': recipe.id, 'title': recipe.title or '', 'cuisine': recipe.cuisine})

    snapshot = ctx.snapshot(_Target(user.id, meal, order))
    titles = []
    for r in meal.recipes:
        if r['title'] and r['title'] not in titles:
            titles.append(r['title'])
    display_title = label or join_titles_fr(titles) or (meal.custom_label or '').strip() or None
    return {
        'display_title': display_title,
        'cuisine': snapshot['cuisine'],
        'highlights': snapshot['items'][:MAX_HIGHLIGHTS],
    }
