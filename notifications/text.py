"""Petits helpers de rédaction (français) partagés par les modèles de messages du registre."""

from __future__ import annotations

from datetime import date

NBSP = ' '
DAYS_FR = ('lundi', 'mardi', 'mercredi', 'jeudi', 'vendredi', 'samedi', 'dimanche')
MONTHS_FR = (
    'janvier', 'février', 'mars', 'avril', 'mai', 'juin',
    'juillet', 'août', 'septembre', 'octobre', 'novembre', 'décembre',
)  # fmt: skip
MEAL_TIMES_FR = {'breakfast': 'petit-déjeuner', 'lunch': 'déjeuner', 'dinner': 'dîner'}


def quoted(text: str | None, fallback: str = 'ton plat') -> str:
    """« Quiche lorraine » (guillemets + espaces insécables) ; `fallback` sans titre.

    Le genre du plat est inconnu (« ta quiche » / « ton gratin ») : on cite le titre plutôt que
    de risquer une faute d'accord.
    """
    text = (text or '').strip()
    return f'«{NBSP}{text}{NBSP}»' if text else fallback


def excerpt(text: str | None, limit: int = 80) -> str:
    text = ' '.join((text or '').split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + '…'


def plural(n: int, singular: str, plural_form: str | None = None) -> str:
    return singular if n == 1 else (plural_form or f'{singular}s')


def people(names: list[str], total: int | None = None) -> str:
    """« Léa », « Léa et Tom », « Léa et 3 autres » (premier nom = le plus récent)."""
    names = [n for n in names if n]
    total = max(total or len(names), len(names))
    if not names:
        return 'Quelqu’un' if total <= 1 else f'{total} personnes'
    if total == 1:
        return names[0]
    if total == 2 and len(names) >= 2:
        return f'{names[0]} et {names[1]}'
    return f'{names[0]} et {total - 1} autres'


def fr_date(d: date | None) -> str:
    """« samedi 15 novembre »."""
    if not d:
        return ''
    return f'{DAYS_FR[d.weekday()]} {d.day} {MONTHS_FR[d.month - 1]}'


def meal_moment(meal_time: str | None, d: date | None) -> str:
    """« au dîner du samedi 15 novembre » / « au repas du … »."""
    label = MEAL_TIMES_FR.get(meal_time or '', 'repas')
    when = fr_date(d)
    return f'au {label} du {when}' if when else f'au {label}'
