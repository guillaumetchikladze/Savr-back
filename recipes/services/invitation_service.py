"""Service sync pour les invitations meal plan."""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AbstractBaseUser
from django.db import transaction
from django.db.models import Q

from accounts.models import Follow, Notification, PushDevice
from accounts.services.expo_push import send_expo_push_notifications
from chat.services.tool_schemas import CompliceSummary, MutationProposal
from recipes.models import MealInvitation, MealPlan
from recipes.utils import get_accessible_meal_plan_filter

User = get_user_model()


def _complice_ids_for_user(user: AbstractBaseUser) -> set[int]:
    following_ids = Follow.objects.filter(follower=user).values_list('following_id', flat=True)
    followers_ids = Follow.objects.filter(following=user).values_list('follower_id', flat=True)
    return set(list(following_ids) + list(followers_ids))


def resolve_complices_by_name(
    user: AbstractBaseUser,
    names: list[str],
) -> list[CompliceSummary]:
    """Résout des usernames vers des complices (follow mutuel ou au moins un lien follow)."""
    complice_ids = _complice_ids_for_user(user)
    if not complice_ids or not names:
        return []

    results = []
    seen = set()
    for name in names:
        name = (name or '').strip()
        if not name:
            continue
        candidates = User.objects.filter(
            id__in=complice_ids,
        ).filter(
            Q(username__iexact=name) | Q(username__icontains=name)
        )[:5]
        for u in candidates:
            if u.id in seen:
                continue
            seen.add(u.id)
            results.append(CompliceSummary(id=u.id, username=u.username))
    return results


def _normalize_meal_plan_ids(meal_plan_ids: list[int] | int | None) -> list[int]:
    if meal_plan_ids is None:
        return []
    if isinstance(meal_plan_ids, int):
        return [meal_plan_ids]
    seen: set[int] = set()
    normalized: list[int] = []
    for raw_id in meal_plan_ids:
        meal_plan_id = int(raw_id)
        if meal_plan_id in seen:
            continue
        seen.add(meal_plan_id)
        normalized.append(meal_plan_id)
    return normalized


def _format_invitation_subtitle(meal_plans: list[MealPlan]) -> str:
    if not meal_plans:
        return 'Inviter des amis'
    if len(meal_plans) == 1:
        meal_plan = meal_plans[0]
        return f'{meal_plan.get_meal_time_display()} du {meal_plan.date.strftime("%d/%m/%Y")}'
    first_date = meal_plans[0].date.strftime('%d/%m')
    last_date = meal_plans[-1].date.strftime('%d/%m/%Y')
    return f'{len(meal_plans)} repas · du {first_date} au {last_date}'


def propose_meal_invitation(
    user: AbstractBaseUser,
    meal_plan_ids: list[int] | int,
    invitee_ids: list[int],
) -> MutationProposal:
    normalized_ids = _normalize_meal_plan_ids(meal_plan_ids)
    if not normalized_ids:
        raise ValueError('Aucun repas sélectionné.')

    accessible_filter = get_accessible_meal_plan_filter(user)
    meal_plans = list(
        MealPlan.objects.filter(accessible_filter, id__in=normalized_ids)
        .select_related('user')
        .order_by('date', 'meal_time', 'id')
    )
    found_ids = {meal_plan.id for meal_plan in meal_plans}
    missing_ids = [meal_plan_id for meal_plan_id in normalized_ids if meal_plan_id not in found_ids]
    if missing_ids:
        raise ValueError('Un ou plusieurs repas sont introuvables ou inaccessibles.')
    if any(meal_plan.user_id != user.id for meal_plan in meal_plans):
        raise PermissionError('Seul le propriétaire peut inviter des amis.')

    seen_slots: set[tuple] = set()
    unique_meal_plans: list[MealPlan] = []
    for meal_plan in meal_plans:
        slot_key = (meal_plan.date, meal_plan.meal_time)
        if slot_key in seen_slots:
            continue
        seen_slots.add(slot_key)
        unique_meal_plans.append(meal_plan)
    meal_plans = unique_meal_plans
    if not meal_plans:
        raise ValueError('Aucun repas sélectionné.')

    complice_ids = _complice_ids_for_user(user)
    valid_ids = [uid for uid in invitee_ids if uid in complice_ids]
    if not valid_ids:
        raise ValueError('Aucun ami valide trouvé.')

    invitees = list(User.objects.filter(id__in=valid_ids))
    usernames = [u.username for u in invitees]

    warnings: list[str] = []
    already_invited_by_plan: list[str] = []
    for meal_plan in meal_plans:
        already_invited = list(
            MealInvitation.objects.filter(
                meal_plan=meal_plan,
                invitee_id__in=valid_ids,
                status__in=['pending', 'accepted'],
            ).values_list('invitee__username', flat=True)
        )
        if already_invited:
            meal_label = meal_plan.get_meal_time_display()
            date_str = meal_plan.date.strftime('%d/%m')
            already_invited_by_plan.append(
                f'{meal_label} {date_str}: {", ".join(already_invited)}'
            )
    if already_invited_by_plan:
        warnings.append('Déjà invité(s) — ' + ' · '.join(already_invited_by_plan))
        if len(already_invited_by_plan) == len(meal_plans):
            all_already = True
            for meal_plan in meal_plans:
                invited_usernames = set(
                    MealInvitation.objects.filter(
                        meal_plan=meal_plan,
                        invitee_id__in=valid_ids,
                        status__in=['pending', 'accepted'],
                    ).values_list('invitee__username', flat=True)
                )
                expected = {u.username for u in invitees}
                if not expected.issubset(invited_usernames):
                    all_already = False
                    break
            if all_already:
                raise ValueError(
                    'Ces amis sont déjà invités pour ces repas. '
                    'Pour savoir qui est invité, consulte get_meal_plans (champ invitees).'
                )

    meal_plan_payload = [
        {
            'meal_plan_id': meal_plan.id,
            'date': meal_plan.date.isoformat(),
            'meal_time': meal_plan.meal_time,
        }
        for meal_plan in meal_plans
    ]
    first_meal_plan = meal_plans[0]

    return MutationProposal(
        card_type='meal_invitation',
        title='Inviter des amis',
        subtitle=_format_invitation_subtitle(meal_plans),
        details={
            'meal_plan_id': first_meal_plan.id,
            'meal_plan_ids': [meal_plan.id for meal_plan in meal_plans],
            'meal_plans': meal_plan_payload,
            'invitee_ids': valid_ids,
            'invitee_usernames': usernames,
            'date': first_meal_plan.date.isoformat(),
            'meal_time': first_meal_plan.meal_time,
        },
        warnings=warnings,
    )


def execute_meal_invitation(user: AbstractBaseUser, payload: dict) -> dict:
    meal_plan_ids = _normalize_meal_plan_ids(payload.get('meal_plan_ids'))
    if not meal_plan_ids and payload.get('meal_plan_id'):
        meal_plan_ids = _normalize_meal_plan_ids(payload['meal_plan_id'])
    if not meal_plan_ids:
        raise ValueError('Aucun repas sélectionné.')

    invitee_ids = payload.get('invitee_ids') or []

    meal_plans = list(
        MealPlan.objects.filter(id__in=meal_plan_ids, user=user).order_by('date', 'meal_time', 'id')
    )
    if len(meal_plans) != len(meal_plan_ids):
        raise ValueError('Un ou plusieurs repas sont introuvables.')

    complice_ids = _complice_ids_for_user(user)
    valid_ids = [uid for uid in invitee_ids if uid in complice_ids]
    if not valid_ids:
        raise ValueError('Aucun ami valide.')

    invitees = {u.id: u for u in User.objects.filter(id__in=valid_ids)}
    invitations = []
    notification_data = []

    for meal_plan in meal_plans:
        for invitee_id in valid_ids:
            invitee = invitees.get(invitee_id)
            if not invitee:
                continue
            invitation, created = MealInvitation.objects.get_or_create(
                inviter=user,
                invitee=invitee,
                meal_plan=meal_plan,
                defaults={'status': 'pending'},
            )
            if created:
                invitations.append(invitation)
                notification_data.append({
                    'user': invitee,
                    'notification_type': 'meal_invitation',
                    'title': f'{user.username} vous invite à un repas',
                    'message': (
                        f'{user.username} vous invite à {meal_plan.get_meal_time_display()} '
                        f'le {meal_plan.date.strftime("%d/%m/%Y")}'
                    ),
                    'related_user': user,
                    'meal_plan': meal_plan,
                })

    if notification_data:
        def create_notifications_and_pushes():
            for notif_data in notification_data:
                meal_plan = notif_data.pop('meal_plan')
                notification = Notification.objects.create(**notif_data)
                target_user = notif_data.get('user')
                if not target_user:
                    continue
                devices = PushDevice.objects.filter(
                    user=target_user,
                    is_active=True,
                ).exclude(expo_push_token='')
                messages = []
                for device in devices:
                    messages.append({
                        'to': device.expo_push_token,
                        'title': notification.title,
                        'body': notification.message,
                        'data': {
                            'source': 'social',
                            'kind': 'meal_invitation',
                            'notification_id': notification.id,
                            'meal_plan_id': meal_plan.id,
                            'meal_plan_date': meal_plan.date.isoformat(),
                            'meal_time': meal_plan.meal_time,
                        },
                        'sound': 'default',
                    })
                if messages:
                    send_expo_push_notifications(messages)

        transaction.on_commit(create_notifications_and_pushes)

    return {
        'meal_plan_id': meal_plans[0].id,
        'meal_plan_ids': [meal_plan.id for meal_plan in meal_plans],
        'invitation_count': len(invitations),
        'invitee_usernames': [invitees[i].username for i in valid_ids if i in invitees],
    }
