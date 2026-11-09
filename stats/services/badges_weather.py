"""
Badges météo (phase 2, spec badges-quirky « La météo ») : opt-in « Ta ville », données Open-Meteo.

Derrière `WEATHER_BADGES_ENABLED` (masqués et jamais évalués sinon) et `async_only` : la météo vient du
réseau, ces badges ne sont évalués que dans la tâche Celery `evaluate_user_badges` (après `refresh_user_weather`),
jamais dans une requête HTTP. Température max / pluie du jour, à la ville déclarée (`stats.services.weather`).
"""

from __future__ import annotations

from .badge_history import BadgeHistory, DishDay
from .badge_types import TIER_LEGENDAIRE, TIER_RARE, TIER_TRES_RARE, BadgeDef, once
from .badges_quirky import BBQ, RACLETTE, SOUPE, SOUPE_FROIDE, _h, _rx

GLACE = _rx(
    r'\b(glaces?|sorbets?|esquimaux?|granites?|creme glacee|nougat glace|parfait glace|vacherin glace|'
    r'omelette norvegienne|frozen yogurt|affogato|coupe glacee|banana split|semifreddo)\b'
)
OVEN_WORDS = _rx(r'\b(au four|gratins?|rotis?|lasagnes?|moussaka|clafoutis|quiches?|tartes?|gateaux?|cakes?)\b')


def _weather(predicate):
    """Premier plat-jour dont la météo (cache) satisfait `predicate(dd, temperature_max, pluie_mm)`."""

    def fn(h: BadgeHistory):
        weather = h.weather
        for dd in h.dish_days:
            w = weather.get(dd.date)
            if w is not None and predicate(dd, w.temperature_max, w.precipitation_mm):
                return once(dd.date, dish=dd.label, temperature_max=w.temperature_max)
        return 0, None

    return _h(fn)


def _hot(threshold: float):
    return lambda t: t is not None and t >= threshold


def _uses_oven(dd: DishDay) -> bool:
    return 'four' in dd.equipment or dd.matches(OVEN_WORDS)


def _wb(code, title, emoji, description, phrase, metric, rarity=TIER_RARE, hint=''):
    return BadgeDef(
        code=code,
        title=title,
        emoji=emoji,
        description=description,
        category='meteo',
        target=1,
        metric=metric,
        family='meteo',
        phrase=phrase,
        secret=bool(hint),
        hint=hint,
        rarity_guess=rarity,
        feature_flag='WEATHER_BADGES_ENABLED',
        async_only=True,
    )


WEATHER_BADGES: tuple[BadgeDef, ...] = (
    _wb('four_canicule', 'Four par canicule', '🥵', 'Four allumé par 33 °C ou plus.', 'Tu as chaud ? Nous aussi.',
        _weather(lambda dd, t, _: _hot(33)(t) and _uses_oven(dd)), TIER_TRES_RARE),
    _wb('soupe_canicule', 'Soupe brûlante', '🍜', 'Une soupe chaude par 30 °C ou plus.',
        'Il paraît que ça rafraîchit. Il paraît.',
        _weather(lambda dd, t, _: _hot(30)(t) and dd.matches(SOUPE) and not dd.matches(SOUPE_FROIDE)),
        TIER_TRES_RARE, hint='Un bol fumant quand le thermomètre s’affole.'),
    _wb('glace_gel', 'Glace sous zéro', '🧊', 'Un dessert glacé par 0 °C ou moins.',
        'Ton congélateur, c’est dehors.',
        _weather(lambda dd, t, _: t is not None and t <= 0 and dd.matches(GLACE)), TIER_TRES_RARE),
    _wb('bbq_pluie', 'Barbecue sous la pluie', '☔', 'Barbecue ou plancha un jour de pluie.',
        'Le parapluie, meilleur ustensile de l’été.',
        _weather(lambda dd, _, rain: (rain or 0) >= 1 and (dd.matches(BBQ) or 'barbecue_plancha' in dd.equipment)),
        TIER_RARE, hint='Les braises n’ont pas peur de l’eau.'),
    _wb('raclette_chaleur', 'Raclette à 30 °C', '🔥', 'Raclette, fondue ou tartiflette par 30 °C ou plus.',
        'Légende vivante.', _weather(lambda dd, t, _: _hot(30)(t) and dd.matches(RACLETTE)), TIER_LEGENDAIRE),
)  # fmt: skip
