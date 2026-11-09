"""
Saisonnalité France (métropole) pour l'agent : saison par mois + fruits et légumes de saison.

Table statique volontairement courte (≈ 10 produits par mois, les plus parlants) : elle sert à
inspirer des idées, pas à faire un calendrier exhaustif.
"""

from datetime import date

MONTHS_FR = (
    'janvier',
    'février',
    'mars',
    'avril',
    'mai',
    'juin',
    'juillet',
    'août',
    'septembre',
    'octobre',
    'novembre',
    'décembre',
)

_SEASON_BY_MONTH = {
    12: 'hiver',
    1: 'hiver',
    2: 'hiver',
    3: 'printemps',
    4: 'printemps',
    5: 'printemps',
    6: 'été',
    7: 'été',
    8: 'été',
    9: 'automne',
    10: 'automne',
    11: 'automne',
}

IN_SEASON_BY_MONTH: dict[int, tuple[str, ...]] = {
    1: ('poireau', 'chou', 'endive', 'céleri-rave', 'panais', 'topinambour', 'mâche', 'clémentine', 'orange', 'kiwi'),
    2: ('endive', 'poireau', 'chou', 'panais', 'salsifis', 'céleri-rave', 'mâche', 'orange', 'pamplemousse', 'kiwi'),
    3: ('épinard', 'poireau', 'endive', 'radis', 'blette', 'chou', 'carotte', 'citron', 'kiwi', 'pomme'),
    4: ('asperge', 'radis', 'épinard', 'petits pois', 'blette', 'laitue', 'oignon nouveau', 'rhubarbe', 'fraise'),
    5: ('asperge', 'petits pois', 'fève', 'radis', 'artichaut', 'laitue', 'fraise', 'rhubarbe', 'cerise'),
    6: ('courgette', 'petits pois', 'fève', 'haricot vert', 'tomate', 'concombre', 'fraise', 'cerise', 'abricot'),
    7: ('tomate', 'courgette', 'aubergine', 'poivron', 'haricot vert', 'concombre', 'melon', 'pêche', 'abricot'),
    8: ('tomate', 'aubergine', 'poivron', 'courgette', 'maïs', 'melon', 'pastèque', 'pêche', 'prune', 'figue'),
    9: ('tomate', 'aubergine', 'poivron', 'champignons', 'potimarron', 'raisin', 'figue', 'prune', 'pomme', 'poire'),
    10: ('courge', 'potimarron', 'champignons', 'céleri', 'chou', 'poireau', 'châtaigne', 'pomme', 'poire', 'coing'),
    11: (
        'courge',
        'potiron',
        'panais',
        'topinambour',
        'poireau',
        'chou',
        'endive',
        'champignons',
        'châtaigne',
        'coing',
    ),
    12: ('courge', 'panais', 'topinambour', 'chou', 'endive', 'poireau', 'mâche', 'clémentine', 'orange', 'châtaigne'),
}


def season_for(day: date) -> str:
    return _SEASON_BY_MONTH[day.month]


def in_season_produce(day: date) -> tuple[str, ...]:
    return IN_SEASON_BY_MONTH[day.month]
