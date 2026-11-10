"""
Historique « cuisiné » préparé pour les badges loufoques (`badges_quirky.py`).

Construit **une fois par évaluation** à partir du `StatsResult` déjà calculé (`compute_stats(user, 'all')` :
repas, plats-jours, ingrédients, convives — aucune requête de plus pour les relire), puis complété par
quelques lectures paresseuses, chacune en une requête et au plus une fois :

- fuseau de l'utilisateur (`NotificationPreference.timezone`) : heure locale de la saisie du « cuisiné » ;
- durées mesurées du mode cuisine (`CookingProgress` terminés) ;
- « À mon tour » : posts d'autres cuisiniers inspirés par les siens ;
- invitations : amis inscrits grâce à son lien (`InviteRedemption.is_acquisition`) ;
- tablées : membres acceptés + dates « cuisiné » des autres membres (2 requêtes) ;
- cuisine « Ta cuisine » (0 ou 1 requête).

Nombre de requêtes constant, quel que soit l'historique : tout le reste est de l'agrégation Python.
Règles douces : un même plat compte une fois par jour (plats-jours de `compute_stats`).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from functools import cached_property
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings

from ..badge_colors import colour_of
from ..cuisines import CUISINES_BY_CODE, NON_COLLECTIBLE
from ..text import fold
from .stats_service import MAX_ANON_GUESTS_PER_MEAL, StatsResult

MEAT_GROUPS = frozenset({'viandes', 'poissons_fruits_de_mer'})


@dataclass
class Ingredient:
    id: int
    name: str  # plié
    group: str | None
    plant: bool
    staple: bool

    @cached_property
    def colour(self) -> str | None:
        return colour_of(self.name)


@dataclass
class DishDay:
    """Un plat-jour (un même plat compte une fois par jour), avec tout ce qu'il faut pour les badges."""

    date: date
    key: tuple
    label: str
    text: str  # libellé + titres des recettes, pliés (détection par mots-clés)
    meal_id: int
    meal_time: str
    scheduled_time: time | None
    log_local: datetime | None  # première saisie (post) dans le fuseau de l'utilisateur
    photo: bool
    people: int  # cuisinier + convives hors foyer (invités avec compte + anonymes plafonnés)
    recipes: list[dict] = field(default_factory=list)
    ingredients: dict[int, Ingredient] = field(default_factory=dict)
    recipe_sizes: list[int] = field(default_factory=list)  # nombre d'ingrédients de chaque recette
    equipment: frozenset[str] = frozenset()
    cuisines: frozenset[str] = frozenset()  # cuisines « explorables » (hors fusion / autre)
    made_sub_recipes: int = 0  # lignes à sous-recette faites maison
    inspired_by_user_ids: frozenset[int] = frozenset()  # « À mon tour » : auteurs des posts d'origine
    inspired_by_post_ids: frozenset[int] = frozenset()

    @property
    def has_recipe(self) -> bool:
        return bool(self.recipes)

    @property
    def is_imported(self) -> bool:
        return any(
            bool(r.get('import_source_url')) or r.get('source_type') in ('url', 'photo', 'text', 'import')
            for r in self.recipes
        )

    def matches(self, pattern: re.Pattern) -> bool:
        return bool(pattern.search(self.text))

    def has_ingredient(self, pattern: re.Pattern) -> bool:
        return any(pattern.search(i.name) for i in self.ingredients.values())

    @property
    def groups(self) -> set[str]:
        return {i.group for i in self.ingredients.values() if i.group}


def user_zone(user_id: int) -> ZoneInfo:
    from notifications.models import NotificationPreference

    name = NotificationPreference.objects.filter(user_id=user_id).values_list('timezone', flat=True).first()
    try:
        return ZoneInfo(name or settings.TIME_ZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(settings.TIME_ZONE)


class BadgeHistory:
    def __init__(self, stats: StatsResult):
        self.stats = stats
        self.user = stats.user
        self.user_id = getattr(stats.user, 'id', None)
        self.today = stats.today or date.today()

    # ------------------------------------------------------------------
    # Lectures paresseuses (une requête chacune, au plus une fois)
    # ------------------------------------------------------------------
    @cached_property
    def zone(self) -> ZoneInfo:
        return user_zone(self.user_id)

    @cached_property
    def cooking_minutes_by_batch(self) -> dict[int, list[int]]:
        from recipes.models import CookingProgress

        out: dict[int, list[int]] = defaultdict(list)
        for batch_id, minutes in CookingProgress.objects.filter(
            user_id=self.user_id, status='completed', total_time_minutes__gt=0
        ).values_list('recipe_batch_id', 'total_time_minutes'):
            out[batch_id].append(minutes)
        return out

    @cached_property
    def inspired_others_dates(self) -> list[date]:
        """Dates (locales) des posts d'autres cuisiniers inspirés d'un de ses posts (« À mon tour »)."""
        from recipes.models import Post

        rows = (
            Post.objects.filter(inspired_by_post__user_id=self.user_id)
            .exclude(user_id=self.user_id)
            .order_by('created_at')
            .values_list('created_at', flat=True)
        )
        return [dt.astimezone(self.zone).date() for dt in rows]

    @cached_property
    def invite_join_dates(self) -> list[date]:
        from accounts.models import InviteRedemption

        rows = (
            InviteRedemption.objects.filter(inviter_id=self.user_id, is_acquisition=True)
            .order_by('created_at')
            .values_list('created_at', flat=True)
        )
        return [dt.astimezone(self.zone).date() for dt in rows]

    @cached_property
    def tables(self) -> list[dict]:
        """[{members: {user_id: date d'arrivée}, cooked: {user_id: set(dates)}}] pour ses tablées (2 requêtes)."""
        from recipes.services.cooked_service import cooked_meal_plans_of_users
        from tables.models import TableMember

        mine = TableMember.objects.filter(user_id=self.user_id, status=TableMember.STATUS_ACCEPTED).values('table_id')
        members: dict[int, dict[int, date]] = defaultdict(dict)
        for row in TableMember.objects.filter(table_id__in=mine, status=TableMember.STATUS_ACCEPTED).values(
            'table_id', 'user_id', 'joined_at', 'created_at'
        ):
            joined = row['joined_at'] or row['created_at']
            members[row['table_id']][row['user_id']] = joined.astimezone(self.zone).date()
        if not members:
            return []
        others = {uid for m in members.values() for uid in m} - {self.user_id}
        since = min(d for m in members.values() for d in m.values())
        cooked: dict[int, set[date]] = defaultdict(set)
        cooked[self.user_id] = {m.date for m in self.stats.meals}
        if others:
            for uid, day in cooked_meal_plans_of_users(others, start=since).values_list('user_id', 'date').distinct():
                cooked[uid].add(day)
        return [{'members': m, 'cooked': cooked} for m in members.values()]

    @cached_property
    def weather(self) -> dict:
        """Météo (cache en base, lecture seule) à la ville déclarée, pour les jours cuisinés (1 requête)."""
        from .weather import user_location, weather_by_date

        return weather_by_date(user_location(self.user), {m.date for m in self.stats.meals})

    @cached_property
    def kitchen(self):
        from recipes.services.equipment_service import effective_kitchen

        return effective_kitchen(self.user)

    # ------------------------------------------------------------------
    # Plats-jours enrichis (Python pur, sur les données de compute_stats)
    # ------------------------------------------------------------------
    @cached_property
    def dish_days(self) -> list[DishDay]:
        s = self.stats
        zone = self.zone
        out = []
        for meal in s.dish_days:
            guests = s.guests_by_meal.get(meal.id, ())
            people = 1 + len(guests) + min(max(meal.guest_count, 0), MAX_ANON_GUESTS_PER_MEAL)
            created = [p['created_at'] for p in meal.posts if p.get('created_at')]
            log_local = min(created).astimezone(zone) if created else None
            ingredients: dict[int, Ingredient] = {}
            sizes, equipment, cuisines = [], set(), set()
            made = 0
            for recipe in meal.recipes:
                rows = s.ingredients_by_recipe.get(recipe['id'], ())
                sizes.append(len(rows))
                equipment.update(recipe.get('required_equipment') or ())
                if recipe.get('cuisine') in CUISINES_BY_CODE and recipe['cuisine'] not in NON_COLLECTIBLE:
                    cuisines.add(recipe['cuisine'])
                modes = recipe.get('sub_recipe_modes') or {}
                for row in rows:
                    iid = row['ingredient_id']
                    info = s.ing_info.get(iid)
                    if info and iid not in ingredients:
                        ingredients[iid] = Ingredient(
                            iid, fold(info['name']), info['group'], info['plant'], info['staple']
                        )
                    if row.get('sub_recipe_id') and modes.get(str(row.get('id')), row.get('default_mode')) == 'make':
                        made += 1
            label = meal.label
            text = fold(' '.join([label, *(r['title'] for r in meal.recipes)]))
            out.append(
                DishDay(
                    date=meal.date,
                    key=meal.dish_key,
                    label=label,
                    text=text,
                    meal_id=meal.id,
                    meal_time=meal.meal_time,
                    scheduled_time=meal.scheduled_time,
                    log_local=log_local,
                    photo=any(p.get('n_photos') for p in meal.posts),
                    people=people,
                    recipes=meal.recipes,
                    ingredients=ingredients,
                    recipe_sizes=sizes,
                    equipment=frozenset(equipment),
                    cuisines=frozenset(cuisines),
                    made_sub_recipes=made,
                    inspired_by_user_ids=frozenset(
                        p['inspired_by_post__user_id']
                        for p in meal.posts
                        if p.get('inspired_by_post__user_id') and p['inspired_by_post__user_id'] != self.user_id
                    ),
                    inspired_by_post_ids=frozenset(
                        p['inspired_by_post_id']
                        for p in meal.posts
                        if p.get('inspired_by_post_id') and p.get('inspired_by_post__user_id') != self.user_id
                    ),
                )
            )
        return out

    @cached_property
    def dates_by_dish(self) -> dict[tuple, list[DishDay]]:
        """Plats identifiés (recette ou libellé) → plats-jours triés. Les plats anonymes sont exclus."""
        out: dict[tuple, list[DishDay]] = defaultdict(list)
        for dd in self.dish_days:
            if dd.key[0] != 'm':
                out[dd.key].append(dd)
        return out

    @cached_property
    def cooked_days(self) -> list[date]:
        return sorted({m.date for m in self.stats.meals})

    @cached_property
    def slots_by_day(self) -> dict[date, set[str]]:
        out: dict[date, set[str]] = defaultdict(set)
        for meal in self.stats.meals:
            if meal.meal_time:
                out[meal.date].add(meal.meal_time)
        return out

    def measured_minutes(self, dd: DishDay) -> list[int]:
        """Durées mesurées par le mode cuisine pour les préparations de ce plat-jour."""
        by_batch = self.cooking_minutes_by_batch
        return [m for r in dd.recipes for m in by_batch.get(r.get('batch_id'), ())]

    def where(self, predicate) -> list[date]:
        """Dates (triées) des plats-jours qui satisfont `predicate`."""
        return [dd.date for dd in self.dish_days if predicate(dd)]


def history_of(stats: StatsResult) -> BadgeHistory:
    """Historique préparé, mémorisé sur le `StatsResult` (une seule construction par évaluation)."""
    if stats.history is None:
        stats.history = BadgeHistory(stats)
    return stats.history


def week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())
