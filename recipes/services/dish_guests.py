"""
Convives d'un plat (ckd2 lot 3) : « Avec qui » et @Léa dans « Pour tes amis ».

Un convive direct = une `MealInvitation` du repas du post **déjà acceptée** (pas d'écran d'invitation) :
même modèle que les invitations acceptées d'avant, donc visibilité du post, « republier » (`can_repost`),
« avec Léa » du carnet et compte des convives marchent sans autre changement.

- Qui peut être ajouté (`guest_candidates`) : amis (suivi approuvé dans un sens ou l'autre ; mutuels
  d'abord), comptes du foyer, membres acceptés de ses tablées ; jamais soi, un compte inactif ou bloqué.
  Un id hors de cette liste est ignoré (le post part quand même).
- `source` : `direct` (notifié « Tom t'a mis dans son plat ») ; `household` = foyer préajouté parce que
  le plat vient du planning partagé (repas existant), **jamais notifié** ; un id du foyer envoyé hors
  planning est traité comme un convive ordinaire.
- Plat « rien que pour toi » (post non publié) : aucun convive ajouté ni notifié (sinon il verrait le
  repas) ; le choix est gardé sur `Post.held_guests` et appliqué à la publication.
- Le convive peut se retirer (`leave_dish`) : invitation refusée, son repost du plat retiré.
- « Annuler » le log retire les convives ajoutés par ce log (`cooked_undo.guests`) et leurs notifications in-app.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.db import transaction

User = get_user_model()

MAX_GUESTS = 20
SOURCE_DIRECT = 'direct'
SOURCE_HOUSEHOLD = 'household'


class GuestError(ValueError):
    def __init__(self, message: str, code: str = 'invalid_guest_ids'):
        super().__init__(message)
        self.message = message
        self.code = code


def parse_guest_ids(value, field: str = 'guest_ids') -> list[int]:
    """Liste d'ids (ordre gardé, sans doublon) ; None / '' → []. Autre forme → GuestError."""
    if value in (None, ''):
        return []
    if not isinstance(value, list | tuple):
        raise GuestError(f'{field} must be a list of user ids')
    out: list[int] = []
    for raw in value:
        try:
            uid = int(raw)
        except (TypeError, ValueError):
            raise GuestError(f'{field} must be a list of user ids') from None
        if uid > 0 and uid not in out:
            out.append(uid)
    return out[:MAX_GUESTS]


@dataclass
class GuestCandidate:
    user: object
    kind: str  # mutual | friend | household | table
    is_household: bool


_KIND_ORDER = {'mutual': 0, 'household': 1, 'friend': 2, 'table': 3}


def guest_candidates(user) -> list[GuestCandidate]:
    """
    Personnes qu'on peut mettre dans son plat, dans l'ordre des suggestions du @ :
    amis mutuels, foyer, autres amis, membres de ses tablées (puis prénom). 5 requêtes.
    """
    from accounts.blocks import blocked_user_ids_for
    from accounts.models import Follow
    from accounts.services.household_service import household_member_ids_map
    from tables.models import TableMember

    uid = user.id
    following = set(Follow.objects.filter(follower_id=uid).values_list('following_id', flat=True))
    followers = set(Follow.objects.filter(following_id=uid).values_list('follower_id', flat=True))
    household = household_member_ids_map([uid]).get(uid, set())
    my_tables = TableMember.objects.filter(user_id=uid, status=TableMember.STATUS_ACCEPTED).values('table_id')
    table_mates = set(
        TableMember.objects.filter(table_id__in=my_tables, status=TableMember.STATUS_ACCEPTED).values_list(
            'user_id', flat=True
        )
    )
    ids = (following | followers | household | table_mates) - {uid} - blocked_user_ids_for(user)
    if not ids:
        return []
    users = User.objects.filter(id__in=ids, is_active=True).only(
        'id', 'username', 'first_name', 'last_name', 'email', 'avatar_url'
    )
    out = []
    for u in users:
        if u.id in following and u.id in followers:
            kind = 'mutual'
        elif u.id in household:
            kind = 'household'
        elif u.id in following or u.id in followers:
            kind = 'friend'
        else:
            kind = 'table'
        out.append(GuestCandidate(user=u, kind=kind, is_household=u.id in household))
    out.sort(key=lambda c: (_KIND_ORDER[c.kind], (c.user.first_name or c.user.username or '').lower(), c.user.id))
    return out


def first_name_of(user) -> str:
    return (getattr(user, 'first_name', '') or '').strip() or getattr(user, 'username', '') or ''


def candidate_payload(candidate: GuestCandidate, context: dict | None = None) -> dict:
    from recipes.serializers import UserLightSerializer

    data = dict(UserLightSerializer(candidate.user, context=context or {}).data)
    data.update(
        {
            'first_name': first_name_of(candidate.user),
            'kind': candidate.kind,
            'is_household': candidate.is_household,
        }
    )
    return data


@dataclass
class GuestPlan:
    """Convives validés (avant écriture) : ids notifiés / ids du foyer muets."""

    direct_ids: list[int]
    household_ids: list[int]

    @property
    def all_ids(self) -> list[int]:
        return self.direct_ids + [i for i in self.household_ids if i not in self.direct_ids]

    def __bool__(self) -> bool:
        return bool(self.direct_ids or self.household_ids)


def plan_guests(user, guest_ids, household_guest_ids, *, from_planning: bool) -> GuestPlan:
    """
    Valide les convives demandés (aucune écriture). `household_guest_ids` n'est muet que pour un plat du
    planning partagé (`from_planning`) et un vrai compte du foyer ; sinon il compte comme `guest_ids`.
    """
    wanted = parse_guest_ids(guest_ids)
    wanted_household = parse_guest_ids(household_guest_ids, 'household_guest_ids')
    if not (wanted or wanted_household):
        return GuestPlan([], [])
    candidates = {c.user.id: c for c in guest_candidates(user)}
    household_ids = [i for i in wanted_household if from_planning and i in candidates and candidates[i].is_household]
    direct_ids = [i for i in wanted + wanted_household if i in candidates and i not in household_ids]
    seen: set[int] = set()
    direct_ids = [i for i in direct_ids if not (i in seen or seen.add(i))]
    return GuestPlan(direct_ids[:MAX_GUESTS], household_ids[:MAX_GUESTS])


def add_guests(host, post, plan: GuestPlan) -> list[dict]:
    """
    Ajoute les convives au repas du post (invitations acceptées). Renvoie la trace d'annulation
    [{invitation_id, created, status, source}] ; notifications `post_guest` au commit (direct seulement,
    nouveaux convives seulement). À appeler dans la transaction du log.
    """
    from recipes.models import MealInvitation

    if not plan or not post.meal_plan_id:
        return []
    trace: list[dict] = []
    notify_ids: list[int] = []
    existing = {inv.invitee_id: inv for inv in MealInvitation.objects.filter(meal_plan_id=post.meal_plan_id)}
    for uid in plan.all_ids:
        source = SOURCE_HOUSEHOLD if uid in plan.household_ids and uid not in plan.direct_ids else SOURCE_DIRECT
        inv = existing.get(uid)
        if inv is None:
            inv = MealInvitation.objects.create(
                inviter=host, invitee_id=uid, meal_plan_id=post.meal_plan_id, status='accepted', source=source
            )
            trace.append({'invitation_id': inv.id, 'created': True})
            if source == SOURCE_DIRECT:
                notify_ids.append(uid)
            continue
        if inv.status == 'accepted':
            continue  # déjà là (invitation acceptée du planning) : rien à changer, pas de 2e notification
        trace.append({'invitation_id': inv.id, 'created': False, 'status': inv.status, 'source': inv.source})
        inv.status = 'accepted'
        inv.source = source
        inv.save(update_fields=['status', 'source', 'updated_at'])
        if source == SOURCE_DIRECT:
            notify_ids.append(uid)
    if notify_ids:
        post_id = post.id

        def _send():
            from notifications.services import notify
            from recipes.models import Post

            fresh = Post.objects.filter(pk=post_id, is_published=True).select_related('meal_plan').first()
            if fresh is None:
                return
            for guest in User.objects.filter(id__in=notify_ids, is_active=True):
                notify('post_guest', guest, actor=host, post=fresh)

        transaction.on_commit(_send)
    return trace


def hold_guests(plan: GuestPlan) -> dict | None:
    if not plan:
        return None
    return {'guest_ids': plan.direct_ids, 'household_guest_ids': plan.household_ids}


def apply_held_guests(post) -> list[dict]:
    """Publication d'un plat « rien que pour toi » : ses convives gardés sont ajoutés (et notifiés)."""
    from recipes.models import Post

    held = post.held_guests or {}
    if not held:
        return []
    Post.objects.filter(pk=post.pk).update(held_guests=None)
    post.held_guests = None
    if not post.meal_plan_id or post.user_id is None:
        return []
    # Revalidé à la publication (amitié ou blocage changés entre-temps).
    plan = plan_guests(
        post.user,
        held.get('guest_ids'),
        held.get('household_guest_ids'),
        from_planning=bool(held.get('household_guest_ids')),
    )
    return add_guests(post.user, post, plan)


def undo_guests(post, trace) -> None:
    """« Annuler » le log : convives créés supprimés, convives modifiés remis comme avant, notifs in-app retirées."""
    from accounts.models import Notification
    from recipes.models import MealInvitation

    trace = trace or []
    created = [t['invitation_id'] for t in trace if t.get('created')]
    if created:
        MealInvitation.objects.filter(id__in=created).delete()
    for t in trace:
        if t.get('created'):
            continue
        MealInvitation.objects.filter(id=t.get('invitation_id')).update(
            status=t.get('status') or 'pending', source=t.get('source') or 'invite'
        )
    Notification.objects.filter(notification_type='post_guest', related_post_id=post.id).delete()


def leave_dish(user, post) -> bool:
    """« Me retirer » : le convive quitte le plat (invitation refusée, son repost retiré)."""
    from recipes.models import MealInvitation, PostRepost

    if not post.meal_plan_id:
        return False
    with transaction.atomic():
        changed = MealInvitation.objects.filter(
            meal_plan_id=post.meal_plan_id, invitee=user, status__in=['accepted', 'pending']
        ).update(status='declined')
        if not changed:
            return False
        PostRepost.objects.filter(user=user, original_post=post).delete()
    from accounts.models import Notification

    Notification.objects.filter(user=user, notification_type='post_guest', related_post_id=post.id).update(is_read=True)
    return True


def is_guest_of(user, post) -> bool:
    from recipes.models import MealInvitation

    if not post.meal_plan_id or not getattr(user, 'is_authenticated', False):
        return False
    return MealInvitation.objects.filter(meal_plan_id=post.meal_plan_id, invitee=user, status='accepted').exists()
