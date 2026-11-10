"""
« Complète ma semaine » (wk02) : le contexte est calculé ici, côté serveur, et les propositions se valident
d'un tap dans l'app. Sync et testable sans LLM ; wrappers async dans `async_tools.py`.

- `build_week_context(user, week=None)` → `WeekContextResult` (tool `get_week_context`, lecture seule) :
  créneaux libres de la semaine (à partir de maintenant), ce qui est déjà prévu et les envies sans date,
  la semaine dernière (à ne pas répéter ; envies pas faites à reproposer), l'historique (plats préférés,
  rythme, cuisines récentes), son carnet (recettes à soi ou de ses collections pas faites récemment),
  convives et contraintes (préférences, foyer, cuisine). Requêtes bornées, aucune écriture.
- `propose_week_meals_for_user(user, meals, week=None)` → `WeekMealProposalsResult` (carte
  `week_meal_proposals`) : **rien n'est écrit**. Chaque plat validé (recette accessible, sans conflit avec
  les préférences / allergies du foyer, créneau dans la semaine) porte ses boutons `CardAction` que l'app
  rejoue au tap : « Jeu. soir » (`schedule` : POST /api/basket/ avec date + meal_time, ou
  POST /api/basket/{id}/schedule/ pour une envie déjà au panier) et « Sans date » (`add_to_week`).
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date as date_type
from datetime import timedelta

from django.contrib.auth.models import AbstractBaseUser
from django.db.models import Prefetch, Q

from chat.services.tool_schemas import (
    CardAction,
    SkippedDish,
    WeekBasketEntry,
    WeekBookRecipe,
    WeekContextResult,
    WeekFavouriteDish,
    WeekMealEntry,
    WeekMealProposal,
    WeekMealProposalsResult,
    WeekSlot,
)

WEEKDAY_SHORT = ('lun.', 'mar.', 'mer.', 'jeu.', 'ven.', 'sam.', 'dim.')
SLOT_WORD = {'lunch': 'midi', 'dinner': 'soir'}
# Un créneau du jour reste « libre » jusqu'à cette heure locale (après, trop tard pour le prévoir).
SLOT_CUTOFF_HOUR = {'lunch': 12, 'dinner': 20}
EATING_OUT_TYPES = ('cantine', 'takeaway')

HISTORY_DAYS = 180
RHYTHM_WEEKS = 8
CUISINE_DAYS = 30
BOOK_REST_DAYS = 21  # une recette du carnet faite il y a moins de 3 semaines n'est pas reproposée
LUNCH_HABIT_DAYS = 28
LUNCH_HABIT_MIN = 3  # ≥ 3 déjeuners prévus en 4 semaines → on compte aussi les midis
MAX_FAVOURITES = 8
MAX_BOOK = 8
MAX_LIST = 14
MAX_PROPOSALS = 10
DEFAULT_TARGET = 3
MAX_TARGET = 7


def slot_label(day: date_type, meal_time: str | None) -> str:
    """« jeu. 12 soir » (créneau standard) ou « jeu. 12 »."""
    base = f'{WEEKDAY_SHORT[day.weekday()]} {day.day}'
    word = SLOT_WORD.get(meal_time or '')
    return f'{base} {word}' if word else base


def slot_button_label(day: date_type, meal_time: str | None) -> str:
    """Bouton de la carte : « Jeu. soir »."""
    text = WEEKDAY_SHORT[day.weekday()].capitalize()
    word = SLOT_WORD.get(meal_time or '')
    return f'{text} {word}' if word else f'{text} {day.day}'


def _short(text: str | None, limit: int = 60) -> str:
    text = ' '.join((text or '').split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + '…'


def _local_now(user):
    from notifications.services import get_preferences, local_now

    return local_now(get_preferences(user))


# ---------------------------------------------------------------------------
# Lecture des repas (titres, identité du plat)
# ---------------------------------------------------------------------------
def _plans_queryset(user, start: date_type, end: date_type):
    from recipes.models import MealPlan, MealPlanRecipeBatch

    return (
        MealPlan.objects.filter(user=user, date__gte=start, date__lte=end)
        .order_by('date', 'meal_time', 'id')
        .prefetch_related(
            Prefetch(
                'meal_plan_recipe_batches',
                queryset=MealPlanRecipeBatch.objects.select_related('recipe_batch__recipe').order_by('order'),
            ),
            'posts',
        )
    )


def _plan_recipes(plan) -> list:
    return [m.recipe_batch.recipe for m in plan.meal_plan_recipe_batches.all() if m.recipe_batch.recipe_id]


def _plan_label(plan) -> str:
    labels = [p.label for p in plan.posts.all() if (p.label or '').strip()]
    return (labels[0] if labels else plan.custom_label or '').strip()


def _plan_title(plan) -> str:
    recipes = _plan_recipes(plan)
    if recipes:
        return ' + '.join(r.title for r in recipes[:2])
    label = _plan_label(plan)
    if label:
        return label
    if plan.meal_type == 'cantine':
        return 'Cantine'
    if plan.meal_type == 'takeaway':
        return 'À emporter / dehors'
    return ''


def _plan_dishes(plan) -> list[tuple[str, str, object]]:
    """[(dish_key, titre, recette | None)] d'un repas (identité `dish_service.dish_key`)."""
    from recipes.services.dish_service import dish_key

    recipes = _plan_recipes(plan)
    if recipes:
        return [(dish_key(recipe_id=r.id), r.title, r) for r in recipes]
    label = _plan_label(plan)
    key = dish_key(label=label) if label else None
    return [(key, label, None)] if key else []


def _plan_fills_slot(plan) -> bool:
    """Un repas occupe son créneau s'il a un plat, un libellé ou si on mange dehors (cantine, à emporter)."""
    if plan.meal_type in EATING_OUT_TYPES or plan.confirmed:
        return True
    return bool(_plan_recipes(plan) or (plan.custom_label or '').strip())


def _cuisine_label(recipe) -> str:
    from stats.cuisines import CUISINES_BY_CODE

    cuisine = CUISINES_BY_CODE.get(getattr(recipe, 'cuisine', None) or '') if recipe is not None else None
    if cuisine is None or cuisine.code in ('autre', 'fusion'):
        return ''
    return cuisine.label.lower()


# ---------------------------------------------------------------------------
# Historique « cuisiné » (source unique : cooked_service.cooked_meal_plans)
# ---------------------------------------------------------------------------
@dataclass
class _DishStat:
    key: str
    title: str
    recipe_id: int | None = None
    cuisine: str = ''
    days: set = field(default_factory=set)

    @property
    def last(self) -> date_type:
        return max(self.days)


def _cooked_history(user, today: date_type) -> tuple[dict[str, _DishStat], list]:
    from recipes.models import MealPlanRecipeBatch
    from recipes.services.cooked_service import cooked_meal_plans

    plans = list(
        cooked_meal_plans(user, today - timedelta(days=HISTORY_DAYS), today)
        .order_by('-date', '-id')
        .prefetch_related(
            Prefetch(
                'meal_plan_recipe_batches',
                queryset=MealPlanRecipeBatch.objects.select_related('recipe_batch__recipe').order_by('order'),
            ),
            'posts',
        )
    )
    stats: dict[str, _DishStat] = {}
    for plan in plans:
        for key, title, recipe in _plan_dishes(plan):
            stat = stats.get(key)
            if stat is None:
                stat = stats[key] = _DishStat(
                    key=key,
                    title=title,
                    recipe_id=recipe.id if recipe is not None else None,
                    cuisine=_cuisine_label(recipe),
                )
            stat.days.add(plan.date)
    return stats, plans


def _favourites(stats: dict[str, _DishStat], today: date_type) -> list[WeekFavouriteDish]:
    ranked = sorted(
        (s for s in stats.values() if len(s.days) >= 2),
        key=lambda s: (-len(s.days), -s.last.toordinal()),
    )
    return [
        WeekFavouriteDish(
            title=_short(s.title),
            recipe_id=s.recipe_id,
            times=len(s.days),
            last_cooked=s.last.isoformat(),
            days_ago=(today - s.last).days,
            cuisine=s.cuisine,
        )
        for s in ranked[:MAX_FAVOURITES]
    ]


def _cooked_per_week(plans: list, today: date_type) -> float:
    since = today - timedelta(weeks=RHYTHM_WEEKS)
    days = {p.date for p in plans if p.date > since}
    return round(len(days) / RHYTHM_WEEKS, 1)


def _recent_cuisines(stats: dict[str, _DishStat], today: date_type) -> list[str]:
    since = today - timedelta(days=CUISINE_DAYS)
    counts: Counter = Counter()
    for s in stats.values():
        if s.cuisine:
            n = sum(1 for d in s.days if d > since)
            if n:
                counts[s.cuisine] += n
    return [f'{label} ×{n}' for label, n in counts.most_common(6)]


# ---------------------------------------------------------------------------
# Carnet : ses recettes (créées / importées / adaptées) + celles de ses collections
# ---------------------------------------------------------------------------
def _book_recipes(user, stats: dict[str, _DishStat], today: date_type, exclude_ids: set[int]) -> list[WeekBookRecipe]:
    from recipes.dietary_filters import conflict_details_by_recipe_id
    from recipes.models import Recipe
    from recipes.services.dish_service import dish_key

    owned = Q(created_by=user)
    in_collections = Q(collections__owner=user) | Q(collections__members__user=user)
    visible = Q(is_public=True) | Q(created_by=user)
    candidates = list(
        Recipe.objects.filter(owned | (in_collections & visible))
        .exclude(id__in=exclude_ids)
        .distinct()
        .only('id', 'title', 'prep_time', 'cook_time', 'cuisine', 'created_at')
        .order_by('-created_at', '-id')[:60]
    )
    rest_since = today - timedelta(days=BOOK_REST_DAYS)
    fresh = []
    for recipe in candidates:
        stat = stats.get(dish_key(recipe_id=recipe.id))
        if stat is not None and stat.last > rest_since:
            continue
        fresh.append((recipe, stat))
    conflicts = conflict_details_by_recipe_id([r.id for r, _ in fresh], user) if fresh else {}
    # Jamais cuisinées d'abord (à découvrir), puis les plus anciennes ; récentes dans le carnet en tête.
    fresh = [(r, s) for r, s in fresh if r.id not in conflicts]
    fresh.sort(key=lambda rs: (rs[1] is not None, rs[1].last.toordinal() if rs[1] else 0))
    return [
        WeekBookRecipe(
            recipe_id=r.id,
            title=_short(r.title),
            total_time_min=(r.prep_time or 0) + (r.cook_time or 0) or None,
            cuisine=_cuisine_label(r),
            times_cooked=len(s.days) if s else 0,
            last_cooked=s.last.isoformat() if s else None,
        )
        for r, s in fresh[:MAX_BOOK]
    ]


# ---------------------------------------------------------------------------
# Contexte de la semaine (tool get_week_context)
# ---------------------------------------------------------------------------
def _lunch_habit(user, today: date_type) -> bool:
    from recipes.models import MealPlan

    since = today - timedelta(days=LUNCH_HABIT_DAYS)
    plans = (
        MealPlan.objects.filter(user=user, meal_time='lunch', date__gte=since, date__lte=today)
        .exclude(meal_type__in=EATING_OUT_TYPES)
        .filter(Q(meal_plan_recipe_batches__isnull=False) | ~Q(custom_label=''))
        .values('date')
        .distinct()
    )
    return plans.count() >= LUNCH_HABIT_MIN


def _invited_slots(user, start: date_type, end: date_type) -> dict[tuple[date_type, str], str]:
    """Créneaux pris par une invitation acceptée chez un·e ami·e : (date, meal_time) → « chez X »."""
    from recipes.models import MealInvitation

    rows = MealInvitation.objects.filter(
        invitee=user, status='accepted', meal_plan__date__gte=start, meal_plan__date__lte=end
    ).select_related('meal_plan', 'inviter')
    return {(inv.meal_plan.date, inv.meal_plan.meal_time): f'Chez {inv.inviter.username}' for inv in rows}


def _people(user, week_start: date_type) -> int:
    from recipes.models import BasketItem
    from recipes.services import basket_service as basket
    from recipes.services.diners_service import basket_item_composition, context_cache

    guests, excluded = basket.week_composition(user, week_start)
    item = BasketItem(user=user, guest_count=guests, excluded_household_ids=excluded)
    return basket_item_composition(item, context_cache({})).total


def _constraints(user) -> list[str]:
    from chat.services.session_context import _equipment_lines, _household_lines, _preference_lines, _safe

    lines = _safe(_preference_lines, user) + _safe(_household_lines, user) + _safe(_equipment_lines, user)
    return [line[2:] if line.startswith('- ') else line for line in lines]


def _basket_entry(item, *, status: str) -> WeekBasketEntry:
    return WeekBasketEntry(
        basket_item_id=item.id,
        title=_short(item.display_title),
        recipe_id=item.recipe_id,
        status=status,
    )


def build_week_context(user: AbstractBaseUser, week=None) -> WeekContextResult:
    """Tout ce qu'il faut pour proposer des repas sur les créneaux libres de la semaine (lecture seule)."""
    from recipes.services import basket_service as basket

    now = _local_now(user)
    today = now.date()
    week_start = basket.parse_week(week, user)
    week_end = week_start + timedelta(days=6)
    last_start = week_start - timedelta(days=7)

    # Repas de la semaine dernière et de cette semaine (une requête + préchargements).
    plans = list(_plans_queryset(user, last_start, week_end))
    this_week = [p for p in plans if p.date >= week_start]
    last_week = [p for p in plans if p.date < week_start]

    planned = [
        WeekMealEntry(
            date=p.date.isoformat(),
            meal_time=p.meal_time,
            title=_short(_plan_title(p)) or '(créneau vide)',
            cooked=bool(p.confirmed),
            meal_plan_id=p.id,
        )
        for p in this_week
        if _plan_fills_slot(p)
    ][:MAX_LIST]
    last_week_entries = [
        WeekMealEntry(
            date=p.date.isoformat(),
            meal_time=p.meal_time,
            title=_short(_plan_title(p)),
            cooked=bool(p.confirmed),
            meal_plan_id=p.id,
        )
        for p in last_week
        if _plan_fills_slot(p) and _plan_title(p)
    ][:MAX_LIST]

    # Envies : sans date cette semaine (à caler d'abord), pas faites la semaine dernière (à reproposer).
    week_now = basket.current_week_start(user)
    basket_items = basket.week_items(user, week_start)
    unscheduled = [_basket_entry(i, status='todo') for i in basket_items if basket.item_status(i, week_now) == 'todo'][
        :MAX_LIST
    ]
    previous_items = basket.week_items(user, last_start)
    not_done = [
        _basket_entry(i, status='not_done')
        for i in previous_items
        if basket.item_status(i, week_now) in ('not_done', 'todo') and not i.meal_plan_id
    ][:MAX_LIST]

    # Créneaux libres : dîners (et midis si l'habitude existe), d'aujourd'hui (avant l'heure) à dimanche.
    lunches = _lunch_habit(user, today)
    slots_wanted = ('lunch', 'dinner') if lunches else ('dinner',)
    taken = {(p.date, p.meal_time) for p in this_week if _plan_fills_slot(p)}
    invited = _invited_slots(user, week_start, week_end)
    empty_slots = []
    day = max(today, week_start)
    while day <= week_end:
        for meal_time in slots_wanted:
            if day == today and now.hour >= SLOT_CUTOFF_HOUR[meal_time]:
                continue
            if (day, meal_time) in taken or (day, meal_time) in invited:
                continue
            empty_slots.append(
                WeekSlot(
                    date=day.isoformat(),
                    meal_time=meal_time,
                    label=slot_label(day, meal_time),
                )
            )
        day += timedelta(days=1)

    # Historique, carnet.
    stats, cooked_plans = _cooked_history(user, today)
    favourites = _favourites(stats, today)
    exclude_ids = {r.id for p in this_week for r in _plan_recipes(p)} | {
        i.recipe_id for i in basket_items if i.recipe_id
    }
    book = _book_recipes(user, stats, today, exclude_ids)
    per_week = _cooked_per_week(cooked_plans, today)

    free_after_basket = max(0, len(empty_slots) - len(unscheduled))
    rhythm = max(DEFAULT_TARGET, math.ceil(per_week)) if per_week else DEFAULT_TARGET
    target = min(free_after_basket, rhythm, MAX_TARGET)

    invited_entries = [
        WeekMealEntry(date=d.isoformat(), meal_time=mt, title=who, cooked=False)
        for (d, mt), who in sorted(invited.items())
    ]

    result = WeekContextResult(
        week_start=week_start.isoformat(),
        week_end=week_end.isoformat(),
        today=today.isoformat(),
        is_past_week=week_end < today,
        people=_people(user, week_start),
        lunches_included=lunches,
        empty_slots=empty_slots,
        planned=planned + invited_entries,
        basket_unscheduled=unscheduled,
        last_week=last_week_entries,
        last_week_not_done=not_done,
        favourites=favourites,
        book=book,
        cooked_per_week=per_week,
        recent_cuisines=_recent_cuisines(stats, today),
        constraints=_constraints(user),
        target_count=target,
    )
    result.message = _context_message(result)
    return result


def _context_message(ctx: WeekContextResult) -> str:
    if ctx.is_past_week:
        return 'Cette semaine est passée : rien à compléter.'
    slots = len(ctx.empty_slots)
    if not slots:
        return 'Ta semaine est déjà pleine.'
    what = 'créneaux libres' if ctx.lunches_included else 'soirs libres'
    parts = [f'{slots} {what}' if slots > 1 else ('1 créneau libre' if ctx.lunches_included else '1 soir libre')]
    if ctx.basket_unscheduled:
        n = len(ctx.basket_unscheduled)
        parts.append(f'{n} envie{"s" if n > 1 else ""} à caler')
    return ', '.join(parts) + '.'


# ---------------------------------------------------------------------------
# Propositions (tool propose_week_meals) : rien n'est écrit, l'app agit au tap
# ---------------------------------------------------------------------------
def _field(meal, name):
    if isinstance(meal, dict):
        return meal.get(name)
    return getattr(meal, name, None)


def _parse_int(value) -> int | None:
    if value in (None, ''):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _resolve_slot(meal, today: date_type, first_day: date_type, week_end: date_type):
    """(date, meal_time) valides dans la semaine (pas dans le passé), sinon (None, None)."""
    from chat.services.kitchen_tools import normalize_meal_time, parse_day

    raw = _field(meal, 'date')
    if not raw:
        return None, None
    try:
        day = parse_day(raw, today)
        meal_time = normalize_meal_time(_field(meal, 'meal_time')) or 'dinner'
    except ValueError:
        return None, None
    if not (first_day <= day <= week_end) or meal_time not in ('breakfast', 'lunch', 'dinner'):
        return None, None
    return day, meal_time


def _source(recipe, stats_keys: set[str], user) -> str:
    from recipes.services.dish_service import dish_key

    if recipe is None:
        return 'nouveau'
    if dish_key(recipe_id=recipe.id) in stats_keys:
        return 'historique'
    if recipe.created_by_id == user.id:
        return 'carnet'
    return 'nouveau'


def propose_week_meals_for_user(user: AbstractBaseUser, meals, week=None) -> WeekMealProposalsResult:
    """
    Carte de propositions pour les créneaux libres. N'écrit rien : chaque plat porte ses boutons (`actions`).
    Plats ignorés (`skipped`) : recette introuvable, envie inconnue, conflit avec ses préférences ou les
    allergies du foyer, doublon. Un créneau hors semaine / passé est retiré (le plat reste ajoutable sans date).
    """
    from chat.services.kitchen_tools import _accessible_recipes, dish_conflict_checker
    from recipes.services import basket_service as basket

    meals = list(meals or [])
    if not meals:
        raise ValueError('Indique au moins un plat (meals).')
    if len(meals) > MAX_PROPOSALS:
        meals = meals[:MAX_PROPOSALS]

    now = _local_now(user)
    today = now.date()
    week_start = basket.parse_week(week, user)
    week_end = week_start + timedelta(days=6)
    first_day = max(today, week_start)

    recipe_ids = [rid for rid in (_parse_int(_field(m, 'recipe_id')) for m in meals) if rid is not None]
    recipes = _accessible_recipes(user, recipe_ids)
    if recipes:
        from recipes.models import Recipe

        recipes = {
            r.id: r
            for r in Recipe.objects.filter(id__in=list(recipes)).only(
                'id', 'title', 'prep_time', 'cook_time', 'image_path', 'created_by_id', 'cuisine'
            )
        }
    item_ids = [iid for iid in (_parse_int(_field(m, 'basket_item_id')) for m in meals) if iid is not None]
    items = {i.id: i for i in basket.basket_queryset(user).filter(id__in=item_ids)} if item_ids else {}
    conflicts_for = dish_conflict_checker(user, list(recipes))
    stats_keys = set(_cooked_history(user, today)[0])

    result = WeekMealProposalsResult(week_start=week_start.isoformat(), week_end=week_end.isoformat())
    seen: set[str] = set()
    used_slots: set[tuple[date_type, str]] = set()
    for index, meal in enumerate(meals):
        reason = _short(str(_field(meal, 'reason') or ''), 80)
        item_id = _parse_int(_field(meal, 'basket_item_id'))
        recipe_id = _parse_int(_field(meal, 'recipe_id'))
        label = _short(str(_field(meal, 'label') or ''), basket.LABEL_MAX_LENGTH)

        item = None
        recipe = None
        if item_id is not None:
            item = items.get(item_id)
            if item is None or (item.meal_plan_id and item.meal_plan.confirmed):
                result.skipped.append(SkippedDish(label=label, reason='invalid', detail='envie introuvable'))
                continue
            recipe = item.recipe if item.recipe_id else None
            title = item.display_title
        elif recipe_id is not None:
            recipe = recipes.get(recipe_id)
            if recipe is None:
                result.skipped.append(SkippedDish(recipe_id=recipe_id, label=label, reason='recipe_not_found'))
                continue
            title = recipe.title
        elif label:
            title = label
        else:
            result.skipped.append(SkippedDish(reason='invalid', detail='plat sans recette ni nom'))
            continue

        dedupe_key = f'b{item.id}' if item else (f'r{recipe.id}' if recipe else f'l{title.lower()}')
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        if item is None:
            conflicts = conflicts_for(recipe.id if recipe else None, title)
            if conflicts:
                reasons = {r for r, _ in conflicts}
                result.skipped.append(
                    SkippedDish(
                        recipe_id=recipe.id if recipe else None,
                        label=title,
                        reason=next(k for k in ('allergy', 'diet', 'dislike') if k in reasons),
                        detail=' ; '.join(d for _, d in conflicts),
                    )
                )
                continue

        day, meal_time = _resolve_slot(meal, today, first_day, week_end)
        if day is not None and (day, meal_time) in used_slots:
            day, meal_time = None, None  # deux plats sur le même créneau : le second reste sans date
        if day is not None:
            used_slots.add((day, meal_time))

        actions: list[CardAction] = []
        dish_body = {'recipe_id': recipe.id} if recipe is not None else {'label': title}
        if day is not None:
            if item is not None:
                actions.append(
                    CardAction(
                        kind='schedule',
                        label=slot_button_label(day, meal_time),
                        method='POST',
                        path=f'/api/basket/{item.id}/schedule/',
                        body={'date': day.isoformat(), 'meal_time': meal_time},
                    )
                )
            else:
                actions.append(
                    CardAction(
                        kind='schedule',
                        label=slot_button_label(day, meal_time),
                        method='POST',
                        path='/api/basket/',
                        body={**dish_body, 'date': day.isoformat(), 'meal_time': meal_time},
                    )
                )
        if item is None:
            actions.append(
                CardAction(
                    kind='add_to_week',
                    label='Sans date',
                    method='POST',
                    path='/api/basket/',
                    body={**dish_body, 'week': week_start.isoformat()},
                )
            )
        if recipe is not None:
            actions.append(CardAction(kind='open_recipe', label='Voir la recette', recipe_id=recipe.id))

        total = ((recipe.prep_time or 0) + (recipe.cook_time or 0)) if recipe is not None else 0
        result.proposals.append(
            WeekMealProposal(
                key=f'{index}-{dedupe_key}',
                title=_short(title, 80),
                recipe_id=recipe.id if recipe is not None else None,
                basket_item_id=item.id if item is not None else None,
                is_free_text=recipe is None,
                image_url=recipe.image_url if recipe is not None else None,
                total_time_min=total or None,
                source='panier' if item is not None else _source(recipe, stats_keys, user),
                reason=reason,
                date=day.isoformat() if day else None,
                meal_time=meal_time,
                slot_label=slot_label(day, meal_time) if day else None,
                actions=actions,
            )
        )

    result.message = _proposals_message(result)
    return result


def _proposals_message(result: WeekMealProposalsResult) -> str:
    n = len(result.proposals)
    if not n:
        base = 'Aucune proposition retenue.'
    else:
        base = f'{n} proposition{"s" if n > 1 else ""} affichée{"s" if n > 1 else ""} : rien n’est encore ajouté, '
        base += 'l’utilisateur valide plat par plat dans la carte.'
    if result.skipped:
        base += ' Ignoré(s) : ' + ', '.join(
            f'{s.label or s.recipe_id} ({s.detail or s.reason})' for s in result.skipped
        )
    return base
