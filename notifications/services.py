"""
Point d'entrée unique des notifications : `notify()` (in-app + push) et `push()` (push seule).

Règles appliquées à chaque push (dans l'ordre) :
1. type activé dans le registre ;
2. appareils actifs ;
3. préférences : catégorie et type (surcharges) ;
4. heures calmes (défaut 22:00–08:00, fuseau de l'utilisateur) : non transactionnel → pas de push,
   `QUIET_DEFER` → l'appelant replanifie (`result.retry_at`), transactionnel → envoyée ;
5. plafond quotidien des non transactionnels (`NOTIFICATIONS_DAILY_CAP`, défaut 2) ;
6. idempotence (`dedupe_key` unique par utilisateur).
La notification in-app (si le type en a une) est toujours créée : les réglages ne coupent que la push.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.models import Notification, PushDevice
from accounts.services.expo_push import send_expo_push_notifications

from . import grouping
from .models import NotificationDelivery, NotificationPreference
from .registry import CATEGORIES, QUIET_DEFER, QUIET_IGNORE, NotificationType, dish_context, get_type

logger = logging.getLogger(__name__)

DEFAULT_QUIET_START = time(22, 0)
DEFAULT_QUIET_END = time(8, 0)


# ---------------------------------------------------------------------------
# Préférences
# ---------------------------------------------------------------------------
def get_preferences(user) -> NotificationPreference:
    """Préférences de l'utilisateur (instance non sauvegardée avec les défauts s'il n'en a pas)."""
    prefs = NotificationPreference.objects.filter(user_id=user.id).first()
    return prefs or NotificationPreference(user_id=user.id)


def category_enabled(prefs: NotificationPreference, category_key: str) -> bool:
    category = CATEGORIES.get(category_key)
    if category is None:
        return False
    if not category.user_configurable:
        return True
    return bool((prefs.categories or {}).get(category_key, category.default_enabled))


def type_enabled(prefs: NotificationPreference, spec: NotificationType) -> bool:
    if not spec.enabled or not category_enabled(prefs, spec.category):
        return False
    return bool((prefs.types or {}).get(spec.key, True))


def user_zone(prefs: NotificationPreference) -> ZoneInfo:
    try:
        return ZoneInfo(prefs.timezone or settings.TIME_ZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(settings.TIME_ZONE)


def local_now(prefs: NotificationPreference, now: datetime | None = None) -> datetime:
    return (now or timezone.now()).astimezone(user_zone(prefs))


def in_quiet_hours(prefs: NotificationPreference, now: datetime | None = None) -> bool:
    start = prefs.quiet_hours_start or DEFAULT_QUIET_START
    end = prefs.quiet_hours_end or DEFAULT_QUIET_END
    if start == end:
        return False
    t = local_now(prefs, now).time()
    if start < end:
        return start <= t < end
    return t >= start or t < end


def quiet_hours_end(prefs: NotificationPreference, now: datetime | None = None) -> datetime:
    """Prochaine fin des heures calmes (aware, UTC)."""
    local = local_now(prefs, now)
    end = prefs.quiet_hours_end or DEFAULT_QUIET_END
    candidate = local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(ZoneInfo('UTC'))


def daily_cap() -> int:
    return int(getattr(settings, 'NOTIFICATIONS_DAILY_CAP', 2))


def non_transactional_sent_today(user, prefs: NotificationPreference, now: datetime | None = None) -> int:
    local = local_now(prefs, now)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return NotificationDelivery.objects.filter(
        user_id=user.id,
        transactional=False,
        status=NotificationDelivery.STATUS_SENT,
        created_at__gte=midnight,
    ).count()


# ---------------------------------------------------------------------------
# Envoi
# ---------------------------------------------------------------------------
@dataclass
class PushResult:
    status: str  # sent | disabled | no_devices | muted | quiet_hours | deferred | capped | duplicate | grouped
    delivery: NotificationDelivery | None = None
    retry_at: datetime | None = None

    @property
    def sent(self) -> bool:
        return self.status == 'sent'


@dataclass
class NotifyResult:
    inbox: Notification | None
    push: PushResult | None


def _active_tokens(user) -> list[str]:
    return list(
        PushDevice.objects.filter(user_id=user.id, is_active=True)
        .exclude(expo_push_token='')
        .values_list('expo_push_token', flat=True)
    )


def _expo_messages(spec: NotificationType, tokens: list[str], title: str, body: str, data: dict) -> list[dict]:
    collapse_id = spec.build_group_key(data)
    messages = []
    for token in tokens:
        msg = {'to': token, 'title': title, 'body': body, 'data': data, 'sound': 'default'}
        if spec.channel_id:
            msg['channelId'] = spec.channel_id
        if spec.ios_category:
            msg['categoryId'] = spec.ios_category
        if collapse_id:
            msg['collapseId'] = collapse_id  # Android : remplace la push précédente du même groupe
        messages.append(msg)
    return messages


def push(
    type_key: str,
    user,
    *,
    context: dict | None = None,
    data: dict | None = None,
    title: str | None = None,
    body: str | None = None,
    inbox: Notification | None = None,
    dedupe_key: str = '',
    now: datetime | None = None,
) -> PushResult:
    """Push seule (pas d'in-app). `title`/`body` explicites : textes déjà rendus par le registre."""
    spec = get_type(type_key)
    if not spec.enabled:
        return PushResult('disabled')
    tokens = _active_tokens(user)
    if not tokens:
        return PushResult('no_devices')
    prefs = get_preferences(user)
    if not type_enabled(prefs, spec):
        return PushResult('muted')
    now = now or timezone.now()

    status = NotificationDelivery.STATUS_SENT
    policy = spec.quiet_policy
    if policy != QUIET_IGNORE and in_quiet_hours(prefs, now):
        if policy == QUIET_DEFER:
            return PushResult('deferred', retry_at=quiet_hours_end(prefs, now))
        status = NotificationDelivery.STATUS_QUIET
    elif not spec.transactional and non_transactional_sent_today(user, prefs, now) >= daily_cap():
        status = NotificationDelivery.STATUS_CAPPED

    if title is None or body is None:
        title, body = spec.render(context)
    data = dict(data or {})
    if inbox is not None:
        data.setdefault('inbox_id', inbox.id)

    try:
        with transaction.atomic():
            delivery = NotificationDelivery.objects.create(
                user_id=user.id,
                type=spec.key,
                category=spec.category,
                transactional=spec.transactional,
                status=status,
                title=title[:200],
                body=body,
                inbox_notification=inbox,
                dedupe_key=dedupe_key[:120],
                device_count=len(tokens) if status == NotificationDelivery.STATUS_SENT else 0,
                created_at=now,
            )
    except IntegrityError:
        logger.info('[notify] %s déjà envoyé à user=%s (%s)', spec.key, user.id, dedupe_key)
        return PushResult('duplicate')

    payload = spec.build_data(data, notification_id=delivery.id)
    delivery.data = payload
    delivery.save(update_fields=['data'])
    if status != NotificationDelivery.STATUS_SENT:
        logger.info('[notify] %s non envoyé à user=%s : %s', spec.key, user.id, status)
        return PushResult(status, delivery)

    send_expo_push_notifications(_expo_messages(spec, tokens, title, body, payload))
    return PushResult('sent', delivery)


def notify(
    type_key: str,
    user,
    *,
    actor=None,
    post=None,
    context: dict | None = None,
    data: dict | None = None,
    inbox: bool = True,
    send_push: bool = True,
    dedupe_key: str = '',
    now: datetime | None = None,
) -> NotifyResult:
    """
    Notification in-app (si le type en a une) + push. `actor` remplit {actor} et `data.user_id` ;
    `post` remplit {dish} (titre affichable du plat) et `data.post_id`.
    Types groupés (`group_window`) : push débouncée puis agrégée (« Léa et 3 autres… »).
    """
    spec = get_type(type_key)
    if not spec.enabled:
        return NotifyResult(None, PushResult('disabled'))
    ctx = dict(context or {})
    data = dict(data or {})
    if actor is not None:
        ctx.setdefault('actor', actor.username)
        data.setdefault('user_id', actor.id)
    if post is not None:
        from recipes.services.cooked_service import post_display_title

        ctx.setdefault('dish', dish_context(post_display_title(post)))
        data.setdefault('post_id', post.id)
    ctx.setdefault('dish', dish_context(None))
    title, body = spec.render(ctx)

    inbox_notification = None
    if inbox and spec.inbox_type:
        inbox_notification = Notification.objects.create(
            user=user,
            notification_type=spec.inbox_type,
            title=title[:200],
            message=body,
            related_user=actor,
            related_post_id=data.get('post_id'),
        )

    push_result = None
    if send_push:
        if spec.group_window and inbox_notification is not None and data.get('post_id'):
            if _active_tokens(user):
                grouping.schedule_grouped_push(user_id=user.id, type_key=spec.key, object_id=data['post_id'])
                push_result = PushResult('grouped')
            else:
                push_result = PushResult('no_devices')
        else:
            push_result = push(
                spec.key,
                user,
                data=data,
                title=title,
                body=body,
                inbox=inbox_notification,
                dedupe_key=dedupe_key,
                now=now,
            )
    return NotifyResult(inbox_notification, push_result)


def send_grouped_push(user_id: int, type_key: str, object_id: int) -> PushResult:
    """
    Push agrégée des notifications in-app non lues d'un groupe (ex. miams d'un post, 24 h) :
    « Léa et 3 autres ont aimé « Quiche ». » Rien si tout a déjà été lu dans l'app.
    """
    from django.contrib.auth import get_user_model

    from recipes.models import Post
    from recipes.services.cooked_service import post_display_title

    spec = get_type(type_key)
    user = get_user_model().objects.filter(pk=user_id).first()
    if user is None:
        return PushResult('disabled')
    notifs = list(
        Notification.objects.filter(
            user_id=user_id,
            notification_type=spec.inbox_type,
            related_post_id=object_id,
            is_read=False,
            created_at__gte=timezone.now() - timedelta(hours=24),
        )
        .select_related('related_user')
        .order_by('-created_at', '-id')
    )
    if not notifs:
        return PushResult('no_unread')
    latest = notifs[0]
    names: list[str] = []
    for n in notifs:
        name = n.related_user.username if n.related_user_id else None
        if name and name not in names:
            names.append(name)

    data = {'post_id': object_id, 'aggregated': len(notifs) > 1, 'count': len(notifs)}
    if len(notifs) == 1:
        # Un seul événement : même texte que l'in-app (déjà rendu par le registre).
        return push(spec.key, user, data=data, title=latest.title, body=latest.message, inbox=latest)
    post = Post.objects.filter(pk=object_id).select_related('meal_plan', 'recipe_batch__recipe').first()
    ctx = {
        'actors': names,
        'actor': names[0] if names else '',
        'count': len(notifs),
        'dish': dish_context(post_display_title(post) if post else None),
    }
    return push(spec.key, user, context=ctx, data=data, inbox=latest)


# ---------------------------------------------------------------------------
# Suivi d'ouverture
# ---------------------------------------------------------------------------
def mark_opened(user, *, notification_id: int | None = None, type_key: str | None = None) -> bool:
    """Pose `opened_at` (première ouverture seulement) ; marque aussi l'in-app liée comme lue."""
    qs = NotificationDelivery.objects.filter(user_id=user.id, status=NotificationDelivery.STATUS_SENT)
    if notification_id:
        delivery = qs.filter(pk=notification_id).first()
    elif type_key:
        delivery = (
            qs.filter(type=type_key, opened_at__isnull=True, created_at__gte=timezone.now() - timedelta(days=7))
            .order_by('-created_at')
            .first()
        )
    else:
        delivery = None
    if delivery is None:
        return False
    if delivery.opened_at is None:
        NotificationDelivery.objects.filter(pk=delivery.pk, opened_at__isnull=True).update(opened_at=timezone.now())
    if delivery.inbox_notification_id:
        Notification.objects.filter(pk=delivery.inbox_notification_id, is_read=False).update(is_read=True)
    return True


def open_rates(since: datetime) -> list[dict]:
    """Taux d'ouverture par type depuis `since` (push réellement envoyées)."""
    from django.db.models import Count, Q

    rows = (
        NotificationDelivery.objects.filter(status=NotificationDelivery.STATUS_SENT, created_at__gte=since)
        .values('type')
        .annotate(sent=Count('id'), opened=Count('id', filter=Q(opened_at__isnull=False)))
        .order_by('type')
    )
    return [{**r, 'open_rate': round(r['opened'] / r['sent'], 3) if r['sent'] else 0.0} for r in rows]
