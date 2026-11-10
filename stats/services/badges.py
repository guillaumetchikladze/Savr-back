"""
Badges : définitions en code (registre), évaluées sur l'historique complet « cuisiné ».

- Registre = 28 badges historiques (ici) + badges loufoques (`badges_quirky.py`, spec badges-quirky)
  + météo (`badges_weather.py`, derrière `WEATHER_BADGES_ENABLED`). Familles : `badge_types.FAMILIES`.
- Règles douces : récompenser la curiosité et la régularité, jamais la quantité par jour
  (les plafonds doux de stats_service s'appliquent : un plat compte une fois par jour).
- `evaluate_badges(user)` persiste les badges nouvellement atteints (idempotent, contrainte unique)
  et renvoie leurs payloads (champ `new_badges` de POST /posts/cooked/). Une seule construction de l'historique
  par évaluation (`compute_stats` + `badge_history`) : nombre de requêtes constant.
- Payload : champs historiques (app 1.0.9) + `family`, `phrase`, `secret`, `hint`, rareté (`badge_rarity`)
  et `celebration` (normal | rare | secret). Secrets non obtenus : « ??? » + indice dans `?all=1`.
- Notification « Nouveau badge » (`new_badge`) ou, pour un badge rare / secret, « Badge secret débloqué… »
  (`badge_rare`) : in-app + push catégorie `rewards`, plafonnée ; push coupée par `BADGE_PUSH_ENABLED`.
  Jamais pour un badge dont la date d'obtention a plus de `BADGE_NOTIFY_MAX_AGE_DAYS` jours (rattrapage).
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import replace
from datetime import date, timedelta

from django.conf import settings
from django.utils import timezone

from core.celery_dispatch import publish

from ..models import UserBadge
from .badge_rarity import rarity_fields, rarity_map
from .badge_types import FAMILIES, TIER_RARE, TIER_TRES_RARE, BadgeDef, Metric
from .stats_service import StatsResult, compute_stats, resolve_period

logger = logging.getLogger(__name__)

__all__ = ['BADGES', 'BADGES_BY_CODE', 'BadgeDef', 'Metric', 'evaluate_badges', 'badges_overview']


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

# Estimation de rareté des badges historiques (avant le premier calcul quotidien).
_HISTORIC_RARITY = {
    'cuisines_10': TIER_RARE, 'cuisines_20': TIER_TRES_RARE, 'ingredients_100': TIER_RARE, 'plants_25': TIER_RARE,
    'streak_12': TIER_RARE, 'streak_26': TIER_TRES_RARE, 'host_20': TIER_RARE, 'host_50': TIER_TRES_RARE,
}  # fmt: skip


def _registry() -> tuple[BadgeDef, ...]:
    from .badges_quirky import QUIRKY_BADGES
    from .badges_weather import WEATHER_BADGES

    historic = tuple(
        replace(b, family=b.category, rarity_guess=_HISTORIC_RARITY.get(b.code, b.rarity_guess)) for b in BADGES
    )
    return (*historic, *QUIRKY_BADGES, *WEATHER_BADGES)


BADGES = _registry()
BADGES_BY_CODE = {b.code: b for b in BADGES}
_ORDER = {code: i for i, code in enumerate(BADGES_BY_CODE)}


def is_enabled(badge: BadgeDef) -> bool:
    return not badge.feature_flag or bool(getattr(settings, badge.feature_flag, False))


def enabled_badges() -> list[BadgeDef]:
    return [b for b in BADGES if is_enabled(b)]


def _measure(badge: BadgeDef, stats: StatsResult) -> tuple[int, date | None, dict]:
    result = badge.metric(stats)
    extra = result[2] if len(result) > 2 else {}
    return result[0], result[1], extra or {}


# ---------------------------------------------------------------------------
# Évaluation
# ---------------------------------------------------------------------------
def lifetime_stats(user) -> StatsResult:
    return compute_stats(user, resolve_period('all'))


def badge_public(badge: BadgeDef, rmap: dict | None = None) -> dict:
    """Payload public d'un badge (sans date) : champs historiques + famille, phrase, secret, rareté."""
    return {**badge.public(), **rarity_fields(badge, rmap)}


def _badge_payload(badge: BadgeDef, ub: UserBadge, rmap: dict | None = None) -> dict:
    return {**badge_public(badge, rmap), 'earned_at': ub.earned_at.isoformat(), 'context': ub.context or {}}


def is_fresh(payload: dict, today: date | None = None) -> bool:
    """Obtenu récemment (date d'obtention ≤ BADGE_NOTIFY_MAX_AGE_DAYS jours, ou inconnue) : à célébrer."""
    achieved = (payload.get('context') or {}).get('achieved_on')
    if not achieved:
        return True
    today = today or timezone.localdate()
    max_age = int(getattr(settings, 'BADGE_NOTIFY_MAX_AGE_DAYS', 7))
    try:
        return date.fromisoformat(achieved[:10]) >= today - timedelta(days=max_age)
    except ValueError:
        return True


def evaluate_badges(
    user,
    *,
    stats: StatsResult | None = None,
    notify: bool = True,
    push: bool = True,
    include_async: bool = False,
) -> list[dict]:
    """
    Persiste les badges atteints et pas encore obtenus ; renvoie les nouveaux (payloads).
    `notify` : notification in-app « Nouveau badge » ; `push` : aussi en push (inutile quand l'app
    affiche déjà les nouveaux badges dans la réponse, ex. POST /posts/cooked/ ou GET /badges/me/).
    Seuls les badges « frais » (`is_fresh`) sont notifiés : un rattrapage historique reste silencieux.
    `include_async` : évalue aussi les badges `async_only` (météo) — tâche Celery seulement.
    """
    stats = stats or lifetime_stats(user)
    owned = set(UserBadge.objects.filter(user=user).values_list('code', flat=True))
    to_create = []
    for badge in enabled_badges():
        if badge.code in owned or (badge.async_only and not include_async):
            continue
        current, achieved_on, extra = _measure(badge, stats)
        if current >= badge.target:
            context = {'achieved_on': achieved_on.isoformat() if achieved_on else None, 'value': current, **extra}
            to_create.append(UserBadge(user=user, code=badge.code, context=context))
    if not to_create:
        return []
    UserBadge.objects.bulk_create(to_create, ignore_conflicts=True)
    # Relecture : ne renvoyer que ceux réellement créés par cet appel (courses concurrentes).
    created = UserBadge.objects.filter(user=user, code__in=[b.code for b in to_create]).exclude(code__in=owned)
    rmap = rarity_map()
    new = [_badge_payload(BADGES_BY_CODE[ub.code], ub, rmap) for ub in created if ub.code in BADGES_BY_CODE]
    new.sort(key=lambda b: _ORDER[b['code']])
    fresh = [b['code'] for b in new if is_fresh(b, stats.today)]
    if notify and fresh:
        _schedule_notification(user.id, fresh, push=push)
    return new


def award_badges_safely(user) -> list[dict]:
    """
    Variante pour le chemin requête : ne lève jamais (le log « cuisiné » ne doit pas échouer).
    Ne renvoie que les badges frais : un rattrapage historique (nouveaux badges du registre) est attribué
    en silence, sans célébration de 20 badges d'un coup.
    """
    try:
        new = evaluate_badges(user, push=False)  # l'app affiche `new_badges` de la réponse
        return [b for b in new if is_fresh(b)]
    except Exception:
        logger.exception('[badges] évaluation en échec pour user=%s', getattr(user, 'id', None))
        return []


def _schedule_notification(user_id: int, codes: list[str], *, push: bool) -> None:
    try:
        from ..tasks import send_new_badge_push

        publish(send_new_badge_push, args=[user_id, codes, push])
    except Exception:
        logger.warning('[badges] notification non planifiée pour user=%s', user_id, exc_info=True)


def earned_badges_public(user) -> list[dict]:
    """Badges obtenus par `user`, pour son profil vu par un autre (sans réévaluation ni contexte)."""
    rows = UserBadge.objects.filter(user=user).order_by('-earned_at', 'code').values('code', 'earned_at')
    rmap = rarity_map()
    return [
        {**badge_public(BADGES_BY_CODE[row['code']], rmap), 'label': BADGES_BY_CODE[row['code']].title,
         'earned_at': row['earned_at'].isoformat()}
        for row in rows
        if row['code'] in BADGES_BY_CODE
    ]  # fmt: skip


def secret_placeholder(badge: BadgeDef, rmap: dict | None = None) -> dict:
    """Badge secret pas encore obtenu : « ??? » + indice (rien qui révèle la condition, code opaque)."""
    opaque = 'secret_' + hashlib.sha1(badge.code.encode()).hexdigest()[:10]
    return {
        'code': opaque,
        'title': '???',
        'emoji': '❓',
        'description': badge.hint,
        'category': badge.category,
        'family': badge.family_code,
        'family_label': FAMILIES.get(badge.family_code, ''),
        'phrase': '',
        'secret': True,
        'hint': badge.hint,
        **{k: v for k, v in rarity_fields(badge, rmap).items() if k != 'celebration'},
        'celebration': 'secret',
        'progress': {'current': 0, 'target': 1, 'ratio': 0.0},
    }


def badges_overview(user, *, next_limit: int | None = 5) -> dict:
    """GET /badges/me/ : badges obtenus + prochains à portée (progression) ; `next_limit=None` = tous les verrouillés."""
    stats = lifetime_stats(user)
    evaluate_badges(user, stats=stats, push=False)  # rattrape les « cuisiné » loggés hors POST /posts/cooked/
    owned = {ub.code: ub for ub in UserBadge.objects.filter(user=user)}
    rmap = rarity_map()
    badges = enabled_badges()
    earned = [_badge_payload(b, owned[b.code], rmap) for b in badges if b.code in owned]
    earned.sort(key=lambda b: ((b['context'] or {}).get('achieved_on') or '', -_ORDER[b['code']]), reverse=True)
    upcoming, secrets = [], []
    for badge in badges:
        if badge.code in owned:
            continue
        if badge.secret:
            # Jamais dans les « prochains » (aperçu) ; « ??? » + indice dans la liste complète.
            if next_limit is None:
                secrets.append(secret_placeholder(badge, rmap))
            continue
        current = 0 if badge.async_only else min(_measure(badge, stats)[0], badge.target)  # météo : tâche seule
        upcoming.append(
            {
                **badge_public(badge, rmap),
                'progress': {'current': current, 'target': badge.target, 'ratio': round(current / badge.target, 2)},
            }
        )
    # Les plus proches d'abord ; les badges ingrédient (0/1) passent après les progressions entamées.
    upcoming.sort(key=lambda b: (-b['progress']['ratio'], b['progress']['target'] == 1))
    families = []
    for code, label in FAMILIES.items():
        total = sum(1 for b in badges if b.family_code == code)
        if total:
            got = sum(1 for b in badges if b.family_code == code and b.code in owned)
            families.append({'code': code, 'label': label, 'earned': got, 'total': total})
    return {
        'earned': earned,
        'next': upcoming + secrets if next_limit is None else upcoming[:next_limit],
        'earned_count': len(earned),
        'total_count': len(badges),
        'families': families,
    }


# ---------------------------------------------------------------------------
# Carte à partager (GET /api/badges/<code>/share/)
# ---------------------------------------------------------------------------
def share_text(badge: BadgeDef, invite_url: str) -> str:
    phrase = (badge.phrase or badge.description).strip()
    return f'J’ai débloqué le badge « {badge.title} » {badge.emoji} sur Tchikook. {phrase} Rejoins-moi : {invite_url}'


def badge_share_payload(user, code: str) -> dict | None:
    """
    Données de la carte verticale à partager (rendue par l'app) : badge, date, rareté, nom affichable et
    lien d'invitation personnel. None si le badge n'existe pas ou n'est pas obtenu (un secret reste secret).
    """
    from accounts.services.invite_service import app_invite_url, get_or_create_active_code, invite_url
    from core.public_links import public_display_name

    badge = BADGES_BY_CODE.get(code)
    if badge is None or not is_enabled(badge):
        return None
    ub = UserBadge.objects.filter(user=user, code=code).first()
    if ub is None:
        return None
    invite = get_or_create_active_code(user)
    url = invite_url(invite.code)
    context = ub.context or {}
    return {
        'badge': badge_public(badge, rarity_map()),
        'earned_at': ub.earned_at.isoformat(),
        'achieved_on': context.get('achieved_on'),
        'context': context,
        'user': {'id': user.id, 'display_name': public_display_name(user)},
        'invite': {'code': invite.code, 'url': url, 'app_url': app_invite_url(invite.code)},
        'share_text': share_text(badge, url),
    }
