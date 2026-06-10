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


def propose_meal_invitation(
    user: AbstractBaseUser,
    meal_plan_id: int,
    invitee_ids: list[int],
) -> MutationProposal:
    accessible_filter = get_accessible_meal_plan_filter(user)
    meal_plan = MealPlan.objects.filter(accessible_filter, id=meal_plan_id).select_related('user').first()
    if not meal_plan:
        raise ValueError('Meal plan introuvable ou inaccessible.')
    if meal_plan.user_id != user.id:
        raise PermissionError('Seul le propriétaire peut inviter des complices.')

    complice_ids = _complice_ids_for_user(user)
    valid_ids = [uid for uid in invitee_ids if uid in complice_ids]
    if not valid_ids:
        raise ValueError('Aucun complice valide trouvé.')

    invitees = list(User.objects.filter(id__in=valid_ids))
    usernames = [u.username for u in invitees]
    meal_label = meal_plan.get_meal_time_display()
    date_str = meal_plan.date.strftime('%d/%m/%Y')

    warnings = []
    already_invited = list(
        MealInvitation.objects.filter(
            meal_plan=meal_plan,
            invitee_id__in=valid_ids,
            status__in=['pending', 'accepted'],
        ).values_list('invitee__username', flat=True)
    )
    if already_invited:
        warnings.append(f'Déjà invité(s): {", ".join(already_invited)}')

    return MutationProposal(
        card_type='meal_invitation',
        title='Inviter des complices',
        subtitle=f'{meal_label} du {date_str}',
        details={
            'meal_plan_id': meal_plan.id,
            'invitee_ids': valid_ids,
            'invitee_usernames': usernames,
            'date': meal_plan.date.isoformat(),
            'meal_time': meal_plan.meal_time,
        },
        warnings=warnings,
    )


def execute_meal_invitation(user: AbstractBaseUser, payload: dict) -> dict:
    meal_plan_id = payload['meal_plan_id']
    invitee_ids = payload.get('invitee_ids') or []

    meal_plan = MealPlan.objects.filter(id=meal_plan_id, user=user).first()
    if not meal_plan:
        raise ValueError('Meal plan introuvable.')

    complice_ids = _complice_ids_for_user(user)
    valid_ids = [uid for uid in invitee_ids if uid in complice_ids]
    if not valid_ids:
        raise ValueError('Aucun complice valide.')

    invitees = {u.id: u for u in User.objects.filter(id__in=valid_ids)}
    invitations = []
    notification_data = []

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
            })

    if notification_data:
        def create_notifications_and_pushes():
            for notif_data in notification_data:
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
        'meal_plan_id': meal_plan.id,
        'invitation_count': len(invitations),
        'invitee_usernames': [invitees[i].username for i in valid_ids if i in invitees],
    }
