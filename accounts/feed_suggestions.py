"""
Suggestions de profils pour le feed : complices en commun + comptes récents + part d'aléatoire.
Reste borné en coût (pool limité, requêtes Follow en masse).
"""
from __future__ import annotations

from collections import defaultdict
import random

from django.contrib.auth import get_user_model
from django.utils import timezone

from .models import Follow

User = get_user_model()

MAX_POOL = 160
MAX_CANDIDATES_AFTER_SCORE = 36
MAX_RETURN = 10
PREVIEW_N = 3
RECENT_SLICE = 90
EXTRA_RANDOM_CAP = 2000
EXTRA_RANDOM_PICK = 70


def _recency_score(created_at, now) -> float:
    if not created_at:
        return 0.0
    days = (now - created_at).total_seconds() / 86400.0
    return max(0.0, 2.0 * (1.0 - min(days, 365.0) / 365.0))


def _viewer_network_ids(viewer_id: int) -> set[int]:
    following = set(
        Follow.objects.filter(follower_id=viewer_id).values_list('following_id', flat=True)
    )
    followers = set(
        Follow.objects.filter(following_id=viewer_id).values_list('follower_id', flat=True)
    )
    return following | followers


def _candidate_pool_ids(excluded: set[int], rng) -> list[int]:
    base = User.objects.filter(is_active=True).exclude(id__in=excluded)
    if not base.exists():
        return []

    recent_ids = list(base.order_by('-created_at').values_list('id', flat=True)[:RECENT_SLICE])
    rest_qs = base.exclude(id__in=recent_ids) if recent_ids else base
    rest_ids = list(rest_qs.values_list('id', flat=True)[:EXTRA_RANDOM_CAP])
    extra: list[int] = []
    if rest_ids:
        k = min(EXTRA_RANDOM_PICK, len(rest_ids))
        extra = rng.sample(rest_ids, k)

    pool = list(set(recent_ids) | set(extra))
    if len(pool) > MAX_POOL:
        pool = rng.sample(pool, MAX_POOL)
    return pool


def _mutual_sets(network: set[int], pool_ids: list[int]) -> dict[int, set[int]]:
    """Pour chaque candidat, complices partagés avec le viewer (intersection réseaux via arêtes Follow)."""
    mutual: dict[int, set[int]] = defaultdict(set)
    if not network or not pool_ids:
        return mutual

    pool_set = set(pool_ids)
    for row in Follow.objects.filter(
        follower_id__in=network, following_id__in=pool_set
    ).values_list('follower_id', 'following_id'):
        u, c = row
        if u != c:
            mutual[c].add(u)

    for row in Follow.objects.filter(
        follower_id__in=pool_set, following_id__in=network
    ).values_list('follower_id', 'following_id'):
        c, u = row
        if u != c:
            mutual[c].add(u)

    return mutual


def build_feed_user_suggestions(viewer):
    """
    Retourne une liste de dicts :
    { 'user': User, 'mutual_complices_count': int, 'mutual_preview': [User, ...] }
    """
    now = timezone.now()
    rng = random.Random(viewer.id * 100_003 + int(now.timestamp() // 86_400))

    following = set(Follow.objects.filter(follower=viewer).values_list('following_id', flat=True))
    followers = set(Follow.objects.filter(following=viewer).values_list('follower_id', flat=True))
    excluded = following | followers | {viewer.id}

    pool_ids = _candidate_pool_ids(excluded, rng)
    if not pool_ids:
        return []

    network = _viewer_network_ids(viewer.id)
    mutual_map = _mutual_sets(network, pool_ids)

    users = User.objects.filter(id__in=pool_ids).only('id', 'username', 'avatar_url', 'created_at')
    scored = []
    for u in users:
        mset = mutual_map.get(u.id, set())
        mcount = len(mset)
        score = 3.0 * mcount + _recency_score(u.created_at, now) + rng.random()
        scored.append((score, u, mset))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:MAX_CANDIDATES_AFTER_SCORE]
    rng.shuffle(top)
    top = top[:MAX_RETURN]

    preview_user_ids: set[int] = set()
    for _, u, mset in top:
        preview_user_ids.update(list(mset)[:PREVIEW_N])

    preview_bulk = User.objects.filter(id__in=preview_user_ids).only('id', 'username', 'avatar_url')
    preview_by_id = {x.id: x for x in preview_bulk}

    out = []
    for _, u, mset in top:
        ordered = sorted(mset)
        preview = [preview_by_id[i] for i in ordered[:PREVIEW_N] if i in preview_by_id]
        out.append(
            {
                'user': u,
                'mutual_complices_count': len(mset),
                'mutual_preview': preview,
            }
        )
    return out
