"""
Foyer (« Foyer + invités ») : qui mange avec moi au quotidien. Contrat front : .claude-work/specs/household.md.

- Entrée **compte** : lien entre deux utilisateurs amis (suivi dans un sens au moins), consenti par la
  personne ajoutée (`pending` → `accepted`) ; une fois accepté, le lien est réciproque.
- Entrée **nominative** (« Jules », enfant…) : juste un nom, acceptée d'office, partagée avec les comptes
  liés à son auteur (le foyer est commun).
- Le foyer est inclus par défaut dans chaque repas / envie (`recipes.services.diners_service`) et ne compte
  jamais comme convive (stats « Régalés », badge « Hôte », ardoise).
"""

from __future__ import annotations

from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from notifications.services import notify

from ..blocks import are_blocked_either_way
from ..models import Follow, HouseholdMember

User = get_user_model()

MAX_HOUSEHOLD_ENTRIES = 12
MAX_NAME_LENGTH = 60
ACCEPTED = HouseholdMember.STATUS_ACCEPTED
PENDING = HouseholdMember.STATUS_PENDING
DECLINED = HouseholdMember.STATUS_DECLINED


class HouseholdError(Exception):
    def __init__(self, message: str, status_code: int = 400, code: str = 'invalid'):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True)
class HouseholdEntry:
    """Une personne du foyer, vue depuis `viewer_id`."""

    id: int  # HouseholdMember.id : identifiant d'exclusion (`excluded_household_ids`)
    kind: str  # 'user' | 'named'
    name: str
    is_child: bool
    status: str
    user: object | None  # User (entrée compte) : l'autre personne du lien
    owner_id: int
    viewer_id: int

    @property
    def user_id(self) -> int | None:
        return self.user.id if self.user is not None else None

    @property
    def added_by_me(self) -> bool:
        return self.owner_id == self.viewer_id


def _display_name(user) -> str:
    return (getattr(user, 'first_name', '') or '').strip() or user.username


def _entry(row: HouseholdMember, viewer_id: int) -> HouseholdEntry:
    if row.user_id:
        other = row.user if row.owner_id == viewer_id else row.owner
        return HouseholdEntry(
            row.id, 'user', _display_name(other), row.is_child, row.status, other, row.owner_id, viewer_id
        )
    return HouseholdEntry(row.id, 'named', row.name, row.is_child, row.status, None, row.owner_id, viewer_id)


def _household_q(user_id: int) -> Q:
    """Entrées acceptées du foyer de `user_id` : ses liens (dans les deux sens) + noms de son foyer commun."""
    links = HouseholdMember.objects.filter(status=ACCEPTED, user__isnull=False)
    partners_added_me = links.filter(user_id=user_id).values('owner_id')
    partners_i_added = links.filter(owner_id=user_id).values('user_id')
    return Q(status=ACCEPTED) & (
        Q(owner_id=user_id)
        | Q(user_id=user_id)
        | Q(user__isnull=True, owner_id__in=partners_added_me)
        | Q(user__isnull=True, owner_id__in=partners_i_added)
    )


def _dedupe(entries: list[HouseholdEntry]) -> list[HouseholdEntry]:
    seen_users: set[int] = set()
    out = []
    for e in entries:
        if e.user_id is not None:
            if e.user_id in seen_users or e.user_id == e.viewer_id:
                continue
            seen_users.add(e.user_id)
        out.append(e)
    # Comptes d'abord (ordre d'ajout), puis les noms.
    return sorted(out, key=lambda e: (e.kind != 'user', e.id))


def household_for(user_id: int | None, cache: dict | None = None) -> list[HouseholdEntry]:
    """Foyer accepté de `user_id` (1 requête ; mémo optionnel `cache` par requête HTTP)."""
    if not user_id:
        return []
    if cache is not None and user_id in cache:
        return cache[user_id]
    rows = HouseholdMember.objects.filter(_household_q(user_id)).select_related('owner', 'user').order_by('id')
    entries = _dedupe([_entry(r, user_id) for r in rows])
    if cache is not None:
        cache[user_id] = entries
    return entries


def household_member_ids_map(user_ids) -> dict[int, set[int]]:
    """{user_id: ids des comptes de son foyer} pour plusieurs utilisateurs, en 1 requête."""
    user_ids = {u for u in user_ids if u}
    result: dict[int, set[int]] = {u: set() for u in user_ids}
    if not user_ids:
        return result
    for owner_id, member_id in HouseholdMember.objects.filter(
        Q(owner_id__in=user_ids) | Q(user_id__in=user_ids), status=ACCEPTED, user__isnull=False
    ).values_list('owner_id', 'user_id'):
        if owner_id in result:
            result[owner_id].add(member_id)
        if member_id in result:
            result[member_id].add(owner_id)
    return result


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------
def user_payload(user, context: dict | None = None) -> dict:
    from recipes.serializers import UserLightSerializer

    return UserLightSerializer(user, context=context or {}).data


def entry_payload(entry: HouseholdEntry, context: dict | None = None) -> dict:
    return {
        'id': entry.id,
        'kind': entry.kind,
        'name': entry.name,
        'is_child': entry.is_child,
        'status': entry.status,
        'user': user_payload(entry.user, context) if entry.user is not None else None,
        'added_by_me': entry.added_by_me,
        'editable': entry.kind == 'named' and entry.added_by_me,
    }


def household_payload(user, context: dict | None = None) -> dict:
    """GET /household/ : membres acceptés + mes demandes envoyées en attente + demandes reçues."""
    members = household_for(user.id)
    outgoing = [
        _entry(r, user.id)
        for r in HouseholdMember.objects.filter(owner=user, status=PENDING, user__isnull=False).select_related(
            'owner', 'user'
        )
    ]
    incoming = HouseholdMember.objects.filter(user=user, status=PENDING).select_related('owner').order_by('-id')
    return {
        'members': [entry_payload(e, context) for e in members + outgoing],
        'incoming_requests': [
            {'id': r.id, 'user': user_payload(r.owner, context), 'created_at': r.created_at.isoformat()}
            for r in incoming
        ],
        'max_members': MAX_HOUSEHOLD_ENTRIES,
    }


# ---------------------------------------------------------------------------
# Écriture
# ---------------------------------------------------------------------------
def _are_friends(a_id: int, b_id: int) -> bool:
    return Follow.objects.filter(
        Q(follower_id=a_id, following_id=b_id) | Q(follower_id=b_id, following_id=a_id)
    ).exists()


def _check_capacity(owner) -> None:
    own = HouseholdMember.objects.filter(owner=owner).exclude(status=DECLINED).count()
    if own >= MAX_HOUSEHOLD_ENTRIES:
        raise HouseholdError(f'Household is limited to {MAX_HOUSEHOLD_ENTRIES} members', 400, 'household_full')


def _clean_name(name) -> str:
    name = ' '.join(str(name or '').split())
    if not name:
        raise HouseholdError('name or user_id is required', 400, 'name_required')
    return name[:MAX_NAME_LENGTH]


@transaction.atomic
def add_named_member(owner, *, name, is_child=False) -> HouseholdMember:
    _check_capacity(owner)
    return HouseholdMember.objects.create(owner=owner, name=_clean_name(name), is_child=bool(is_child), status=ACCEPTED)


@transaction.atomic
def request_member(owner, target_id) -> tuple[HouseholdMember, str]:
    """
    Ajoute un compte au foyer : demande à accepter (notif `household_request`). Si l'autre m'avait déjà
    demandé, c'est une acceptation. Retourne (lien, action) : request_sent | accepted | already_member |
    already_requested.
    """
    try:
        target = User.objects.get(id=int(target_id))
    except (TypeError, ValueError, User.DoesNotExist):
        raise HouseholdError('User not found', 404, 'user_not_found') from None
    if target.id == owner.id:
        raise HouseholdError('You are already in your household', 400, 'self')
    if are_blocked_either_way(owner, target):
        raise HouseholdError('Action impossible', 403, 'blocked')
    if not _are_friends(owner.id, target.id):
        raise HouseholdError('Only friends can join your household', 403, 'not_friends')

    reverse = HouseholdMember.objects.select_for_update().filter(owner=target, user=owner).first()
    if reverse and reverse.status == ACCEPTED:
        return reverse, 'already_member'
    if reverse and reverse.status == PENDING:
        return respond(owner, reverse.id, accept=True), 'accepted'

    link = HouseholdMember.objects.select_for_update().filter(owner=owner, user=target).first()
    if link and link.status == ACCEPTED:
        return link, 'already_member'
    if link and link.status == PENDING:
        return link, 'already_requested'
    _check_capacity(owner)
    if link:  # refusé : nouvelle demande
        link.status, link.responded_at = PENDING, None
        link.save(update_fields=['status', 'responded_at', 'updated_at'])
    else:
        link = HouseholdMember.objects.create(owner=owner, user=target, status=PENDING)
    link_id = link.id
    transaction.on_commit(
        lambda: notify(
            'household_request',
            target,
            actor=owner,
            data={'household_link_id': link_id},
            dedupe_key=f'household_request:{link_id}:{timezone.now():%Y%m%d}',
        )
    )
    return link, 'request_sent'


@transaction.atomic
def respond(user, link_id, *, accept: bool) -> HouseholdMember:
    """La personne ajoutée accepte (notif `household_accepted` à l'auteur) ou refuse (silencieux)."""
    link = (
        HouseholdMember.objects.select_for_update()
        .select_related('owner', 'user')
        .filter(id=link_id, user=user, status=PENDING)
        .first()
    )
    if not link:
        raise HouseholdError('Request not found', 404, 'not_found')
    link.status = ACCEPTED if accept else DECLINED
    link.responded_at = timezone.now()
    link.save(update_fields=['status', 'responded_at', 'updated_at'])
    if accept:
        owner, link_id = link.owner, link.id
        transaction.on_commit(
            lambda: notify('household_accepted', owner, actor=user, data={'household_link_id': link_id})
        )
    return link


def _removable(user):
    """Entrées que `user` peut retirer : les siennes (noms, demandes, liens) + les liens acceptés vers lui."""
    return HouseholdMember.objects.filter(Q(owner=user) | Q(user=user, status=ACCEPTED))


@transaction.atomic
def remove_entry(user, entry_id) -> None:
    row = _removable(user).filter(id=entry_id).first()
    if not row:
        raise HouseholdError('Household member not found', 404, 'not_found')
    row.delete()  # caches stats / ardoises : signaux `stats` (HouseholdMember)


@transaction.atomic
def update_named_member(user, entry_id, *, name=None, is_child=None) -> HouseholdMember:
    row = HouseholdMember.objects.filter(id=entry_id, owner=user, user__isnull=True).first()
    if not row:
        raise HouseholdError('Household member not found', 404, 'not_found')
    fields = []
    if name is not None:
        row.name = _clean_name(name)
        fields.append('name')
    if is_child is not None:
        row.is_child = bool(is_child)
        fields.append('is_child')
    if fields:
        row.save(update_fields=[*fields, 'updated_at'])
    return row


@transaction.atomic
def replace_household(user, members) -> None:
    """
    PUT /household/ : liste déclarative `[{id?, user_id?, name?, is_child?}]`.
    - entrée connue (`id`) : gardée (nom / enfant modifiables pour mes entrées nominatives) ;
    - nouvelle entrée : `user_id` → demande au compte, sinon `name` → entrée nominative ;
    - mes entrées / liens absents de la liste sont retirés (les noms ajoutés par un autre membre restent).
    """
    if not isinstance(members, list):
        raise HouseholdError('members must be a list', 400, 'invalid')
    keep_ids: set[int] = set()
    keep_user_ids: set[int] = set()
    to_create = []
    for raw in members:
        if not isinstance(raw, dict):
            raise HouseholdError('members must be objects', 400, 'invalid')
        if raw.get('id') is not None:
            try:
                keep_ids.add(int(raw['id']))
            except (TypeError, ValueError):
                raise HouseholdError('id must be an integer', 400, 'invalid') from None
            to_create.append(('update', raw))
        elif raw.get('user_id') is not None:
            try:
                keep_user_ids.add(int(raw['user_id']))
            except (TypeError, ValueError):
                raise HouseholdError('user_id must be an integer', 400, 'invalid') from None
            to_create.append(('user', raw))
        else:
            to_create.append(('named', raw))

    for row in _removable(user).exclude(id__in=keep_ids):
        other = row.user_id if row.owner_id == user.id else row.owner_id
        if row.user_id and other in keep_user_ids:
            keep_ids.add(row.id)  # même personne renvoyée par user_id : on garde le lien existant
            continue
        remove_entry(user, row.id)

    for kind, raw in to_create:
        if kind == 'update':
            if HouseholdMember.objects.filter(id=int(raw['id']), owner=user, user__isnull=True).exists():
                update_named_member(user, int(raw['id']), name=raw.get('name'), is_child=raw.get('is_child'))
        elif kind == 'user':
            request_member(user, raw['user_id'])
        else:
            add_named_member(user, name=raw.get('name'), is_child=raw.get('is_child', False))


def clean_excluded_ids(value) -> list[int]:
    """`excluded_household_ids` écrit par l'app : liste d'entiers uniques (ids inconnus ignorés au calcul)."""
    if value in (None, ''):
        return []
    if not isinstance(value, list | tuple):
        raise HouseholdError('excluded_household_ids must be a list of ids', 400, 'invalid')
    out: list[int] = []
    for v in value:
        try:
            i = int(v)
        except (TypeError, ValueError):
            raise HouseholdError('excluded_household_ids must be a list of ids', 400, 'invalid') from None
        if i not in out:
            out.append(i)
    return out[:50]
