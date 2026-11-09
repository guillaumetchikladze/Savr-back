"""
Registre unique des notifications (push + in-app) : une entrée par type.

Chaque entrée définit la catégorie (réglable par l'utilisateur), les modèles de titre / corps,
les champs `data` attendus, le lien profond, le regroupement et le caractère transactionnel.
Aucune chaîne de notification ne doit vivre ailleurs : les appelants passent par
`notifications.services.notify()` / `push()` avec un `type` de ce registre.

Ton : tutoiement, phrases courtes, au plus un emoji (seulement s'il apporte quelque chose),
pas de point d'exclamation, jamais de culpabilisation (« tu vas perdre ta série », « ça fait
longtemps » : interdit).

Payload `data` (identique pour tous les types, cf. `build_data`) :
    type, category, notification_id (envoi, pour le suivi d'ouverture), url (lien profond),
    ids cibles (post_id, user_id, meal_plan_id…), inbox_id (notification in-app éventuelle),
    + source / kind historiques pour les apps déjà publiées.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from django.conf import settings

from .text import excerpt, meal_moment, people, plural, quoted

# ---------------------------------------------------------------------------
# Catégories (réglages utilisateur)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    description: str
    default_enabled: bool = True
    user_configurable: bool = True


CATEGORIES: dict[str, Category] = {
    c.key: c
    for c in (
        Category('social', 'Réactions et amis', 'Miams, commentaires, mentions et nouveaux amis.'),
        Category('friends_activity', 'Activité de tes amis', 'Un résumé par jour au plus quand tes amis publient.'),
        Category('weekly_recap', 'Ta semaine à table', 'Le dimanche soir, ce que tu as cuisiné dans la semaine.'),
        Category('rewards', 'Badges', 'Quand tu débloques un badge.'),
        Category('cooking_reminders', 'Rappels cuisine', '« Tu as cuisiné… ? » après ton repas.'),
        Category('planning', 'Repas et courses partagés', 'Invitations à table et listes de courses partagées.'),
        Category('tables', 'Tes tablées', 'Invitations et, le dimanche soir, ce que ta tablée a cuisiné.'),
        # Non réglable : minuteurs lancés par l'utilisateur, tests techniques.
        Category('essential', 'Minuteurs', 'Fin de minuteur pendant que tu cuisines.', user_configurable=False),
    )
}

# Valeurs de `quiet_hours` : que faire d'une push pendant les heures calmes.
QUIET_SKIP = 'skip'  # pas de push (la notification in-app reste)
QUIET_DEFER = 'defer'  # l'appelant replanifie à la fin des heures calmes
QUIET_IGNORE = 'ignore'  # envoyée quand même (transactionnel)

Renderer = Callable[[dict], tuple[str, str]]


class _SafeDict(dict):
    def __missing__(self, key):
        return ''


@dataclass(frozen=True)
class NotificationType:
    key: str
    category: str
    title: str = ''
    body: str = ''
    renderer: Renderer | None = None  # pluriels / agrégation ; prioritaire sur title/body
    transactional: bool = False  # True : hors plafond quotidien
    enabled: bool = True  # False : type réservé, jamais envoyé
    inbox_type: str | None = None  # accounts.Notification.notification_type (in-app) ; None = push seule
    data_fields: tuple[str, ...] = ()  # ids cibles toujours présents dans `data` (None si absents)
    url: str = ''  # chemin du lien profond, formaté avec `data`
    group_key: str = ''  # regroupement (collapseId Android, agrégation serveur)
    group_window: int = 0  # > 0 : push débouncée puis agrégée (secondes)
    quiet_hours: str = ''  # '' → QUIET_IGNORE si transactionnel, sinon QUIET_SKIP
    channel_id: str = ''  # canal Android déjà créé par l'app
    ios_category: str = ''  # catégorie à boutons (categoryId Expo)
    legacy_source: str = ''  # data.source / data.kind lus par l'app ≤ 1.0.x
    legacy_kind: str = ''
    legacy_aliases: dict[str, str] = field(default_factory=dict)  # data_field → ancien nom (camelCase)

    @property
    def quiet_policy(self) -> str:
        return self.quiet_hours or (QUIET_IGNORE if self.transactional else QUIET_SKIP)

    def render(self, context: dict | None = None) -> tuple[str, str]:
        ctx = _SafeDict(context or {})
        if self.renderer:
            return self.renderer(ctx)
        return self.title.format_map(ctx), self.body.format_map(ctx)

    def build_url(self, data: dict) -> str:
        if not self.url:
            return ''
        scheme = getattr(settings, 'NOTIFICATIONS_DEEP_LINK_SCHEME', 'tchikook')
        return f'{scheme}://{self.url.format_map(_SafeDict({k: ("" if v is None else v) for k, v in data.items()}))}'

    def build_group_key(self, data: dict) -> str:
        if not self.group_key:
            return ''
        return self.group_key.format_map(_SafeDict({k: ('' if v is None else v) for k, v in data.items()}))

    def build_data(self, data: dict | None = None, *, notification_id: int | None = None) -> dict:
        """Payload `data` de la push : forme commune à tous les types."""
        data = dict(data or {})
        payload = {
            'type': self.key,
            'category': self.category,
            'notification_id': notification_id,
            'url': self.build_url(data),
        }
        for name in self.data_fields:
            payload[name] = data.get(name)
        for name, value in data.items():
            payload.setdefault(name, value)
        for name, alias in self.legacy_aliases.items():
            payload.setdefault(alias, payload.get(name))
        if self.legacy_source:
            payload['source'] = self.legacy_source
        if self.legacy_kind:
            payload['kind'] = self.legacy_kind
        return payload


# ---------------------------------------------------------------------------
# Renderers (pluriels, agrégation)
# ---------------------------------------------------------------------------
# Contexte commun : actor (nom), actors (noms uniques, le plus récent d'abord), count (nombre
# d'événements), dish (titre du plat déjà entre guillemets, ou « ton plat »).


def _actors(ctx: dict) -> tuple[list[str], int]:
    names = list(ctx.get('actors') or ([ctx['actor']] if ctx.get('actor') else []))
    return names, max(len(names), 1)


def _render_miam(ctx: dict) -> tuple[str, str]:
    names, n = _actors(ctx)
    verb = 'a aimé' if n == 1 else 'ont aimé'
    return plural(n, 'Nouveau miam', 'Nouveaux miams'), f'{people(names, n)} {verb} {ctx["dish"]}.'


def _render_my_turn(ctx: dict) -> tuple[str, str]:
    names, n = _actors(ctx)
    verb = 'veut refaire' if n == 1 else 'veulent refaire'
    return 'Ton plat donne envie', f'{people(names, n)} {verb} {ctx["dish"]}.'


def _render_my_turn_cooked(ctx: dict) -> tuple[str, str]:
    names, n = _actors(ctx)
    verb = 'a refait' if n == 1 else 'ont refait'
    return 'Ton plat a été refait', f'{people(names, n)} {verb} {ctx["dish"]}.'


def _render_invite_joined(ctx: dict) -> tuple[str, str]:
    """Lien d'invitation utilisé : nouvel inscrit (« grâce à toi ») ou compte existant (amis)."""
    if ctx.get('new_member'):
        return 'Un ami de plus', f'{ctx["actor"]} a rejoint Tchikook grâce à toi.'
    return 'Nouvel ami', f'{ctx["actor"]} a utilisé ton lien. Tu peux voir ses plats.'


def _render_comment(ctx: dict) -> tuple[str, str]:
    names, n = _actors(ctx)
    count = max(int(ctx.get('count') or 1), n)
    if count == 1:
        text = excerpt(ctx.get('comment'))
        body = f'{names[0] if names else "Quelqu’un"} a commenté {ctx["dish"]}'
        return 'Nouveau commentaire', f'{body} : {text}' if text else f'{body}.'
    if n == 1:
        return 'Nouveaux commentaires', f'{names[0]} a laissé {count} commentaires sous {ctx["dish"]}.'
    return 'Nouveaux commentaires', f'{people(names, n)} ont commenté {ctx["dish"]}.'


def _render_meal_invitation(ctx: dict) -> tuple[str, str]:
    count = int(ctx.get('count') or 1)
    if count > 1:
        return 'Invitation à table', f'{ctx["actor"]} t’invite à {count} repas.'
    return 'Invitation à table', f'{ctx["actor"]} t’invite {meal_moment(ctx.get("meal_time"), ctx.get("date"))}.'


def _render_meal_invitation_accepted(ctx: dict) -> tuple[str, str]:
    moment = meal_moment(ctx.get('meal_time'), ctx.get('date'))
    return 'Invitation acceptée', f'{ctx["actor"]} a accepté ton invitation {moment}.'


def _render_timer(ctx: dict) -> tuple[str, str]:
    step = ctx.get('step_order')
    recipe = (ctx.get('recipe') or '').strip()
    if step and recipe:
        body = f'Étape {step} de {quoted(recipe)} : plus que quelques secondes.'
    elif step:
        body = f'Étape {step} : plus que quelques secondes.'
    else:
        body = 'Plus que quelques secondes, reviens en cuisine.'
    return 'Minuteur bientôt fini', body


def _render_cooked_prompt(ctx: dict) -> tuple[str, str]:
    dish = (ctx.get('dish_title') or '').strip()
    question = f'Tu as cuisiné {dish} ?' if dish else 'Tu as cuisiné ?'
    return 'À table', f'{question} Partage-le en 1 tap.'


def _render_cooked_pending(ctx: dict) -> tuple[str, str]:
    """« Je l'ai déjà » : rappel du soir d'un repas planifié non confirmé (Oui / Pas fait en 1 tap)."""
    dish = (ctx.get('dish_title') or '').strip()
    question = f'Tu as cuisiné {dish} ?' if dish else 'Tu as cuisiné ce que tu avais prévu ?'
    return 'À table', f'{question} Un tap suffit.'


def _table(ctx: dict) -> str:
    return quoted(ctx.get('table'), 'ta tablée')


def _render_table_invitation(ctx: dict) -> tuple[str, str]:
    name = (ctx.get('table') or '').strip()
    where = f'la tablée {quoted(name)}' if name else 'sa tablée'
    return 'Invitation à une tablée', f'{ctx["actor"] or "Un ami"} t’invite à {where}.'


def _render_table_joined(ctx: dict) -> tuple[str, str]:
    return 'Ta tablée s’agrandit', f'{ctx["actor"] or "Quelqu’un"} a rejoint {_table(ctx)}.'


def _render_table_weekly(ctx: dict) -> tuple[str, str]:
    """Dimanche soir, positif seulement (jamais envoyé quand personne n'a cuisiné)."""
    cooked = max(int(ctx.get('cooked_members') or 1), 1)
    total = max(int(ctx.get('member_count') or cooked), cooked)
    if total >= 2 and cooked >= total:
        body = f'Cette semaine, tout le monde a cuisiné dans {_table(ctx)}.'
    else:
        verb = 'a' if cooked == 1 else 'ont'
        body = f'Cette semaine, {cooked} sur {total} {verb} cuisiné dans {_table(ctx)}.'
    return 'Ta tablée', body


def _render_new_badge(ctx: dict) -> tuple[str, str]:
    title = f'Nouveau badge : {ctx["badge"]}'
    if ctx.get('emoji'):
        title = f'{title} {ctx["emoji"]}'
    others = int(ctx.get('others') or 0)
    body = (ctx.get('description') or '').strip()
    if others:
        extra = f'Et {others} autre{"s" if others > 1 else ""} badge{"s" if others > 1 else ""} à découvrir.'
        body = f'{body} {extra}'.strip()
    return title, body or 'Viens le voir sur ton profil.'


def _render_friends_digest(ctx: dict) -> tuple[str, str]:
    names = list(ctx.get('actors') or [])
    authors = max(int(ctx.get('author_count') or len(names)), 1)
    posts = max(int(ctx.get('post_count') or 1), 1)
    who = people(names, authors)
    if authors == 1 and posts == 1:
        dish = (ctx.get('dish_title') or '').strip()
        body = f'{who} a partagé {quoted(dish)}.' if dish else f'{who} a partagé un nouveau plat.'
    elif authors == 1:
        body = f'{who} a partagé {posts} plats.'
    else:
        body = f'{who} ont partagé de nouveaux plats.'
    return 'Du nouveau chez tes amis', body


def _render_weekly_recap(ctx: dict) -> tuple[str, str]:
    cooked = int(ctx.get('cooked') or 0)
    new = int(ctx.get('new_dishes') or 0)
    friends = int(ctx.get('friends_posts') or 0)
    if cooked:
        body = f'Cette semaine tu as cuisiné {cooked} {plural(cooked, "plat")}'
        body += f', dont {new} {plural(new, "nouveau", "nouveaux")}.' if new else '.'
        if friends:
            body += f' Tes amis en ont partagé {friends}.'
    else:
        body = f'Tes amis ont partagé {friends} {plural(friends, "plat")} cette semaine. De quoi piocher des idées.'
    return 'Ta semaine à table', body


# ---------------------------------------------------------------------------
# Registre
# ---------------------------------------------------------------------------
_POST = ('post_id',)

_TYPES = (
    # --- Social ----------------------------------------------------------------
    NotificationType(
        'post_miam',
        'social',
        renderer=_render_miam,
        inbox_type='post_miam',
        data_fields=_POST,
        url='post/{post_id}',
        group_key='post_miam:{post_id}',
        group_window=12,
        legacy_source='social',
        legacy_kind='post_miam',
    ),
    NotificationType(
        'post_comment',
        'social',
        renderer=_render_comment,
        transactional=True,
        inbox_type='post_comment',
        data_fields=_POST,
        url='post/{post_id}',
        group_key='post_comment:{post_id}',
        group_window=12,
        legacy_source='social',
        legacy_kind='post_comment',
    ),
    NotificationType(
        'post_comment_mention',
        'social',
        title='On parle de toi',
        body='{actor} parle de toi sous {dish}.',
        transactional=True,
        inbox_type='post_comment_mention',
        data_fields=_POST,
        url='post/{post_id}',
        legacy_source='social',
        legacy_kind='post_comment_mention',
    ),
    NotificationType(
        'post_repost',
        'social',
        title='Ton plat circule',
        body='{actor} a repartagé {dish}.',
        inbox_type='post_repost',
        data_fields=_POST,
        url='post/{post_id}',
        legacy_source='social',
        legacy_kind='post_repost',
    ),
    NotificationType(
        'follow_new',
        'social',
        title='{actor} te suit',
        body='Jette un œil à ses plats.',
        inbox_type='follow',
        data_fields=('user_id',),
        url='user/{user_id}',
        legacy_source='social',
        legacy_kind='follow',
    ),
    NotificationType(
        'follow_request',
        'social',
        title='Demande d’ami',
        body='{actor} veut te suivre.',
        transactional=True,
        inbox_type='follow_request',
        data_fields=('user_id',),
        url='user/{user_id}',
        legacy_source='social',
        legacy_kind='follow_request',
    ),
    NotificationType(
        'follow_accepted',
        'social',
        title='Demande acceptée',
        body='{actor} a accepté ta demande. Tu peux voir ses plats.',
        transactional=True,
        inbox_type='follow',
        data_fields=('user_id',),
        url='user/{user_id}',
        legacy_source='social',
        legacy_kind='follow',
    ),
    # Lien d'invitation utilisé (« Tes amis avant ton premier plat ») : amitié mutuelle déjà créée.
    # In-app `follow` (lue par l'app 1.0.9 comme un nouvel ami) ; pas de push la nuit.
    NotificationType(
        'invite_joined',
        'social',
        renderer=_render_invite_joined,
        transactional=True,
        quiet_hours=QUIET_SKIP,
        inbox_type='follow',
        data_fields=('user_id',),
        url='user/{user_id}',
        legacy_source='social',
        legacy_kind='follow',
    ),
    # « À mon tour » (2e réaction à côté du miam) : un ami met ton plat dans son panier « Cette semaine ».
    # Une fois par (ami, post) ; push débouncée et agrégée comme les miams, plafond quotidien.
    NotificationType(
        'a_mon_tour',
        'social',
        renderer=_render_my_turn,
        inbox_type='a_mon_tour',
        data_fields=_POST,
        url='post/{post_id}',
        group_key='a_mon_tour:{post_id}',
        group_window=12,
    ),
    # L'ami a cuisiné l'envie : `post_id` = ton post d'origine (regroupement), `cooked_post_id` = son post.
    NotificationType(
        'a_mon_tour_cooked',
        'social',
        renderer=_render_my_turn_cooked,
        inbox_type='a_mon_tour_cooked',
        data_fields=(*_POST, 'cooked_post_id'),
        url='post/{post_id}',
        group_key='a_mon_tour_cooked:{post_id}',
        group_window=12,
    ),
    # --- Activité des amis / récap / badges (rétention) ----------------------------
    NotificationType(
        'friends_digest',
        'friends_activity',
        renderer=_render_friends_digest,
        data_fields=('post_ids', 'author_ids'),
        url='feed/friends',
        group_key='friends_digest',
    ),
    NotificationType(
        'weekly_recap',
        'weekly_recap',
        renderer=_render_weekly_recap,
        data_fields=('week_start', 'cooked', 'new_dishes', 'friends_posts'),
        url='stats/week',
        group_key='weekly_recap',
    ),
    NotificationType(
        'new_badge',
        'rewards',
        renderer=_render_new_badge,
        inbox_type='achievement',
        data_fields=('badge_codes',),
        url='badges',
        group_key='new_badge',
        legacy_source='rewards',
        legacy_kind='new_badge',
        legacy_aliases={'badge_codes': 'codes'},
    ),
    # --- Cuisine -------------------------------------------------------------------
    NotificationType(
        'cooked_prompt',
        'cooking_reminders',
        renderer=_render_cooked_prompt,
        transactional=True,  # 1 par repas cuisiné, déclenché par l'utilisateur : hors plafond
        quiet_hours=QUIET_DEFER,  # repoussée à la fin des heures calmes, jamais à minuit
        data_fields=('meal_plan_id', 'recipe_id', 'recipe_ids', 'date', 'meal_time', 'title', 'action'),
        url='cooked/{meal_plan_id}',
        group_key='cooked_prompt:{meal_plan_id}',
        channel_id='meal_photos',
        ios_category='cooked_prompt',
        legacy_source='cooking',
        legacy_kind='meal_time_photo_reminder',
    ),
    # « Je l'ai déjà » : rappel du soir d'un repas planifié dont le créneau est passé, sans confirmation
    # (liste `pending_confirmations`, refus « Pas fait » respectés). 1 par soir au plus, jamais un repas
    # déjà relancé (même `dedupe_key` `cooked_prompt:{meal_plan_id}` que la push de fin de cuisson).
    # Payload identique à `cooked_prompt` (`action`, `source`/`kind`) : l'app 1.0.9 le traite pareil.
    NotificationType(
        'cooked_pending',
        'cooking_reminders',
        renderer=_render_cooked_pending,
        quiet_hours=QUIET_SKIP,
        data_fields=(
            'meal_plan_id',
            'basket_item_id',
            'pending_kind',
            'pending_id',
            'recipe_id',
            'recipe_ids',
            'date',
            'meal_time',
            'title',
            'action',
        ),
        url='cooked/{meal_plan_id}',
        group_key='cooked_prompt:{meal_plan_id}',
        channel_id='meal_photos',
        ios_category='cooked_prompt',
        legacy_source='cooking',
        legacy_kind='meal_time_photo_reminder',
    ),
    NotificationType(
        'timer_almost_finished',
        'essential',
        renderer=_render_timer,
        transactional=True,
        data_fields=('timer_id', 'recipe_id', 'recipe_batch_id', 'step_order'),
        url='cooking/{recipe_id}',
        group_key='timer:{timer_id}',
        channel_id='timer_near_end',
        legacy_source='timer',
        legacy_kind='almost_finished',
        legacy_aliases={
            'timer_id': 'timerId',
            'recipe_id': 'recipeId',
            'recipe_batch_id': 'recipeBatchId',
            'step_order': 'stepOrder',
        },
    ),
    # --- Repas et courses partagés -----------------------------------------------------
    NotificationType(
        'meal_invitation',
        'planning',
        renderer=_render_meal_invitation,
        transactional=True,
        inbox_type='meal_invitation',
        data_fields=('meal_plan_id', 'meal_plan_date', 'meal_time'),
        url='meal-plan/{meal_plan_id}',
        group_key='meal_invitation:{user_id}',
        legacy_source='social',
        legacy_kind='meal_invitation',
    ),
    NotificationType(
        'meal_invitation_accepted',
        'planning',
        renderer=_render_meal_invitation_accepted,
        transactional=True,
        inbox_type='meal_invitation',  # l'app lit « accepté » dans le texte pour l'état de la carte
        data_fields=('meal_plan_id', 'meal_plan_date', 'meal_time', 'user_id'),
        url='meal-plan/{meal_plan_id}',
        legacy_source='social',
        legacy_kind='meal_invitation',
    ),
    NotificationType(
        'shopping_list_invitation',
        'planning',
        title='Liste de courses partagée',
        body='{actor} t’invite sur la liste {list_name}.',
        transactional=True,
        inbox_type='shopping_list_invitation',
        data_fields=('shopping_list_id',),
        url='shopping-list/{shopping_list_id}',
        group_key='shopping_list:{shopping_list_id}',
        legacy_source='social',
        legacy_kind='shopping_list_invitation',
    ),
    NotificationType(
        'shopping_list_member_added',
        'planning',
        title='Liste de courses partagée',
        body='{actor} partage la liste {list_name} avec toi.',
        transactional=True,
        inbox_type='shopping_list_invitation',
        data_fields=('shopping_list_id',),
        url='shopping-list/{shopping_list_id}',
        group_key='shopping_list:{shopping_list_id}',
        legacy_source='social',
        legacy_kind='shopping_list_invitation',
    ),
    # Foyer (« Foyer + invités ») : un ami t'ajoute à son foyer (repas comptés pour toi par défaut).
    NotificationType(
        'household_request',
        'planning',
        title='Foyer',
        body='{actor} veut t’ajouter à son foyer.',
        transactional=True,
        inbox_type='household_request',
        data_fields=('household_link_id', 'user_id'),
        url='household',
        group_key='household:{household_link_id}',
    ),
    NotificationType(
        'household_accepted',
        'planning',
        title='Foyer',
        body='{actor} a rejoint ton foyer.',
        transactional=True,
        inbox_type='household_accepted',
        data_fields=('household_link_id', 'user_id'),
        url='household',
        group_key='household:{household_link_id}',
    ),
    # --- Les tablées (petits groupes privés) ----------------------------------------------
    NotificationType(
        'table_invitation',
        'tables',
        renderer=_render_table_invitation,
        transactional=True,
        quiet_hours=QUIET_SKIP,
        inbox_type='table_invitation',
        data_fields=('table_id', 'user_id'),
        url='tables/{table_id}',
        group_key='table:{table_id}',
    ),
    NotificationType(
        'table_joined',
        'tables',
        renderer=_render_table_joined,
        inbox_type='table_joined',
        data_fields=('table_id', 'user_id'),
        url='tables/{table_id}',
        group_key='table:{table_id}',
    ),
    # Dimanche soir (même beat que le récap) : positif seulement, jamais à 0 ; 1 par semaine au plus.
    NotificationType(
        'table_weekly',
        'tables',
        renderer=_render_table_weekly,
        data_fields=('table_id', 'week_start', 'cooked_members', 'member_count'),
        url='tables/{table_id}',
        group_key='table_weekly',
    ),
    # --- Technique ---------------------------------------------------------------------
    NotificationType(
        'test_push',
        'essential',
        title='Test des notifications',
        body='Si tu vois ceci, les notifications fonctionnent.',
        transactional=True,
        legacy_source='debug',
        legacy_kind='test_push',
    ),
)

REGISTRY: dict[str, NotificationType] = {t.key: t for t in _TYPES}

# Clés de l'ancien écran de réglages de l'app (AsyncStorage `notification_preferences_v1`,
# par notification_type) → types du registre. Acceptées par PATCH /notifications/preferences/.
LEGACY_PREFERENCE_KEYS: dict[str, tuple[str, ...]] = {
    'post_miam': ('post_miam',),
    'post_repost': ('post_repost',),
    'post_comment': ('post_comment',),
    'post_comment_mention': ('post_comment_mention',),
    'follow': ('follow_new', 'follow_request', 'follow_accepted'),
    'meal_invitation': ('meal_invitation', 'meal_invitation_accepted'),
}


def get_type(key: str) -> NotificationType:
    try:
        return REGISTRY[key]
    except KeyError:
        raise KeyError(f'Type de notification inconnu : {key}') from None


def configurable_categories() -> list[Category]:
    return [c for c in CATEGORIES.values() if c.user_configurable]


def dish_context(title: str | None) -> str:
    """Valeur `dish` des modèles : « Quiche » entre guillemets, ou « ton plat »."""
    return quoted(title)
