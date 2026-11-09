"""
« Je l'ai déjà » : confirmer en 1 tap un repas planifié dont le créneau est passé (contrat front :
.claude-work/specs/tables.md, partie « Je l'ai déjà »).

Repas « en attente de confirmation » de `user` :
- `MealPlan` qu'il possède, daté d'aujourd'hui ou d'hier (fuseau de l'utilisateur), non confirmé, sans post ;
- avec un plat connu : recettes, envie du panier datée (`BasketItem.meal_plan`), ou plat libre
  (`custom_label` d'un créneau standard ; sur un créneau « other » c'est le nom du créneau) ;
  ni cantine ni à emporter ;
- créneau passé : heure prévue (`scheduled_time`) + `COOKED_PENDING_GRACE_MINUTES`, sinon fin de créneau
  par défaut (petit-déjeuner 10:00, déjeuner 14:00, dîner et autre 21:00) ;
- pas refusé (« Pas fait » → `CookedConfirmationDismissal`).
Au plus `COOKED_PENDING_MAX` (3), le plus récent d'abord. « Oui » = `POST /posts/cooked/` avec
`cooked_payload` (le repas est confirmé et sort de la liste) ; « Pas fait » = `dismiss()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from django.conf import settings
from django.db.models import Exists, OuterRef, Prefetch

from recipes.models import BasketItem, CookedConfirmationDismissal, MealPlan, MealPlanRecipeBatch, Post

KIND_MEAL_PLAN = 'meal_plan'
KIND_BASKET_ITEM = 'basket_item'
KINDS = (KIND_MEAL_PLAN, KIND_BASKET_ITEM)
SLOT_END = {'breakfast': time(10, 0), 'lunch': time(14, 0), 'dinner': time(21, 0), 'other': time(21, 0)}
_STANDARD_SLOTS = ('breakfast', 'lunch', 'dinner')
_NOT_COOKED_TYPES = ('cantine', 'takeaway')


class PendingError(Exception):
    def __init__(self, message: str, status_code: int = 400, code: str = 'invalid'):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


def grace() -> timedelta:
    return timedelta(minutes=int(getattr(settings, 'COOKED_PENDING_GRACE_MINUTES', 60)))


def max_pending() -> int:
    return int(getattr(settings, 'COOKED_PENDING_MAX', 3))


@dataclass
class PendingItem:
    meal_plan: MealPlan
    basket_item: BasketItem | None
    title: str
    recipe_ids: list[int]
    due_at: datetime  # heure locale (aware) à partir de laquelle on peut demander

    @property
    def kind(self) -> str:
        return KIND_BASKET_ITEM if self.basket_item is not None else KIND_MEAL_PLAN

    @property
    def id(self) -> int:
        return self.basket_item.id if self.basket_item is not None else self.meal_plan.id

    @property
    def cooked_payload(self) -> dict:
        """Corps prêt pour `POST /api/posts/cooked/` (et `cooked/preview/`)."""
        if self.basket_item is not None:
            return {'basket_item_id': self.basket_item.id}
        return {'meal_plan_id': self.meal_plan.id}

    def payload(self) -> dict:
        mp = self.meal_plan
        return {
            'kind': self.kind,
            'id': self.id,
            'meal_plan_id': mp.id,
            'basket_item_id': self.basket_item.id if self.basket_item is not None else None,
            'title': self.title,
            'question': f'Tu as cuisiné {self.title} ?',
            'date': mp.date.isoformat(),
            'meal_time': mp.meal_time,
            'scheduled_time': mp.scheduled_time.strftime('%H:%M') if mp.scheduled_time else None,
            'recipe_id': self.recipe_ids[0] if self.recipe_ids else None,
            'recipe_ids': self.recipe_ids,
            'cooked_payload': self.cooked_payload,
            'dismiss_url': f'/api/cooked/pending-confirmations/{self.kind}/{self.id}/dismiss/',
        }


def due_at(meal_plan: MealPlan, zone) -> datetime:
    if meal_plan.scheduled_time:
        local = datetime.combine(meal_plan.date, meal_plan.scheduled_time) + grace()
    else:
        local = datetime.combine(meal_plan.date, SLOT_END.get(meal_plan.meal_time, SLOT_END['other']))
    return local.replace(tzinfo=zone)


def _recipes(meal_plan: MealPlan) -> tuple[list[int], list[str]]:
    ids, titles = [], []
    for mprb in meal_plan.meal_plan_recipe_batches.all():
        recipe = getattr(mprb.recipe_batch, 'recipe', None)
        if recipe is not None and recipe.id not in ids:
            ids.append(recipe.id)
            if recipe.title:
                titles.append(recipe.title)
    return ids, titles


def pending_confirmations(user, *, now: datetime | None = None, days: int = 2, limit: int | None = None) -> list:
    """Repas planifiés passés à confirmer (aujourd'hui et hier par défaut ; `days=1` : aujourd'hui seulement)."""
    from notifications.services import get_preferences, local_now, user_zone
    from recipes.services.cooked_service import join_titles_fr

    prefs = get_preferences(user)
    zone = user_zone(prefs)
    local = local_now(prefs, now)
    today = local.date()
    dates = [today - timedelta(days=i) for i in range(max(days, 1))]
    plans = (
        MealPlan.objects.filter(user=user, confirmed=False, date__in=dates)
        .exclude(meal_type__in=_NOT_COOKED_TYPES)
        .annotate(
            _has_post=Exists(Post.objects.filter(meal_plan_id=OuterRef('pk'))),
            _dismissed=Exists(CookedConfirmationDismissal.objects.filter(user_id=user.id, meal_plan_id=OuterRef('pk'))),
        )
        .filter(_has_post=False, _dismissed=False)
        .prefetch_related(
            Prefetch(
                'meal_plan_recipe_batches',
                queryset=MealPlanRecipeBatch.objects.select_related('recipe_batch__recipe').order_by('order', 'id'),
            ),
            Prefetch(
                'basket_items',
                queryset=BasketItem.objects.filter(user=user).select_related('recipe').order_by('id'),
                to_attr='_basket',
            ),
        )
    )
    items: list[PendingItem] = []
    for mp in plans:
        when = due_at(mp, zone)
        if local < when:
            continue
        recipe_ids, titles = _recipes(mp)
        basket = mp._basket[0] if mp._basket else None
        label = (mp.custom_label or '').strip() if mp.meal_time in _STANDARD_SLOTS else ''
        title = join_titles_fr(titles) or (basket.display_title if basket else '') or label
        if not title:
            continue  # créneau vide ou nom de créneau seul : rien à confirmer
        items.append(PendingItem(mp, basket, title, recipe_ids, when))
    items.sort(key=lambda i: (i.due_at, i.meal_plan.id), reverse=True)
    return items[: limit if limit is not None else max_pending()]


def pending_payload(user, *, now: datetime | None = None, with_preview: bool = False) -> dict:
    items = pending_confirmations(user, now=now)
    out = []
    for item in items:
        data = item.payload()
        if with_preview:
            data['ardoise'] = _preview(user, item)
        out.append(data)
    return {'items': out, 'count': len(out)}


def _preview(user, item: PendingItem) -> dict | None:
    """Aperçu de L'ardoise (`?preview=1`, ≤ 3 calculs) ; jamais bloquant."""
    from recipes.services.cooked_service import CookedError
    from stats.services.highlights import preview_cooked

    try:
        data = preview_cooked(user, item.cooked_payload)
    except CookedError:
        return None
    return {'cuisine': data.get('cuisine'), 'highlights': data.get('highlights') or []}


def _resolve(user, kind: str, object_id) -> tuple[MealPlan, BasketItem | None]:
    if kind not in KINDS:
        raise PendingError('kind must be meal_plan or basket_item', 404, 'not_found')
    try:
        oid = int(object_id)
    except (TypeError, ValueError):
        raise PendingError('Not found', 404, 'not_found') from None
    if kind == KIND_MEAL_PLAN:
        meal_plan = MealPlan.objects.filter(id=oid, user=user).first()
        if meal_plan is None:
            raise PendingError('Meal plan not found', 404, 'meal_plan_not_found')
        return meal_plan, None
    item = BasketItem.objects.select_related('meal_plan').filter(id=oid, user=user).first()
    if item is None:
        raise PendingError('Basket item not found', 404, 'basket_item_not_found')
    if item.meal_plan_id is None:
        raise PendingError('Basket item is not scheduled', 400, 'basket_item_not_scheduled')
    return item.meal_plan, item


def dismiss(user, kind: str, object_id) -> CookedConfirmationDismissal:
    """« Pas fait » : n'est plus proposé. Le repas reste planifié, l'envie reste dans le panier."""
    meal_plan, item = _resolve(user, kind, object_id)
    dismissal, _ = CookedConfirmationDismissal.objects.get_or_create(
        user=user, meal_plan=meal_plan, defaults={'basket_item': item}
    )
    return dismissal


def undo_dismiss(user, kind: str, object_id) -> int:
    meal_plan, _ = _resolve(user, kind, object_id)
    deleted, _ = CookedConfirmationDismissal.objects.filter(user=user, meal_plan=meal_plan).delete()
    return deleted


def is_dismissed(meal_plan_id: int) -> bool:
    return CookedConfirmationDismissal.objects.filter(meal_plan_id=meal_plan_id).exists()
