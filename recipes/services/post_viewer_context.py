"""
État « viewer » des posts sérialisés, calculé en lot une fois par requête (pas de N+1).

Stocké dans le contexte du serializer (`context[CONTEXT_KEY]`) :
- relations de suivi viewer ↔ auteurs et co-cuisiniers (`follow_service.get_follow_relations`) ;
- repas où le viewer est invité (invitation en attente ou acceptée) → `can_repost` ;
- posts déjà repostés par le viewer (si l'annotation `_has_reposted_by_me` manque).

`prime(context, posts)` précharge toute une page (appelé par `PostPageListSerializer`) ; les accès
unitaires complètent le cache pour les ids manquants (détail d'un post : requêtes constantes, quel que
soit le nombre de co-cuisiniers).
"""

from __future__ import annotations

from dataclasses import dataclass, field

CONTEXT_KEY = '_post_viewer_state'
INVITATION_STATUSES = ('pending', 'accepted')


@dataclass
class PostViewerState:
    viewer: object | None
    relations: dict[int, dict] = field(default_factory=dict)
    invited_meal_plan_ids: set[int] = field(default_factory=set)
    checked_meal_plan_ids: set[int] = field(default_factory=set)
    reposted_post_ids: set[int] = field(default_factory=set)
    checked_post_ids: set[int] = field(default_factory=set)

    @property
    def authenticated(self) -> bool:
        return bool(self.viewer is not None and getattr(self.viewer, 'is_authenticated', False))

    # -- relations de suivi --------------------------------------------------
    def load_relations(self, user_ids) -> None:
        missing = {i for i in user_ids if i is not None and i not in self.relations}
        if missing:
            from accounts.services.follow_service import get_follow_relations

            self.relations.update(get_follow_relations(self.viewer, missing))

    def relation(self, user_id: int) -> dict:
        self.load_relations([user_id])
        return dict(self.relations[user_id])

    # -- invitations au repas ------------------------------------------------
    def load_invitations(self, meal_plan_ids) -> None:
        missing = {i for i in meal_plan_ids if i and i not in self.checked_meal_plan_ids}
        if not missing or not self.authenticated:
            return
        from recipes.models import MealInvitation

        self.invited_meal_plan_ids.update(
            MealInvitation.objects.filter(
                invitee_id=self.viewer.id, meal_plan_id__in=missing, status__in=INVITATION_STATUSES
            ).values_list('meal_plan_id', flat=True)
        )
        self.checked_meal_plan_ids |= missing

    # -- reposts du viewer ---------------------------------------------------
    def load_reposts(self, post_ids) -> None:
        missing = {i for i in post_ids if i and i not in self.checked_post_ids}
        if not missing or not self.authenticated:
            return
        from recipes.models import PostRepost

        self.reposted_post_ids.update(
            PostRepost.objects.filter(user_id=self.viewer.id, original_post_id__in=missing).values_list(
                'original_post_id', flat=True
            )
        )
        self.checked_post_ids |= missing

    def has_reposted(self, post) -> bool:
        annotated = getattr(post, '_has_reposted_by_me', None)
        if annotated is not None:
            return bool(annotated)
        self.load_reposts([post.id])
        return post.id in self.reposted_post_ids

    def can_repost(self, post) -> bool:
        """Même règle que `repost_service.user_can_repost_post`, sur les données préchargées."""
        if not self.authenticated:
            return False
        if not post.is_published or not post.meal_plan_id:
            return False
        if post.user_id == self.viewer.id:
            return False
        if self.has_reposted(post):
            return False
        self.load_invitations([post.meal_plan_id])
        return post.meal_plan_id in self.invited_meal_plan_ids


def viewer_state(context: dict) -> PostViewerState:
    state = context.get(CONTEXT_KEY)
    if state is None:
        request = context.get('request')
        state = PostViewerState(viewer=getattr(request, 'user', None) if request else None)
        context[CONTEXT_KEY] = state
    return state


def _co_cooked_user_ids(post) -> list[int]:
    ids = [post.user_id] if post.user_id else []
    reposts = getattr(post, '_prefetched_objects_cache', {}).get('reposts')
    if reposts is not None:
        ids.extend(r.user_id for r in reposts)
    return ids


def prime(context: dict, posts) -> None:
    """Précharge l'état viewer d'une page de posts (≤ 5 requêtes, quelle que soit la taille)."""
    posts = [p for p in posts if p is not None and getattr(p, 'pk', None)]
    state = viewer_state(context)
    if not posts or not state.authenticated:
        return
    without_prefetched_reposts = [p.id for p in posts if 'reposts' not in getattr(p, '_prefetched_objects_cache', {})]
    user_ids = set()
    for post in posts:
        user_ids.update(_co_cooked_user_ids(post))
    if without_prefetched_reposts:
        from recipes.models import PostRepost

        user_ids.update(
            PostRepost.objects.filter(original_post_id__in=without_prefetched_reposts).values_list('user_id', flat=True)
        )
    state.load_relations(user_ids)
    state.load_reposts([p.id for p in posts if getattr(p, '_has_reposted_by_me', None) is None])
    state.load_invitations(
        [
            p.meal_plan_id
            for p in posts
            if p.is_published and p.meal_plan_id and p.user_id != state.viewer.id and not state.has_reposted(p)
        ]
    )
