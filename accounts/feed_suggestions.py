"""
Suggestions de profils pour le feed : complices en commun + comptes récents + part d'aléatoire.
Les comptes qui vous suivent sans que vous les suiviez en retour sont éligibles et listés en premier.
Reste borné en coût (pool limité, requêtes Follow en masse).
"""

from __future__ import annotations

import random
from collections import defaultdict

from django.contrib.auth import get_user_model
from django.utils import timezone

from .blocks import blocked_user_ids_for
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
    following = set(Follow.objects.filter(follower_id=viewer_id).values_list('following_id', flat=True))
    followers = set(Follow.objects.filter(following_id=viewer_id).values_list('follower_id', flat=True))
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
    for row in Follow.objects.filter(follower_id__in=network, following_id__in=pool_set).values_list(
        'follower_id', 'following_id'
    ):
        u, c = row
        if u != c:
            mutual[c].add(u)

    for row in Follow.objects.filter(follower_id__in=pool_set, following_id__in=network).values_list(
        'follower_id', 'following_id'
    ):
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
    # Ne pas exclure les abonnés : quelqu’un qui vous suit sans que vous le suiviez doit pouvoir être
    # suggéré (et en priorité), comme sur les réseaux habituels.
    # Exclure aussi les comptes bloqués (dans les deux sens).
    excluded = following | {viewer.id} | blocked_user_ids_for(viewer)

    pool_ids = _candidate_pool_ids(excluded, rng)
    if not pool_ids:
        return []

    network = _viewer_network_ids(viewer.id)
    mutual_map = _mutual_sets(network, pool_ids)

    users = User.objects.filter(id__in=pool_ids).only('id', 'username', 'first_name', 'avatar_url', 'created_at')
    scored = []
    for u in users:
        mset = mutual_map.get(u.id, set())
        mcount = len(mset)
        score = 3.0 * mcount + _recency_score(u.created_at, now) + rng.random()
        scored.append((score, u, mset))

    scored.sort(key=lambda x: x[0], reverse=True)

    def _follows_you_not_following_back(uid: int) -> bool:
        return uid in followers and uid not in following

    prio = [row for row in scored if _follows_you_not_following_back(row[1].id)]
    rest = [row for row in scored if not _follows_you_not_following_back(row[1].id)]
    prio.sort(key=lambda x: x[0], reverse=True)
    prio_pick = prio[:MAX_RETURN]
    remaining = MAX_RETURN - len(prio_pick)
    rest_top = rest[:MAX_CANDIDATES_AFTER_SCORE]
    rng.shuffle(rest_top)
    top = prio_pick + rest_top[: max(0, remaining)]

    preview_user_ids: set[int] = set()
    for _, _u, mset in top:
        preview_user_ids.update(list(mset)[:PREVIEW_N])

    preview_bulk = User.objects.filter(id__in=preview_user_ids).only('id', 'username', 'first_name', 'avatar_url')
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


# ---------------------------------------------------------------------------
# Onboarding social (« Tes amis avant ton premier plat ») : amis d'amis + liens d'invitation.
# Jamais de géolocalisation ni de carnet d'adresses : seulement le graphe Follow et les invitations.
# ---------------------------------------------------------------------------
REASON_INVITED_YOU = 'invited_you'
REASON_YOU_INVITED = 'you_invited'
REASON_FOLLOWS_YOU = 'follows_you'
REASON_MUTUAL_FRIENDS = 'mutual_friends'
REASON_SUGGESTED = 'suggested'
_REASON_BONUS = {REASON_INVITED_YOU: 100.0, REASON_YOU_INVITED: 90.0, REASON_FOLLOWS_YOU: 50.0}
MAX_SECOND_DEGREE_EDGES = 5000


def _second_degree_mutuals(network: set[int], excluded: set[int]) -> dict[int, set[int]]:
    """Amis d'amis : pour chaque candidat hors `excluded`, les complices du viewer qui le relient."""
    mutual: dict[int, set[int]] = defaultdict(set)
    if not network:
        return mutual
    outgoing = (
        Follow.objects.filter(follower_id__in=network)
        .exclude(following_id__in=excluded)
        .values_list('follower_id', 'following_id')[:MAX_SECOND_DEGREE_EDGES]
    )
    for u, c in outgoing:
        mutual[c].add(u)
    incoming = (
        Follow.objects.filter(following_id__in=network)
        .exclude(follower_id__in=excluded)
        .values_list('following_id', 'follower_id')[:MAX_SECOND_DEGREE_EDGES]
    )
    for u, c in incoming:
        mutual[c].add(u)
    return mutual


def build_onboarding_people_suggestions(viewer, limit: int = MAX_RETURN):
    """
    Personnes à ajouter pendant l'onboarding social, classées :
    lien d'invitation (qui t'a invité / que tu as invité) > te suit déjà > amis en commun.
    Exclut soi, les comptes déjà suivis, les blocages (deux sens), les comptes inactifs ou en attente.
    Complété si besoin par `build_feed_user_suggestions` (raison `suggested`).

    Retourne des dicts `{user, mutual_complices_count, mutual_preview, reason, follow_request_outgoing}`.
    """
    from .models import FollowRequest, InviteRedemption

    following = set(Follow.objects.filter(follower=viewer).values_list('following_id', flat=True))
    followers = set(Follow.objects.filter(following=viewer).values_list('follower_id', flat=True))
    blocked = blocked_user_ids_for(viewer)
    excluded = following | {viewer.id} | blocked

    mutual_map = _second_degree_mutuals(following | followers, excluded)
    reasons: dict[int, str] = {uid: REASON_FOLLOWS_YOU for uid in followers - excluded}
    for uid in InviteRedemption.objects.filter(inviter=viewer).values_list('invitee_id', flat=True):
        if uid not in excluded:
            reasons[uid] = REASON_YOU_INVITED
    for uid in InviteRedemption.objects.filter(invitee=viewer).values_list('inviter_id', flat=True):
        if uid not in excluded:
            reasons[uid] = REASON_INVITED_YOU

    candidate_ids = set(mutual_map) | set(reasons)
    users = {
        u.id: u
        for u in User.objects.filter(id__in=candidate_ids, is_active=True, validated_at__isnull=False).only(
            'id', 'username', 'first_name', 'avatar_url', 'created_at'
        )
    }

    def score(uid: int) -> tuple:
        reason = reasons.get(uid)
        return (_REASON_BONUS.get(reason, 0.0) + 3.0 * len(mutual_map.get(uid, ())), -uid)

    ranked = sorted(users, key=score, reverse=True)[:limit]
    rows = [
        {
            'user': users[uid],
            'mset': mutual_map.get(uid, set()),
            'reason': reasons.get(uid) or REASON_MUTUAL_FRIENDS,
        }
        for uid in ranked
    ]

    preview_ids = {i for row in rows for i in sorted(row['mset'])[:PREVIEW_N]}
    preview_by_id = {
        x.id: x for x in User.objects.filter(id__in=preview_ids).only('id', 'username', 'first_name', 'avatar_url')
    }
    out = [
        {
            'user': row['user'],
            'mutual_complices_count': len(row['mset']),
            'mutual_preview': [preview_by_id[i] for i in sorted(row['mset'])[:PREVIEW_N] if i in preview_by_id],
            'reason': row['reason'],
        }
        for row in rows
    ]

    if len(out) < limit:
        seen = {row['user'].id for row in out}
        for row in build_feed_user_suggestions(viewer):
            if len(out) >= limit:
                break
            if row['user'].id not in seen:
                seen.add(row['user'].id)
                out.append({**row, 'reason': REASON_SUGGESTED})

    pending = set(
        FollowRequest.objects.filter(
            requester=viewer, target_id__in=[row['user'].id for row in out], status='pending'
        ).values_list('target_id', flat=True)
    )
    for row in out:
        row['follow_request_outgoing'] = row['user'].id in pending
    return out
