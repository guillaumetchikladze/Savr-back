"""
Récap annuel (spec gamification §3.6) : slides ordonnées, une idée par écran.

Chaque slide : {id, title, big_number, unit, subtitle, data}. Les slides sans matière (aucun invité,
aucun miam…) sont omises. Historique trop mince (< MIN_COOKED_FOR_RECAP) → payload « ça commence
maintenant ». La carte de partage (image 9:16) est générée côté app ; le partage reste opt-in.
"""

from __future__ import annotations

from datetime import date

from django.conf import settings
from django.utils import timezone

from .stats_service import MONTHS_FR, compute_stats, resolve_period

MIN_COOKED_FOR_RECAP = 3

PERSONAS = {
    'hote': ('Hôte', '🍽️', 'Ta cuisine, c’est fait pour être partagé : tu as régalé ton monde.'),
    'explorateur': ('Explorateur', '🧭', 'Nouvelles saveurs, nouvelles cuisines : tu as fait le tour du monde aux fourneaux.'),
    'regulier': ('Régulier', '⏱️', 'Semaine après semaine, tu as tenu le rythme : la cuisine fait partie de ta vie.'),
    'fidele': ('Fidèle', '💛', 'Tu sais ce que tu aimes, et tu le cuisines à la perfection.'),
    'gourmand': ('Gourmand', '😋', 'Tu cuisines pour le plaisir, et ça se voit.'),
}  # fmt: skip


def recap_available_from(year: int) -> date:
    month, day = (int(x) for x in str(getattr(settings, 'RECAP_AVAILABLE_FROM', '12-01')).split('-'))
    return date(year, month, day)


def is_recap_available(year: int, user=None, today: date | None = None) -> bool:
    today = today or timezone.localdate()
    if getattr(settings, 'RECAP_FORCE_AVAILABLE', False):
        return True
    if user is not None and getattr(user, 'is_staff', False):
        return True
    return today >= recap_available_from(year)


def pick_persona(p: dict) -> dict:
    """Règle simple et explicable sur les stats de l'année (pas de score caché)."""
    cooked = max(p['cooked_count'], 1)
    hosted = p['hosted']
    star = p['star_dish']
    if hosted['people_fed_count'] >= 10 or (hosted['meals_hosted'] >= 4 and hosted['meals_hosted'] / cooked >= 0.25):
        code = 'hote'
    elif p['cuisines_count'] >= 5 or p['new_ingredients_in_period'] >= 25:
        code = 'explorateur'
    elif p['weekly_streak_best'] >= 8:
        code = 'regulier'
    elif star and star['count'] >= 4 and star['count'] / cooked >= 0.15:
        code = 'fidele'
    else:
        code = 'gourmand'
    label, emoji, description = PERSONAS[code]
    return {'code': code, 'label': label, 'emoji': emoji, 'description': description}


def _plural(n: int, singular: str, plural: str | None = None) -> str:
    return singular if n == 1 else (plural or f'{singular}s')


def build_recap(user, year: int, *, today: date | None = None) -> dict:
    today = today or timezone.localdate()
    period = resolve_period('year', year, today=today)
    stats = compute_stats(user, period, today=today, with_recap_extras=True)
    p = stats.payload
    base = {
        'year': year,
        'available': True,
        'generated_at': timezone.now().isoformat(),
        'cooked_count': p['cooked_count'],
    }

    if p['cooked_count'] < MIN_COOKED_FOR_RECAP:
        missing = MIN_COOKED_FOR_RECAP - p['cooked_count']
        ongoing = year == today.year
        return {
            **base,
            'thin': True,
            'persona': None,
            'slides': [
                {
                    'id': 'starting_now',
                    'title': 'Ton récap commence maintenant',
                    'big_number': p['cooked_count'],
                    'unit': _plural(p['cooked_count'], 'plat'),
                    'subtitle': (
                        f'Encore {missing} {_plural(missing, "plat")} pour débloquer ton récap {year}.'
                        if ongoing
                        else f'Chaque plat cuisiné compte pour ton récap {year + 1}.'
                    ),
                    'data': {'min_cooked': MIN_COOKED_FOR_RECAP, 'missing': missing, 'next_year': year + 1},
                }
            ],
        }

    slides = []
    month = p['most_active_month']
    slides.append(
        {
            'id': 'year_in_cooking',
            'title': 'Ton année en cuisine',
            'big_number': p['cooked_count'],
            'unit': _plural(p['cooked_count'], 'plat cuisiné', 'plats cuisinés'),
            'subtitle': f'{p["cooked_days"]} {_plural(p["cooked_days"], "jour")} aux fourneaux'
            + (f', et {month["label"]} en tête' if month else ''),
            'data': {
                'cooked_days': p['cooked_days'],
                'total_cooking_minutes': p['total_cooking_minutes'],
                'most_active_month': month,
                'months': [{'month': i + 1, 'label': MONTHS_FR[i], 'count': n} for i, n in enumerate(stats.months)],
            },
        }
    )
    star = p['star_dish']
    if star:
        slides.append(
            {
                'id': 'star_dish',
                'title': 'Ton plat star',
                'big_number': star['count'],
                'unit': 'fois',
                'subtitle': star['label'],
                'data': star,
            }
        )
    if p['distinct_ingredients']:
        new = p['new_ingredients_in_period']
        slides.append(
            {
                'id': 'ingredients',
                'title': 'Tes ingrédients',
                'big_number': p['distinct_ingredients'],
                'unit': 'ingrédients différents',
                'subtitle': f'dont {new} {_plural(new, "nouveau", "nouveaux")} cette année'
                if new
                else 'Tes classiques',
                'data': {
                    'new_ingredients_in_period': new,
                    'new_ingredients_sample': p['new_ingredients_sample'],
                    'favorite_ingredient': p['favorite_ingredient'],
                    'distinct_plants': p['distinct_plants'],
                    'food_groups': p['food_groups'],
                },
            }
        )
    collectible = [c for c in p['cuisines'] if c['code'] not in ('fusion', 'autre')]
    if collectible:
        top = collectible[0]
        slides.append(
            {
                'id': 'world_tour',
                'title': 'Ton tour du monde',
                'big_number': p['cuisines_count'],
                'unit': _plural(p['cuisines_count'], 'cuisine'),
                'subtitle': f'{top["flag"]} {top["label"]} en tête',
                'data': {'cuisines': p['cuisines'], 'top_cuisine': top},
            }
        )
    if p['weekly_streak_best'] >= 2:
        slides.append(
            {
                'id': 'streak',
                'title': 'Ta série',
                'big_number': p['weekly_streak_best'],
                'unit': _plural(p['weekly_streak_best'], 'semaine'),
                'subtitle': 'd’affilée avec au moins un plat maison',
                'data': {'weekly_streak_best': p['weekly_streak_best'], 'record_week': p['record_week']},
            }
        )
    hosted = p['hosted']
    if hosted['people_fed_count'] or hosted['invited_count']:
        top_guest = hosted['top_guest']
        slides.append(
            {
                'id': 'hosted',
                'title': 'Tes régalés',
                'big_number': hosted['people_fed_count'],
                'unit': _plural(hosted['people_fed_count'], 'personne régalée', 'personnes régalées'),
                'subtitle': (
                    f'{top_guest["username"]} a le plus mangé chez toi'
                    if top_guest
                    else f'Invité·e {hosted["invited_count"]} fois à la table des autres'
                ),
                'data': hosted,
            }
        )
    if p['miam_received']:
        top_post = p['top_miam_post']
        slides.append(
            {
                'id': 'miam',
                'title': 'Tes miam',
                'big_number': p['miam_received'],
                'unit': 'miam reçus',
                'subtitle': f'« {top_post["title"]} » a fait le plus saliver' if top_post else '',
                'data': {'top_miam_post': top_post, 'biggest_fan': stats.biggest_fan},
            }
        )
    persona = pick_persona(p)
    slides.append(
        {
            'id': 'persona',
            'title': 'Ton profil cuisinier',
            'big_number': None,
            'unit': None,
            'subtitle': f'{persona["emoji"]} {persona["label"]}',
            'data': {
                **persona,
                'highlights': {
                    'cooked_count': p['cooked_count'],
                    'cuisines_count': p['cuisines_count'],
                    'distinct_ingredients': p['distinct_ingredients'],
                    'weekly_streak_best': p['weekly_streak_best'],
                    'people_fed_count': hosted['people_fed_count'],
                },
            },
        }
    )
    return {**base, 'thin': False, 'persona': persona, 'slides': slides}
