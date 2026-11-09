"""
Badges : définitions en code (registre), évaluées sur l'historique complet « cuisiné ».

- Règles douces : récompenser la curiosité et la régularité, jamais la quantité par jour
  (les plafonds doux de stats_service s'appliquent : un plat compte une fois par jour).
- `evaluate_badges(user)` persiste les badges nouvellement atteints (idempotent, contrainte unique)
  et renvoie leurs payloads (champ `new_badges` de POST /posts/cooked/).
- Push « Nouveau badge » derrière `settings.BADGE_PUSH_ENABLED` (défaut : désactivé).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from django.conf import settings

from ..models import UserBadge
from .stats_service import StatsResult, compute_stats, resolve_period

logger = logging.getLogger(__name__)

Metric = Callable[[StatsResult], tuple[int, date | None]]


@dataclass(frozen=True)
class BadgeDef:
    code: str
    title: str
    emoji: str
    description: str
    category: str  # premiere_fois | ingredient | collection | serie | hote
    target: int
    metric: Metric

    def public(self) -> dict:
        return {
            'code': self.code,
            'title': self.title,
            'emoji': self.emoji,
            'description': self.description,
            'category': self.category,
        }


# ---------------------------------------------------------------------------
# Métriques
# ---------------------------------------------------------------------------
def _threshold(dates_attr: str, target: int) -> Metric:
    def metric(s: StatsResult):
        dates = getattr(s, dates_attr)
        return len(dates), (dates[target - 1] if len(dates) >= target else None)

    return metric


def _first(attr: str) -> Metric:
    def metric(s: StatsResult):
        d = getattr(s, attr)
        return (1, d) if d else (0, None)

    return metric


def _ingredient(pattern: str) -> Metric:
    regex = re.compile(pattern)

    def metric(s: StatsResult):
        hits = [d for name, d in s.ingredient_first_seen.values() if regex.search(name)]
        return (1, min(hits)) if hits else (0, None)

    return metric


def _streak(target: int) -> Metric:
    def metric(s: StatsResult):
        best = max((run for run, _ in s.streak_runs), default=0)
        reached = next((monday for run, monday in s.streak_runs if run >= target), None)
        return best, reached

    return metric


def _people_fed(target: int) -> Metric:
    def metric(s: StatsResult):
        total = s.people_fed_timeline[-1][1] if s.people_fed_timeline else 0
        reached = next((d for d, cumul in s.people_fed_timeline if cumul >= target), None)
        return total, reached

    return metric


# ---------------------------------------------------------------------------
# Registre (ordre = ordre d'affichage)
# ---------------------------------------------------------------------------
_INGREDIENT_BADGES = (
    ('aubergine', 'Aubergine', '🍆', r'\baubergines?\b'),
    ('courge', 'Courge', '🎃', r'\b(courges?|butternut|potirons?|potimarrons?|citrouilles?)\b'),
    ('poireau', 'Poireau', '🥬', r'\bpoireaux?\b'),
    ('champignon', 'Champignon', '🍄', r'\b(champignons?|cepes?|girolles?|shiitakes?|pleurotes?)\b'),
    ('lentilles', 'Lentilles', '🫘', r'\blentilles?\b'),
    ('pois_chiches', 'Pois chiches', '🧆', r'\bpois chiches?\b'),
    ('epinards', 'Épinards', '🌿', r'\bepinards?\b'),
    ('betterave', 'Betterave', '🟣', r'\bbetteraves?\b'),
    ('chou', 'Chou', '🥦', r'\bchoux?\b'),
    ('tomate', 'Tomate', '🍅', r'\btomates?\b'),
    ('avocat', 'Avocat', '🥑', r'\bavocats?\b'),
    ('saumon', 'Saumon', '🐟', r'\bsaumons?\b'),
)

BADGES: tuple[BadgeDef, ...] = (
    BadgeDef(
        'first_cooked', 'Premier plat', '👩‍🍳', 'Ton premier « J’ai cuisiné ».', 'premiere_fois', 1,
        _first('first_cooked_on'),
    ),
    BadgeDef(
        'first_photo', 'Première photo', '📸', 'Ton premier plat partagé en photo.', 'premiere_fois', 1,
        _first('first_photo_on'),
    ),
    *(
        BadgeDef(
            f'ingredient_{code}', title, emoji, f'Premier plat cuisiné avec : {title.lower()}.',
            'ingredient', 1, _ingredient(pattern),
        )
        for code, title, emoji, pattern in _INGREDIENT_BADGES
    ),
    *(
        BadgeDef(
            f'cuisines_{n}', f'{n} cuisines du monde', '🌍', f'{n} cuisines différentes explorées.', 'collection', n,
            _threshold('cuisine_dates', n),
        )
        for n in (5, 10, 20)
    ),
    *(
        BadgeDef(
            f'ingredients_{n}', f'{n} ingrédients', '🧺', f'{n} ingrédients différents cuisinés (hors sel, huile…).',
            'collection', n, _threshold('distinct_ingredient_dates', n),
        )
        for n in (25, 50, 100)
    ),
    *(
        BadgeDef(
            f'plants_{n}', f'{n} végétaux', '🌱', f'{n} végétaux différents cuisinés.', 'collection', n,
            _threshold('plant_dates', n),
        )
        for n in (10, 25)
    ),
    *(
        BadgeDef(
            f'streak_{n}', f'{n} semaines d’affilée', '🔥', f'Au moins un plat cuisiné chaque semaine, {n} semaines de suite.',
            'serie', n, _streak(n),
        )
        for n in (4, 12, 26)
    ),
    *(
        BadgeDef(
            f'host_{n}', f'{n} personnes régalées', '🍽️', f'Tu as cuisiné pour {n} personnes.', 'hote', n,
            _people_fed(n),
        )
        for n in (5, 20, 50)
    ),
)  # fmt: skip
BADGES_BY_CODE = {b.code: b for b in BADGES}


# ---------------------------------------------------------------------------
# Évaluation
# ---------------------------------------------------------------------------
def lifetime_stats(user) -> StatsResult:
    return compute_stats(user, resolve_period('all'))


def _badge_payload(badge: BadgeDef, ub: UserBadge) -> dict:
    return {**badge.public(), 'earned_at': ub.earned_at.isoformat(), 'context': ub.context or {}}


def evaluate_badges(user, *, stats: StatsResult | None = None, notify: bool = True) -> list[dict]:
    """Persiste les badges atteints et pas encore obtenus ; renvoie les nouveaux (payloads)."""
    stats = stats or lifetime_stats(user)
    owned = set(UserBadge.objects.filter(user=user).values_list('code', flat=True))
    to_create = []
    for badge in BADGES:
        if badge.code in owned:
            continue
        current, achieved_on = badge.metric(stats)
        if current >= badge.target:
            context = {'achieved_on': achieved_on.isoformat() if achieved_on else None, 'value': current}
            to_create.append(UserBadge(user=user, code=badge.code, context=context))
    if not to_create:
        return []
    UserBadge.objects.bulk_create(to_create, ignore_conflicts=True)
    # Relecture : ne renvoyer que ceux réellement créés par cet appel (courses concurrentes).
    created = UserBadge.objects.filter(user=user, code__in=[b.code for b in to_create]).exclude(code__in=owned)
    new = [_badge_payload(BADGES_BY_CODE[ub.code], ub) for ub in created if ub.code in BADGES_BY_CODE]
    new.sort(key=lambda b: list(BADGES_BY_CODE).index(b['code']))
    if notify and new and getattr(settings, 'BADGE_PUSH_ENABLED', False):
        _schedule_push(user.id, [b['code'] for b in new])
    return new


def award_badges_safely(user) -> list[dict]:
    """Variante pour le chemin requête : ne lève jamais (le log « cuisiné » ne doit pas échouer)."""
    try:
        return evaluate_badges(user)
    except Exception:
        logger.exception('[badges] évaluation en échec pour user=%s', getattr(user, 'id', None))
        return []


def _schedule_push(user_id: int, codes: list[str]) -> None:
    try:
        from ..tasks import send_new_badge_push

        send_new_badge_push.apply_async(args=[user_id, codes], retry=False)
    except Exception:
        logger.warning('[badges] push non planifiée pour user=%s', user_id, exc_info=True)


def badges_overview(user, *, next_limit: int = 5) -> dict:
    """GET /badges/me/ : badges obtenus + prochains à portée (progression)."""
    stats = lifetime_stats(user)
    evaluate_badges(user, stats=stats)  # rattrape les « cuisiné » loggés hors POST /posts/cooked/
    owned = {ub.code: ub for ub in UserBadge.objects.filter(user=user)}
    earned = [_badge_payload(b, owned[b.code]) for b in BADGES if b.code in owned]
    order = {code: i for i, code in enumerate(BADGES_BY_CODE)}
    earned.sort(key=lambda b: ((b['context'] or {}).get('achieved_on') or '', -order[b['code']]), reverse=True)
    upcoming = []
    for badge in BADGES:
        if badge.code in owned:
            continue
        current, _ = badge.metric(stats)
        current = min(current, badge.target)
        upcoming.append(
            {
                **badge.public(),
                'progress': {'current': current, 'target': badge.target, 'ratio': round(current / badge.target, 2)},
            }
        )
    # Les plus proches d'abord ; les badges ingrédient (0/1) passent après les progressions entamées.
    upcoming.sort(key=lambda b: (-b['progress']['ratio'], b['progress']['target'] == 1))
    return {
        'earned': earned,
        'next': upcoming[:next_limit],
        'earned_count': len(earned),
        'total_count': len(BADGES),
    }
