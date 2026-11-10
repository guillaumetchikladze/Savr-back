"""
« Les tablées » : règles métier (contrat front : .claude-work/specs/tables.md).

- Une tablée = 2 à 8 personnes (`TABLE_MAX_MEMBERS`, invitations en attente comprises) ; chacun peut être
  dans `TABLES_MAX_PER_USER` (5) tablées acceptées. Seul·e, on attend qu'une invitation soit acceptée.
- Invitations **consenties** : un membre invite un·e ami·e (suivi approuvé dans un sens ou l'autre, sans
  blocage) ; l'invité·e accepte ou refuse (un refus supprime l'invitation, sans notification).
  Notifs : `table_invitation` (« Léa t'invite à la tablée « Les colocs » ») et `table_joined`.
- Le ou la créateur·rice (`owner`) renomme, retire un membre ou annule une invitation, supprime la tablée ;
  tout le monde peut partir (le rôle passe au membre le plus ancien ; dernier départ = tablée supprimée).
- Semaine : compteur **collectif** (« 3 sur 5 ont cuisiné cette semaine », définition « cuisiné » de
  `cooked_meal_plans_of_users`), plats et cuisines distincts de la tablée, dernier post **visible** de
  chacun (`post_visibility.visible_posts` du viewer), et rituel « Qui cuisine quoi cette semaine ? » :
  envies non datées de la semaine des membres qui ont choisi de partager leur panier (`share_basket`).
  Jamais de classement ni de compteur individuel.
- Blocage : l'appartenance ne change pas ; deux membres en blocage ne voient ni le profil, ni les posts,
  ni le panier l'un de l'autre dans la tablée (ils restent comptés dans le compteur collectif anonyme).
"""

from __future__ import annotations

import re
from datetime import date as date_type
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.blocks import are_blocked_either_way, blocked_user_ids_for
from accounts.models import Follow
from notifications.services import notify

from .models import Table, TableMember

User = get_user_model()

OWNER = TableMember.ROLE_OWNER
MEMBER = TableMember.ROLE_MEMBER
INVITED = TableMember.STATUS_INVITED
ACCEPTED = TableMember.STATUS_ACCEPTED

NAME_MAX_LENGTH = 60
EMOJI_MAX_LENGTH = 16
RITUAL_MAX_ITEMS_PER_MEMBER = 10
RITUAL_TITLE = 'Qui cuisine quoi cette semaine ?'
_COLOR_RE = re.compile(r'^#[0-9a-fA-F]{6}$')


def min_members() -> int:
    return int(getattr(settings, 'TABLE_MIN_MEMBERS', 2))


def max_members() -> int:
    return int(getattr(settings, 'TABLE_MAX_MEMBERS', 8))


def max_tables_per_user() -> int:
    return int(getattr(settings, 'TABLES_MAX_PER_USER', 5))


class TableError(Exception):
    def __init__(self, message: str, status_code: int = 400, code: str = 'invalid'):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


# ---------------------------------------------------------------------------
# Lecture / accès
# ---------------------------------------------------------------------------
def membership(user, table_id) -> TableMember | None:
    try:
        tid = int(table_id)
    except (TypeError, ValueError):
        return None
    return TableMember.objects.select_related('table').filter(table_id=tid, user=user).first()


def get_membership_or_404(user, table_id, *, accepted: bool = True) -> TableMember:
    """Tablée vue par `user` : 404 (jamais 403) si on n'en est pas membre (ou invité·e si `accepted=False`)."""
    m = membership(user, table_id)
    if m is None or (accepted and m.status != ACCEPTED):
        raise TableError('Table not found', 404, 'table_not_found')
    return m


def accepted_tables_count(user) -> int:
    return TableMember.objects.filter(user=user, status=ACCEPTED).count()


def shared_tables(viewer, other) -> list[dict]:
    """
    Tablées où le lecteur **et** `other` sont membres acceptés (profil d'un ami : « Vos tablées »).
    Seulement celles dont le lecteur fait partie : jamais les autres tablées de `other`, ni ses invitations,
    ni les membres. Vide pour soi-même ou en cas de blocage. Une requête.
    """
    if viewer is None or other is None or viewer.id == other.id:
        return []
    if are_blocked_either_way(viewer, other):
        return []
    mine = TableMember.objects.filter(user=viewer, status=ACCEPTED).values('table_id')
    tables = (
        Table.objects.filter(id__in=mine, memberships__user=other, memberships__status=ACCEPTED)
        .order_by('created_at', 'id')
        .distinct()
    )
    return [{'id': t.id, 'name': t.name, 'emoji': t.emoji, 'color': t.color} for t in tables]


def are_friends(a_id: int, b_id: int) -> bool:
    """Ami·e = suivi approuvé dans un sens ou l'autre (le blocage est vérifié à part)."""
    return Follow.objects.filter(
        Q(follower_id=a_id, following_id=b_id) | Q(follower_id=b_id, following_id=a_id)
    ).exists()


def _clean_name(value) -> str:
    name = ' '.join(str(value or '').split())
    if not name:
        raise TableError('name is required', 400, 'name_required')
    return name[:NAME_MAX_LENGTH]


def _clean_emoji(value) -> str:
    return str(value or '').strip()[:EMOJI_MAX_LENGTH]


def _clean_color(value) -> str:
    color = str(value or '').strip()
    if color and not _COLOR_RE.match(color):
        raise TableError('color must be #RRGGBB', 400, 'invalid_color')
    return color


def _parse_ids(value, field: str = 'user_ids') -> list[int]:
    if value in (None, ''):
        return []
    if not isinstance(value, list | tuple):
        value = [value]
    out: list[int] = []
    for v in value:
        if isinstance(v, bool):
            raise TableError(f'{field} must be a list of ids', 400, 'invalid')
        try:
            i = int(v)
        except (TypeError, ValueError):
            raise TableError(f'{field} must be a list of ids', 400, 'invalid') from None
        if i not in out:
            out.append(i)
    return out


def _check_user_capacity(user) -> None:
    if accepted_tables_count(user) >= max_tables_per_user():
        raise TableError(f'You can be in up to {max_tables_per_user()} tables', 400, 'too_many_tables')


# ---------------------------------------------------------------------------
# Écriture
# ---------------------------------------------------------------------------
@transaction.atomic
def create_table(user, *, name, emoji='', color='', member_ids=None) -> tuple[Table, dict]:
    name, emoji, color = _clean_name(name), _clean_emoji(emoji), _clean_color(color)
    ids = _parse_ids(member_ids, 'member_ids')
    _check_user_capacity(user)
    table = Table.objects.create(name=name, emoji=emoji, color=color, created_by=user)
    TableMember.objects.create(table=table, user=user, role=OWNER, status=ACCEPTED, joined_at=timezone.now())
    results = invite(user, table.id, ids) if ids else {}
    return table, results


@transaction.atomic
def update_table(user, table_id, data) -> Table:
    m = get_membership_or_404(user, table_id)
    if m.role != OWNER:
        raise TableError('Only the table owner can edit it', 403, 'not_owner')
    table = m.table
    fields = []
    if 'name' in data:
        table.name = _clean_name(data.get('name'))
        fields.append('name')
    if 'emoji' in data:
        table.emoji = _clean_emoji(data.get('emoji'))
        fields.append('emoji')
    if 'color' in data:
        table.color = _clean_color(data.get('color'))
        fields.append('color')
    if fields:
        table.save(update_fields=[*fields, 'updated_at'])
    return table


@transaction.atomic
def delete_table(user, table_id) -> None:
    m = get_membership_or_404(user, table_id)
    if m.role != OWNER:
        raise TableError('Only the table owner can delete it', 403, 'not_owner')
    m.table.delete()


@transaction.atomic
def invite(inviter, table_id, user_ids) -> dict:
    """
    Invite des ami·es (`{user_id: statut}` : invited | already_member | already_invited). Refus explicites :
    utilisateur introuvable (404), soi-même, pas ami·e (403 `not_friends`), blocage (403 `blocked`),
    tablée pleine (400 `table_full`, invitations en attente comprises).
    """
    m = get_membership_or_404(inviter, table_id)
    ids = _parse_ids(user_ids)
    if not ids:
        raise TableError('user_ids is required', 400, 'user_ids_required')
    table = Table.objects.select_for_update().get(pk=m.table_id)
    existing = dict(TableMember.objects.filter(table=table).values_list('user_id', 'status'))
    targets = {u.id: u for u in User.objects.filter(id__in=ids, is_active=True)}
    results: dict[int, str] = {}
    to_invite = []
    for uid in ids:
        if uid in existing:
            results[uid] = 'already_member' if existing[uid] == ACCEPTED else 'already_invited'
            continue
        target = targets.get(uid)
        if target is None:
            raise TableError('User not found', 404, 'user_not_found')
        if uid == inviter.id:
            raise TableError('You are already at this table', 400, 'self')
        if are_blocked_either_way(inviter, target):
            raise TableError('Action impossible', 403, 'blocked')
        if not are_friends(inviter.id, uid):
            raise TableError('Only friends can be invited to a table', 403, 'not_friends')
        to_invite.append(target)
    if len(existing) + len(to_invite) > max_members():
        raise TableError(f'A table is limited to {max_members()} people', 400, 'table_full')

    for target in to_invite:
        TableMember.objects.create(table=table, user=target, role=MEMBER, status=INVITED, invited_by=inviter)
        results[target.id] = 'invited'
        _notify_on_commit(
            'table_invitation',
            target,
            actor=inviter,
            table=table,
            dedupe_key=f'table_invitation:{table.id}:{target.id}:{timezone.now():%Y%m%d}',
        )
    return results


@transaction.atomic
def respond(user, table_id, *, accept: bool) -> TableMember | None:
    """L'invité·e accepte (notif `table_joined` aux membres) ou refuse (invitation supprimée, silencieux)."""
    m = get_membership_or_404(user, table_id, accepted=False)
    if m.status != INVITED:
        raise TableError('No pending invitation', 404, 'invitation_not_found')
    if not accept:
        m.delete()
        return None
    _check_user_capacity(user)
    m.status = ACCEPTED
    m.joined_at = timezone.now()
    m.save(update_fields=['status', 'joined_at', 'updated_at'])
    others = list(
        User.objects.filter(
            table_memberships__table_id=m.table_id, table_memberships__status=ACCEPTED, is_active=True
        ).exclude(id=user.id)
    )
    blocked = blocked_user_ids_for(user)
    for other in others:
        if other.id in blocked:
            continue
        _notify_on_commit(
            'table_joined', other, actor=user, table=m.table, dedupe_key=f'table_joined:{m.id}:{other.id}'
        )
    return m


@transaction.atomic
def leave(user, table_id) -> None:
    """Quitter (membre) ; le rôle de créateur·rice passe au membre le plus ancien ; dernier départ = suppression."""
    m = get_membership_or_404(user, table_id)
    table = Table.objects.select_for_update().get(pk=m.table_id)
    was_owner = m.role == OWNER
    m.delete()
    remaining = TableMember.objects.filter(table=table, status=ACCEPTED).order_by('joined_at', 'id')
    successor = remaining.first()
    if successor is None:
        table.delete()  # plus aucun membre : invitations en attente supprimées avec la tablée
        return
    if was_owner:
        successor.role = OWNER
        successor.save(update_fields=['role', 'updated_at'])


@transaction.atomic
def remove_member(owner, table_id, user_id) -> None:
    """Le ou la créateur·rice retire un membre ou annule une invitation (silencieux)."""
    m = get_membership_or_404(owner, table_id)
    if m.role != OWNER:
        raise TableError('Only the table owner can remove members', 403, 'not_owner')
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        raise TableError('Member not found', 404, 'member_not_found') from None
    if uid == owner.id:
        raise TableError('Use leave to quit your own table', 400, 'self')
    deleted, _ = TableMember.objects.filter(table_id=m.table_id, user_id=uid).delete()
    if not deleted:
        raise TableError('Member not found', 404, 'member_not_found')


@transaction.atomic
def set_my_settings(user, table_id, data) -> TableMember:
    m = get_membership_or_404(user, table_id)
    if 'share_basket' in data:
        value = data.get('share_basket')
        if not isinstance(value, bool):
            raise TableError('share_basket must be a boolean', 400, 'invalid')
        if m.share_basket != value:
            m.share_basket = value
            m.save(update_fields=['share_basket', 'updated_at'])
    return m


def cancel_invitations_between(user_a, user_b) -> int:
    """Blocage : les invitations en attente envoyées par l'un à l'autre sont annulées (l'appartenance reste)."""
    deleted, _ = TableMember.objects.filter(
        Q(user_id=user_a.id, invited_by_id=user_b.id) | Q(user_id=user_b.id, invited_by_id=user_a.id),
        status=INVITED,
    ).delete()
    return deleted


def _notify_on_commit(type_key: str, recipient, *, actor, table: Table, dedupe_key: str) -> None:
    table_id, table_name = table.id, table.name
    transaction.on_commit(
        lambda: notify(
            type_key,
            recipient,
            actor=actor,
            context={'table': table_name},
            data={'table_id': table_id},
            dedupe_key=dedupe_key,
        )
    )


# ---------------------------------------------------------------------------
# Semaine de la tablée (compteur collectif, plats, cuisines)
# ---------------------------------------------------------------------------
def week_bounds(week_start: date_type) -> tuple[date_type, date_type]:
    return week_start, week_start + timedelta(days=6)


def cooked_member_ids(member_ids, start: date_type, end: date_type) -> set[int]:
    """Membres ayant cuisiné au moins un plat sur [start, end] (définition unique de « cuisiné »)."""
    from recipes.services.cooked_service import cooked_meal_plans_of_users

    ids = list(member_ids)
    if not ids:
        return set()
    return set(cooked_meal_plans_of_users(ids, start, end).values_list('user_id', flat=True).distinct())


def week_dishes_and_cuisines(member_ids, start: date_type, end: date_type) -> tuple[int, int]:
    """(plats distincts, cuisines distinctes) cuisinés par la tablée : collectif, jamais par personne."""
    from recipes.models import MealPlanRecipeBatch, Post
    from recipes.services.cooked_service import cooked_meal_plans_of_users
    from recipes.services.dish_service import dish_key
    from stats.cuisines import CUISINES, NON_COLLECTIBLE

    ids = list(member_ids)
    if not ids:
        return 0, 0
    plans = cooked_meal_plans_of_users(ids, start, end)
    labels = dict(plans.values_list('id', 'custom_label'))
    if not labels:
        return 0, 0
    recipes: dict[int, set[int]] = {}
    cuisines: set[str] = set()
    valid = {c.code for c in CUISINES} - set(NON_COLLECTIBLE)
    for meal_id, recipe_id, cuisine in MealPlanRecipeBatch.objects.filter(
        meal_plan_id__in=list(labels), recipe_batch__recipe__isnull=False
    ).values_list('meal_plan_id', 'recipe_batch__recipe_id', 'recipe_batch__recipe__cuisine'):
        recipes.setdefault(meal_id, set()).add(recipe_id)
        if cuisine in valid:
            cuisines.add(cuisine)
    post_labels: dict[int, str] = {}
    for meal_id, label in (
        Post.objects.filter(meal_plan_id__in=list(labels))
        .exclude(label='')
        .order_by('id')
        .values_list('meal_plan_id', 'label')
    ):
        post_labels.setdefault(meal_id, label)
    dishes: set[str] = set()
    for meal_id, custom_label in labels.items():
        if recipes.get(meal_id):
            dishes.update(dish_key(rid) for rid in recipes[meal_id])
            continue
        key = dish_key(label=post_labels.get(meal_id) or custom_label)
        if key:
            dishes.add(key)
    return len(dishes), len(cuisines)


def week_headline(cooked: int, total: int) -> str:
    """« 3 sur 5 ont cuisiné cette semaine » ; jamais de reproche quand personne n'a encore cuisiné."""
    if cooked <= 0:
        return 'Qui ouvre le bal cette semaine ?'
    if total >= 2 and cooked >= total:
        return 'Tout le monde a cuisiné cette semaine'
    verb = 'a' if cooked == 1 else 'ont'
    return f'{cooked} sur {total} {verb} cuisiné cette semaine'


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------
def _user_light(users, request) -> dict[int, dict]:
    from recipes.serializers import UserLightSerializer

    context = {'request': request}
    return {u.id: UserLightSerializer(u, context=context).data for u in users}


def _display_name(user) -> str:
    from core.public_links import public_display_name

    return public_display_name(user)


def table_header(table: Table, m: TableMember, accepted_count: int, invited_count: int) -> dict:
    return {
        'id': table.id,
        'name': table.name,
        'emoji': table.emoji,
        'color': table.color,
        'created_at': table.created_at.isoformat() if table.created_at else None,
        'member_count': accepted_count,
        'invited_count': invited_count,
        'max_members': max_members(),
        'min_members': min_members(),
        'me': {
            'role': m.role,
            'status': m.status,
            'share_basket': m.share_basket,
            'joined_at': m.joined_at.isoformat() if m.joined_at else None,
            'is_owner': m.role == OWNER,
        },
    }


def tables_list_payload(user, request=None) -> dict:
    """GET /api/tables/ : mes tablées (acceptées) + invitations reçues. Requêtes constantes."""
    mine = list(
        TableMember.objects.filter(user=user).select_related('table', 'invited_by').order_by('created_at', 'id')
    )
    table_ids = [m.table_id for m in mine]
    blocked = blocked_user_ids_for(user)
    rows = list(
        TableMember.objects.filter(table_id__in=table_ids, user__is_active=True)
        .select_related('user')
        .order_by('joined_at', 'id')
    )
    users = _user_light({r.user for r in rows} | {m.invited_by for m in mine if m.invited_by_id}, request)
    by_table: dict[int, list[TableMember]] = {}
    for r in rows:
        by_table.setdefault(r.table_id, []).append(r)

    tables, invitations = [], []
    for m in mine:
        members = by_table.get(m.table_id, [])
        accepted = [r for r in members if r.status == ACCEPTED]
        payload = table_header(m.table, m, len(accepted), len(members) - len(accepted))
        payload['members_preview'] = [
            users[r.user_id] for r in accepted if r.user_id not in blocked and r.user_id != user.id
        ][:5]
        if m.status == ACCEPTED:
            tables.append(payload)
        else:
            payload['invited_by'] = users.get(m.invited_by_id) if m.invited_by_id else None
            invitations.append(payload)
    return {
        'tables': tables,
        'invitations': invitations,
        'max_tables': max_tables_per_user(),
        'max_members': max_members(),
    }


def _latest_posts(viewer, member_ids) -> dict:
    """Dernier post **visible par le viewer** de chaque membre (DISTINCT ON, requêtes constantes)."""
    from recipes.models import Post
    from recipes.services.cooked_service import post_display_title
    from recipes.services.dish_service import _first_photos, post_dish
    from recipes.services.post_visibility import visible_posts

    ids = list(member_ids)
    if not ids:
        return {}
    latest_ids = list(
        visible_posts(viewer)
        .filter(user_id__in=ids)
        .order_by('user_id', '-created_at', '-id')
        .distinct('user_id')
        .values_list('id', flat=True)
    )
    if not latest_ids:
        return {}
    posts = list(
        Post.objects.filter(id__in=latest_ids)
        .select_related('meal_plan', 'recipe_batch__recipe')
        .prefetch_related('meal_plan__meal_plan_recipe_batches__recipe_batch__recipe')
    )
    photos = _first_photos([p.id for p in posts])
    out = {}
    for p in posts:
        photo = photos.get(p.id)
        out[p.user_id] = {
            'id': p.id,
            'title': post_display_title(p),
            'dish': post_dish(p),
            'photo_url': photo.image_url if photo is not None else None,
            'date': p.meal_plan.date.isoformat() if p.meal_plan_id and p.meal_plan else None,
            'created_at': p.created_at.isoformat() if p.created_at else None,
        }
    return out


def _ritual(viewer, sharers: list, week_start: date_type, users: dict) -> dict:
    """« Qui cuisine quoi cette semaine ? » : envies non datées des membres qui partagent leur panier."""
    from recipes.models import BasketItem

    by_user: dict[int, list[dict]] = {}
    if sharers:
        for item in (
            BasketItem.objects.filter(
                user_id__in=sharers, week_start=week_start, meal_plan__isnull=True, carried_over_to__isnull=True
            )
            .select_related('recipe')
            .order_by('user_id', 'position', 'id')
        ):
            items = by_user.setdefault(item.user_id, [])
            if len(items) < RITUAL_MAX_ITEMS_PER_MEMBER:
                items.append(
                    {
                        'id': item.id,
                        'title': item.display_title,
                        'recipe_id': item.recipe_id,
                        'image_url': item.recipe.image_url if item.recipe_id and item.recipe else None,
                    }
                )
    return {
        'title': RITUAL_TITLE,
        'week_start': week_start.isoformat(),
        'entries': [{'user': users[uid], 'items': by_user[uid]} for uid in sharers if by_user.get(uid)],
        'sharing_count': len(sharers),
    }


def table_detail_payload(user, table_id, *, week_start: date_type, request=None) -> dict:
    """
    GET /api/tables/<id>/ : en-tête, membres (dernier post visible), semaine collective, rituel.
    Invité·e : en-tête et membres seulement (`week` / `ritual` à null) pour décider d'accepter.
    """
    m = get_membership_or_404(user, table_id, accepted=False)
    table = m.table
    rows = list(
        TableMember.objects.filter(table=table, user__is_active=True)
        .select_related('user', 'invited_by')
        .order_by('joined_at', 'id')
    )
    accepted = [r for r in rows if r.status == ACCEPTED]
    blocked = blocked_user_ids_for(user)
    visible_rows = [r for r in rows if r.user_id not in blocked]
    users = _user_light([r.user for r in visible_rows] + ([m.invited_by] if m.invited_by_id else []), request)
    payload = table_header(table, m, len(accepted), len(rows) - len(accepted))
    if m.status != ACCEPTED:
        payload['invited_by'] = users.get(m.invited_by_id) if m.invited_by_id else None
        payload['members'] = [
            {'user': users[r.user_id], 'display_name': _display_name(r.user), 'role': r.role, 'status': r.status}
            for r in visible_rows
            if r.status == ACCEPTED
        ]
        payload['week'] = None
        payload['ritual'] = None
        return payload

    start, end = week_bounds(week_start)
    accepted_ids = [r.user_id for r in accepted]
    visible_accepted = [r.user_id for r in accepted if r.user_id not in blocked]
    latest = _latest_posts(user, visible_accepted)
    payload['members'] = [
        {
            'user': users[r.user_id],
            'display_name': _display_name(r.user),
            'role': r.role,
            'status': r.status,
            'joined_at': r.joined_at.isoformat() if r.joined_at else None,
            'shares_basket': r.share_basket if r.status == ACCEPTED else False,
            'latest_post': latest.get(r.user_id) if r.status == ACCEPTED else None,
        }
        for r in visible_rows
    ]
    cooked = len(cooked_member_ids(accepted_ids, start, end))
    dishes, cuisines = week_dishes_and_cuisines(accepted_ids, start, end)
    payload['week'] = {
        'start': start.isoformat(),
        'end': end.isoformat(),
        'cooked_members': cooked,
        'member_count': len(accepted_ids),
        'headline': week_headline(cooked, len(accepted_ids)),
        'dishes_count': dishes,
        'cuisines_count': cuisines,
    }
    sharers = [r.user_id for r in accepted if r.share_basket and r.user_id not in blocked]
    payload['ritual'] = _ritual(user, sharers, start, users)
    return payload


# ---------------------------------------------------------------------------
# Push du dimanche (via notifications.retention)
# ---------------------------------------------------------------------------
def best_table_week(user, start: date_type, end: date_type) -> dict | None:
    """
    Tablée à mettre en avant dans la push du dimanche : celle où le plus de membres ont cuisiné
    (au moins 1, au moins `TABLE_MIN_MEMBERS` membres). None → pas de push (jamais de « 0 sur 5 »).
    """
    table_ids = list(TableMember.objects.filter(user=user, status=ACCEPTED).values_list('table_id', flat=True))
    if not table_ids:
        return None
    members: dict[int, list[int]] = {}
    for tid, uid in TableMember.objects.filter(
        table_id__in=table_ids, status=ACCEPTED, user__is_active=True
    ).values_list('table_id', 'user_id'):
        members.setdefault(tid, []).append(uid)
    all_ids = {uid for ids in members.values() for uid in ids}
    cooked_ids = cooked_member_ids(all_ids, start, end)
    names = dict(Table.objects.filter(id__in=table_ids).values_list('id', 'name'))
    best = None
    for tid in sorted(members):
        ids = members[tid]
        if len(ids) < min_members():
            continue
        cooked = sum(1 for uid in ids if uid in cooked_ids)
        if cooked == 0:
            continue
        candidate = (cooked / len(ids), cooked, -tid)
        if best is None or candidate > best[0]:
            best = (candidate, {'table_id': tid, 'table': names.get(tid, ''), 'cooked': cooked, 'total': len(ids)})
    return best[1] if best else None
