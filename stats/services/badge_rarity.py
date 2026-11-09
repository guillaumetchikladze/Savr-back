"""
Rareté des badges : « Seulement 3 % des cuisiniers Tchikook l'ont ».

- Calcul quotidien (`compute_badge_rarity`, beat `stats-badge-rarity`) : pour chaque code du registre, part des
  cuisiniers **actifs** (≥ 1 « cuisiné » sur `BADGE_RARITY_ACTIVE_DAYS` jours, comptes actifs) qui l'ont obtenu.
  Résultat persisté dans `BadgeRarity` (une ligne par code), relu via un cache (1 requête au plus par heure).
- Paliers : légendaire < 1 %, très rare < 5 %, rare < 20 %, commun sinon. Tant que la population active est
  petite (< `BADGE_RARITY_MIN_ACTIVE_USERS`) ou qu'aucun calcul n'existe, le palier est l'estimation du
  registre (`BadgeDef.rarity_guess`) ; le pourcentage n'est exposé qu'une fois calculé.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count
from django.utils import timezone

from ..models import BadgeRarity, UserBadge
from .badge_types import RARE_TIERS, TIER_COMMUN, TIER_LEGENDAIRE, TIER_RARE, TIER_TRES_RARE, TIERS, BadgeDef

RARITY_CACHE_KEY = 'stats:badge-rarity:v1'
RARITY_CACHE_SECONDS = 3600
# (pourcentage strictement inférieur à, palier), du plus rare au moins rare.
THRESHOLDS: tuple[tuple[float, str], ...] = ((1.0, TIER_LEGENDAIRE), (5.0, TIER_TRES_RARE), (20.0, TIER_RARE))


def tier_for(percent: float) -> str:
    for limit, tier in THRESHOLDS:
        if percent < limit:
            return tier
    return TIER_COMMUN


def active_user_ids(today=None):
    """Sous-requête : cuisiniers actifs (≥ 1 repas confirmé sur la fenêtre, compte actif)."""
    from recipes.models import MealPlan

    today = today or timezone.localdate()
    since = today - timedelta(days=int(getattr(settings, 'BADGE_RARITY_ACTIVE_DAYS', 90)))
    return (
        MealPlan.objects.filter(confirmed=True, date__gte=since, date__lte=today, user__is_active=True)
        .values('user_id')
        .distinct()
    )


def compute_badge_rarity(*, today=None) -> dict[str, BadgeRarity]:
    """Recalcule et persiste la rareté de chaque badge du registre (3 requêtes + écriture en lot)."""
    from .badges import BADGES

    active = active_user_ids(today)
    active_count = active.count()
    holders = dict(
        UserBadge.objects.filter(user_id__in=active)
        .values('code')
        .annotate(n=Count('user_id', distinct=True))
        .values_list('code', 'n')
    )
    min_users = int(getattr(settings, 'BADGE_RARITY_MIN_ACTIVE_USERS', 20))
    now = timezone.now()
    rows = []
    for badge in BADGES:
        n = holders.get(badge.code, 0)
        percent = round(100.0 * n / active_count, 2) if active_count else 0.0
        tier = tier_for(percent) if active_count >= min_users else badge.rarity_guess
        rows.append(
            BadgeRarity(
                code=badge.code, holders=n, active_users=active_count, percent=percent, tier=tier, computed_at=now
            )
        )
    BadgeRarity.objects.bulk_create(
        rows,
        update_conflicts=True,
        unique_fields=['code'],
        update_fields=['holders', 'active_users', 'percent', 'tier', 'computed_at'],
    )
    try:
        cache.delete(RARITY_CACHE_KEY)
    except Exception:
        pass
    return {r.code: r for r in rows}


def rarity_map() -> dict[str, tuple[float, str]]:
    """{code: (pourcentage, palier)} du dernier calcul (cache 1 h ; vide tant que rien n'est calculé)."""
    try:
        cached = cache.get(RARITY_CACHE_KEY)
    except Exception:
        cached = None
    if cached is not None:
        return cached
    data = {code: (percent, tier) for code, percent, tier in BadgeRarity.objects.values_list('code', 'percent', 'tier')}
    try:
        cache.set(RARITY_CACHE_KEY, data, RARITY_CACHE_SECONDS)
    except Exception:
        pass
    return data


def _percent_fr(percent: float) -> str:
    if percent < 1:
        value = f'{percent:.1f}'.replace('.', ',')
        return 'moins de 1 %' if value in ('0,0', '1,0') else f'{value} %'
    return f'{round(percent)} %'


def rarity_text(percent: float | None) -> str | None:
    """« Seulement 3 % des cuisiniers Tchikook l'ont » (None tant que la rareté n'est pas calculée)."""
    if percent is None:
        return None
    if percent <= 0:
        return 'Presque personne ne l’a encore.'
    text = f'{_percent_fr(percent)} des cuisiniers Tchikook l’ont'
    return f'Seulement {text}' if percent < 20 else text[0].upper() + text[1:]


def rarity_fields(badge: BadgeDef, rmap: dict[str, tuple[float, str]] | None) -> dict:
    """Champs de rareté d'un payload badge + `celebration` (normal | rare | secret) pour la célébration."""
    percent, tier = (rmap or {}).get(badge.code, (None, badge.rarity_guess))
    return {
        'rarity_percent': round(percent, 1) if percent is not None else None,
        'rarity_tier': tier,
        'rarity_label': TIERS.get(tier, ''),
        'rarity_text': rarity_text(percent),
        'celebration': 'secret' if badge.secret else ('rare' if tier in RARE_TIERS else 'normal'),
    }


def is_special(badge: BadgeDef, rmap: dict[str, tuple[float, str]] | None) -> bool:
    """Badge rare (palier rare et au-delà) ou secret : notification « badge_rare », célébration renforcée."""
    return rarity_fields(badge, rmap)['celebration'] != 'normal'
