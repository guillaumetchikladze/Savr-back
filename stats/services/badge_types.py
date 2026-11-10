"""Types partagés du registre des badges (`badges.py` = historiques, `badges_quirky.py` = loufoques)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from .stats_service import StatsResult

# Une métrique renvoie (valeur courante, date d'obtention | None[, contexte additionnel]).
Metric = Callable[[StatsResult], tuple]

# Familles (grille groupée côté app). Les 5 premières = `category` historique des 28 badges d'origine.
FAMILIES: dict[str, str] = {
    'premiere_fois': 'Premières fois',
    'ingredient': 'Ingrédients',
    'collection': 'Collections',
    'serie': 'Séries',
    'hote': 'Hôte',
    'meme_recette': 'La même recette, encore',
    'calendrier': 'Le calendrier',
    'heures': 'Les heures',
    'defis': 'Les défis absurdes',
    'a_plusieurs': 'À plusieurs',
    'materiel': 'Le matériel',
    'carnet': 'Le carnet',
    'meteo': 'La météo',
}

# Paliers de rareté (du plus courant au plus rare).
TIER_COMMUN = 'commun'
TIER_RARE = 'rare'
TIER_TRES_RARE = 'tres_rare'
TIER_LEGENDAIRE = 'legendaire'
TIERS: dict[str, str] = {
    TIER_COMMUN: 'Commun',
    TIER_RARE: 'Rare',
    TIER_TRES_RARE: 'Très rare',
    TIER_LEGENDAIRE: 'Légendaire',
}
RARE_TIERS = frozenset({TIER_RARE, TIER_TRES_RARE, TIER_LEGENDAIRE})

# Énigmes : les indices 2 et 3 se débloquent après N plats cuisinés **depuis l'arrivée du badge** (`since`),
# pas sur tout l'historique (sinon un cuisinier actif lit la condition dès le premier jour).
HINT_STEPS = (10, 25)
# Date d'arrivée des énigmes v3 (bdg3, 13 secrets convertis + 6 nouvelles). Une énigme ajoutée plus tard
# renseigne son propre `BadgeDef.since` (date de mise en prod).
ENIGMES_V3_SINCE = date(2026, 11, 10)


@dataclass(frozen=True)
class BadgeDef:
    code: str
    title: str
    emoji: str
    description: str  # condition, lisible (affichée sauf secret non obtenu)
    category: str  # historique (app 1.0.9) ; = famille pour les nouveaux badges
    target: int
    metric: Metric
    family: str = ''
    phrase: str = ''  # phrase drôle (carte à partager, célébration)
    secret: bool = False
    hint: str = ''  # indice vague d'un badge secret (« ??? »)
    hints: tuple[str, ...] = ()  # 3 indices progressifs pour les énigmes
    icon: str = ''  # nom de l'icône Microsoft Fluent Flat
    mystery: str = ''  # 'enigme' | 'introuvable' | ''
    since: date | None = None  # arrivée de l'énigme (indices progressifs) ; None = ENIGMES_V3_SINCE
    rarity_guess: str = TIER_COMMUN  # palier tant que la rareté calculée n'existe pas
    feature_flag: str = ''  # réglage Django qui doit valoir True pour évaluer / afficher ce badge
    async_only: bool = False  # évalué seulement en tâche Celery (météo : données réseau), jamais dans la requête

    @property
    def hints_since(self) -> date:
        return self.since or ENIGMES_V3_SINCE

    @property
    def family_code(self) -> str:
        return self.family or self.category

    def public(self) -> dict:
        return {
            'code': self.code,
            'title': self.title,
            'emoji': self.emoji,
            'icon': self.icon,
            'description': self.description,
            'category': self.category,
            'family': self.family_code,
            'family_label': FAMILIES.get(self.family_code, ''),
            'phrase': self.phrase,
            'secret': self.secret,
            'mystery': self.mystery or ('enigme' if self.secret else ''),
            'hint': self.hint if self.secret else '',
        }


def reached(dates: list[date], target: int) -> tuple[int, date | None]:
    """(nombre, date de la `target`-ième occurrence) sur une liste de dates triées."""
    return len(dates), (dates[target - 1] if len(dates) >= target else None)


def once(achieved_on: date | None, **extra) -> tuple:
    """Badge « une fois » : (1, date, contexte) ou (0, None)."""
    if achieved_on is None:
        return 0, None
    return (1, achieved_on, extra) if extra else (1, achieved_on)
