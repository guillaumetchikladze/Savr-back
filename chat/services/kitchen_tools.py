"""
Actions « cuisine » de l'agent v2 (sync, testables sans LLM) : panier « Cette semaine »,
« J'ai cuisiné », courses depuis le panier, et leurs annulations.

Les wrappers async exposés au modèle sont dans `async_tools.py`. Toutes ces actions sont
réversibles : elles s'exécutent sans carte de confirmation et renvoient de quoi annuler
(`UndoHint` pour l'app, tools `remove_from_basket` / `undo_log_cooked` pour le chat).
"""

from __future__ import annotations

import logging
from datetime import date as date_type
from datetime import datetime, time, timedelta

from django.contrib.auth.models import AbstractBaseUser
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from chat.services.tool_schemas import (
    BasketAddition,
    BasketRemovalResult,
    BasketShoppingResult,
    BattlePlanResult,
    EquipmentAdaptationResult,
    LogCookedResult,
    PlanWeekResult,
    SkippedDish,
    UndoCookedResult,
    UndoHint,
)
from recipes.dietary_filters import conflict_details_by_recipe_id, text_conflict_hits
from recipes.models import Post, Recipe
from recipes.services import basket_service as basket
from recipes.services.basket_service import BasketError
from recipes.services.cooked_service import CookedError, post_display_title, undo_cooked
from recipes.services.cooked_service import log_cooked as log_cooked_service

logger = logging.getLogger(__name__)

MAX_DISHES_PER_CALL = 14
_REASON_FR = {'allergy': 'allergie', 'diet': 'régime', 'dislike': "n'aime pas"}

_MEAL_TIME_ALIASES = {
    'breakfast': 'breakfast',
    'petit-dejeuner': 'breakfast',
    'petit dejeuner': 'breakfast',
    'petit-déjeuner': 'breakfast',
    'petit déjeuner': 'breakfast',
    'matin': 'breakfast',
    'lunch': 'lunch',
    'midi': 'lunch',
    'ce midi': 'lunch',
    'dejeuner': 'lunch',
    'déjeuner': 'lunch',
    'dinner': 'dinner',
    'soir': 'dinner',
    'ce soir': 'dinner',
    'diner': 'dinner',
    'dîner': 'dinner',
    'other': 'other',
    'gouter': 'other',
    'goûter': 'other',
}
# Heure « murale » par défaut d'un repas logué a posteriori (« hier soir »).
_DEFAULT_HOUR = {'breakfast': time(8, 30), 'lunch': time(12, 30), 'dinner': time(20, 0), 'other': time(16, 0)}

_RELATIVE_DAYS = {
    "aujourd'hui": 0,
    'aujourd’hui': 0,
    'aujourdhui': 0,
    'today': 0,
    'hier': -1,
    'yesterday': -1,
    'avant-hier': -2,
    'avant hier': -2,
    'demain': 1,
    'tomorrow': 1,
    'après-demain': 2,
    'apres-demain': 2,
}


# ---------------------------------------------------------------------------
# Parsing tolérant (le modèle envoie parfois « hier » ou « ce midi » tels quels)
# ---------------------------------------------------------------------------
def parse_day(value, today: date_type) -> date_type:
    raw = str(value or '').strip().lower()
    if raw in _RELATIVE_DAYS:
        return today + timedelta(days=_RELATIVE_DAYS[raw])
    try:
        return date_type.fromisoformat(raw[:10])
    except ValueError as exc:
        raise ValueError('date attendue au format YYYY-MM-DD (ou « hier », « aujourd’hui »).') from exc


def normalize_meal_time(value) -> str | None:
    raw = str(value or '').strip().lower()
    if not raw:
        return None
    if raw not in _MEAL_TIME_ALIASES:
        raise ValueError('meal_time : breakfast, lunch, dinner ou other.')
    return _MEAL_TIME_ALIASES[raw]


def _accessible_recipes(user: AbstractBaseUser, ids) -> dict[int, Recipe]:
    ids = [int(i) for i in ids]
    if not ids:
        return {}
    qs = Recipe.objects.filter(Q(is_public=True) | Q(created_by=user), id__in=ids).only('id', 'title')
    return {r.id: r for r in qs}


def _dedupe(values) -> list:
    return list(dict.fromkeys(v for v in values if v not in (None, '')))


def _conflict_detail(hit: dict, who: str = '') -> str:
    reason = _REASON_FR.get(hit['reason'], hit['reason'])
    owner = f' ({who})' if who else ''
    return f'{reason}{owner} : {hit["label"]}'


def _household_account_members(user: AbstractBaseUser) -> list:
    """Comptes du foyer (leurs allergies comptent aussi pour les plats de la semaine)."""
    from accounts.services.household_service import household_for

    return [(e.name, e.user) for e in household_for(user.id) if e.user is not None]


# ---------------------------------------------------------------------------
# plan_week : envies du panier (sans date par défaut)
# ---------------------------------------------------------------------------
def plan_week_for_user(
    user: AbstractBaseUser,
    *,
    recipe_ids=None,
    labels=None,
    week: str | None = None,
    date: str | None = None,
    meal_time: str | None = None,
    servings: int | None = None,
    allow_conflicts: bool = False,
) -> PlanWeekResult:
    """
    Ajoute des plats au panier de la semaine. Sans `date` : envies non datées (« Cette semaine »).
    Avec `date` (+ `meal_time`, défaut dîner) : chaque envie est aussi datée (vrai repas planifié).
    Les plats en conflit avec les allergies / régime / goûts de l'utilisateur, ou avec les allergies
    d'un membre du foyer, sont ignorés (`skipped`) sauf `allow_conflicts` (demande explicite).
    """
    try:
        rids = _dedupe(int(r) for r in (recipe_ids or []))
    except (TypeError, ValueError) as exc:
        raise ValueError('recipe_ids doit être une liste d’entiers.') from exc
    texts = _dedupe(str(lbl).strip()[: basket.LABEL_MAX_LENGTH] for lbl in (labels or []))
    if not rids and not texts:
        raise ValueError('Indique au moins un plat (recipe_ids ou labels).')
    if len(rids) + len(texts) > MAX_DISHES_PER_CALL:
        raise ValueError(f'{MAX_DISHES_PER_CALL} plats maximum par appel.')

    today = basket.user_today(user)
    target_date = parse_day(date, today) if date else None
    slot = (normalize_meal_time(meal_time) or 'dinner') if target_date else None
    week_start = basket.monday_of(target_date) if target_date else basket.parse_week(week, user)

    recipes = _accessible_recipes(user, rids)
    recipe_conflicts = conflict_details_by_recipe_id(list(recipes), user) if recipes else {}
    members = _household_account_members(user)
    member_recipe_conflicts = [
        (name, conflict_details_by_recipe_id(list(recipes), member)) for name, member in members if recipes
    ]

    result = PlanWeekResult(week_start=week_start.isoformat())

    def conflicts_for(recipe_id: int | None, text: str) -> list[tuple[str, str]]:
        """[(raison, détail)] : préférences de l'utilisateur + allergies des comptes du foyer."""
        if recipe_id is not None:
            own = (recipe_conflicts.get(recipe_id) or {}).get('hits', [])
            others = [
                (name, h)
                for name, by_id in member_recipe_conflicts
                for h in (by_id.get(recipe_id) or {}).get('hits', [])
                if h['reason'] == 'allergy'
            ]
        else:
            own = text_conflict_hits(text, user)
            others = [
                (name, h) for name, member in members for h in text_conflict_hits(text, member, kinds=('allergy',))
            ]
        return [(h['reason'], _conflict_detail(h)) for h in own] + [
            (h['reason'], _conflict_detail(h, name)) for name, h in others
        ]

    dishes = [(rid, '') for rid in rids] + [(None, text) for text in texts]
    for recipe_id, text in dishes:
        if recipe_id is not None and recipe_id not in recipes:
            result.skipped.append(SkippedDish(recipe_id=recipe_id, reason='recipe_not_found'))
            continue
        title = recipes[recipe_id].title if recipe_id is not None else text
        conflicts = conflicts_for(recipe_id, title)
        if conflicts and not allow_conflicts:
            reasons = {r for r, _ in conflicts}
            reason = next(k for k in ('allergy', 'diet', 'dislike') if k in reasons)
            detail = ' ; '.join(d for _, d in conflicts)
            result.skipped.append(SkippedDish(recipe_id=recipe_id, label=title, reason=reason, detail=detail))
            continue
        try:
            with transaction.atomic():  # envie + datation : tout ou rien
                item = basket.add_item(
                    user,
                    recipe_id=recipe_id,
                    label=text or None,
                    servings=servings,
                    week=week_start.isoformat(),
                )
                if target_date:
                    item = basket.schedule_item(user, item, date=target_date.isoformat(), meal_time=slot)
        except BasketError as exc:
            result.skipped.append(SkippedDish(recipe_id=recipe_id, label=title, reason='invalid', detail=exc.message))
            continue
        result.added.append(
            BasketAddition(
                basket_item_id=item.id,
                display_title=item.display_title,
                recipe_id=item.recipe_id,
                is_free_text=item.recipe_id is None,
                week_start=item.week_start.isoformat(),
                status='planned' if item.meal_plan_id else 'todo',
                date=item.meal_plan.date.isoformat() if item.meal_plan_id else None,
                meal_time=item.meal_plan.meal_time if item.meal_plan_id else None,
                meal_plan_id=item.meal_plan_id,
                undo=UndoHint(method='DELETE', path=f'/api/basket/{item.id}/', label='Retirer'),
            )
        )

    result.message = _plan_week_message(result, dated=bool(target_date))
    return result


def _plan_week_message(result: PlanWeekResult, *, dated: bool) -> str:
    parts = []
    if result.added:
        titles = ', '.join(a.display_title for a in result.added[:5])
        where = 'au planning' if dated else 'au panier de la semaine'
        parts.append(f'{len(result.added)} plat(s) ajouté(s) {where} : {titles}.')
    if result.skipped:
        parts.append(
            'Ignoré(s) : ' + ', '.join(f'{s.label or s.recipe_id} ({s.detail or s.reason})' for s in result.skipped)
        )
    return ' '.join(parts) or 'Aucun plat ajouté.'


def remove_basket_items(user: AbstractBaseUser, basket_item_ids) -> BasketRemovalResult:
    """Annule un ajout au panier (envie retirée ; un repas déjà cuisiné reste dans l'historique)."""
    try:
        ids = _dedupe(int(i) for i in (basket_item_ids or []))
    except (TypeError, ValueError) as exc:
        raise ValueError('basket_item_ids doit être une liste d’entiers.') from exc
    if not ids:
        raise ValueError('Indique au moins une envie (basket_item_ids).')
    removed, titles = [], []
    for item in basket.basket_queryset(user).filter(id__in=ids).order_by('id'):
        removed.append(item.id)
        titles.append(item.display_title)
        basket.remove_item(item)
    if not removed:
        raise ValueError('Aucune envie trouvée dans ton panier avec ces ids.')
    return BasketRemovalResult(removed_basket_item_ids=removed, message=f'Retiré du panier : {", ".join(titles)}.')


# ---------------------------------------------------------------------------
# log_cooked : « J'ai cuisiné X »
# ---------------------------------------------------------------------------
def _cooked_at(day: date_type, meal_time: str | None, today: date_type) -> datetime | None:
    """Heure du log : maintenant pour aujourd'hui (sauf repas déjà passé), heure type du repas sinon."""
    if day == today and not meal_time:
        return None
    local = timezone.make_aware(datetime.combine(day, _DEFAULT_HOUR[meal_time or 'dinner']))
    if local > timezone.now():
        return None  # « ce soir » alors qu'il est 19 h : maintenant, créneau explicite
    return local


def log_cooked_for_user(
    user: AbstractBaseUser,
    *,
    recipe_id: int | None = None,
    label: str | None = None,
    basket_item_id: int | None = None,
    date: str | None = None,
    meal_time: str | None = None,
    comment: str | None = None,
    publish: bool | None = None,
) -> LogCookedResult:
    """
    « J'ai cuisiné X » via le flux canonique (`cooked_service.log_cooked`, comme POST /posts/cooked/) :
    repas confirmé, envie du panier cochée, post (publié selon `publish`, sinon selon le réglage
    `auto_publish_cooked` de l'utilisateur), badges débloqués et ardoise.
    """
    today = timezone.localdate()
    day = parse_day(date, today) if date else today
    if day > today:
        raise ValueError('Ce plat n’est pas encore cuisiné : utilise plan_week pour le prévoir.')
    slot = normalize_meal_time(meal_time)
    if recipe_id is not None and not _accessible_recipes(user, [recipe_id]):
        raise ValueError(f'Recette {recipe_id} introuvable.')
    if publish is None:
        publish = bool(getattr(user, 'auto_publish_cooked', False))

    try:
        cooked = log_cooked_service(
            user,
            recipe_id=recipe_id,
            label=label,
            basket_item_id=basket_item_id,
            cooked_at=_cooked_at(day, slot, today),
            meal_time=slot,
            comment=comment,
            publish=bool(publish),
        )
    except CookedError as exc:
        raise ValueError(exc.message) from exc

    from stats.services.badges import award_badges_safely

    post = Post.objects.select_related('meal_plan', 'recipe_batch__recipe').get(pk=cooked.post.pk)
    new_badges = award_badges_safely(user)
    ardoise = _safe_ardoise(post)
    meal_plan = post.meal_plan
    title = post_display_title(post) or (label or '').strip()
    basket_item = meal_plan.basket_items.order_by('id').first() if meal_plan else None

    message = f'« {title} » noté comme cuisiné' + (' et publié.' if post.is_published else ' (privé).')
    if not cooked.created:
        message = f'« {title} » était déjà noté ce jour-là.'
    return LogCookedResult(
        post_id=post.id,
        meal_plan_id=post.meal_plan_id,
        created=cooked.created,
        is_published=post.is_published,
        display_title=title,
        date=(meal_plan.date if meal_plan else day).isoformat(),
        meal_time=meal_plan.meal_time if meal_plan else (slot or ''),
        basket_item_id=basket_item.id if basket_item else None,
        cuisine=ardoise.get('cuisine'),
        highlights=ardoise.get('highlights') or [],
        new_badges=new_badges,
        undo=UndoHint(method='DELETE', path=f'/api/posts/{post.id}/?undo_cooked=true'),
        message=message,
    )


def _safe_ardoise(post: Post) -> dict:
    try:
        from stats.services.highlights import post_ardoise

        return post_ardoise(post)
    except Exception:  # l'ardoise ne doit jamais faire échouer le log
        logger.exception('ardoise indisponible pour le post %s', post.pk)
        return {}


UNDO_LAST_COOKED_WINDOW = timedelta(hours=6)


def undo_cooked_for_user(user: AbstractBaseUser, post_id: int | None = None) -> UndoCookedResult:
    """
    Annule un log « J'ai cuisiné » (même effet que DELETE /posts/{id}/?undo_cooked=true). Sans `post_id` :
    le dernier log « J'ai cuisiné » de l'utilisateur des 6 dernières heures (« annule » juste après).
    """
    posts = Post.objects.select_related('meal_plan', 'user').filter(user=user)
    if post_id is not None:
        post = posts.filter(pk=int(post_id)).first()
    else:
        post = (
            posts.filter(cooked_undo__isnull=False, created_at__gte=timezone.now() - UNDO_LAST_COOKED_WINDOW)
            .order_by('-created_at', '-id')
            .first()
        )
    if not post:
        raise ValueError('Aucun « J’ai cuisiné » à annuler (post introuvable).')
    title = post_display_title(post) or 'ce plat'
    pk = post.pk
    undo_cooked(post)
    return UndoCookedResult(post_id=pk, message=f'« {title} » n’est plus noté comme cuisiné.')


# ---------------------------------------------------------------------------
# Courses depuis le panier
# ---------------------------------------------------------------------------
def basket_to_shopping_list(
    user: AbstractBaseUser,
    *,
    basket_item_ids=None,
    week: str | None = None,
    shopping_list_id: int | None = None,
    name: str | None = None,
) -> BasketShoppingResult:
    """Ingrédients des envies (avec recette) → liste de courses (même service que POST /basket/shopping-list/)."""
    try:
        items = basket.items_for_shopping(user, item_ids=basket_item_ids or None, week=week)
        if not items:
            raise ValueError(
                'Aucune envie avec recette à ajouter (les plats libres n’ont pas d’ingrédients) : '
                'propose de créer leur recette.'
            )
        result = basket.add_to_shopping_list(user, items, shopping_list_id=shopping_list_id, name=name)
    except BasketError as exc:
        raise ValueError(exc.message) from exc

    shopping_list = result.shopping_list
    added = len(result.added_item_ids)
    message = f'Ingrédients de {added} plat(s) ajoutés à « {shopping_list.name} ».'
    if not added:
        message = f'Rien de nouveau à ajouter à « {shopping_list.name} ».'
    no_recipe = sum(1 for s in result.skipped if s.get('reason') == 'no_recipe')
    if not basket_item_ids:  # semaine entière : les plats libres ont été écartés en amont
        week_now = basket.current_week_start(user)
        no_recipe += sum(
            1
            for i in basket.week_items(user, basket.parse_week(week, user))
            if not i.recipe_id and basket.item_status(i, week_now) in basket.OPEN_STATUSES
        )
    if no_recipe:
        message += f' {no_recipe} plat(s) libre(s) sans recette ignoré(s).'
    return BasketShoppingResult(
        shopping_list_id=shopping_list.id,
        shopping_list_name=shopping_list.name,
        created_list=result.created_list,
        added_basket_item_ids=result.added_item_ids,
        skipped=result.skipped,
        message=message,
    )


# ---------------------------------------------------------------------------
# « Adapter pour chez moi » : recette qui demande un équipement absent
# ---------------------------------------------------------------------------
def adapt_recipe_equipment_for_chat(user: AbstractBaseUser, *, recipe_id: int) -> EquipmentAdaptationResult:
    """Copie privée adaptée à la cuisine de l'utilisateur (même service que POST /api/recipes/<id>/adapt-equipment/)."""
    from recipes.services import equipment_catalog as catalog
    from recipes.services.equipment_adaptation import AdaptationError, adapt_recipe_for_user

    try:
        copy, created, missing = adapt_recipe_for_user(user, recipe_id)
    except AdaptationError as exc:
        raise ValueError(exc.message) from exc
    replaced = [m['label'] for m in missing]
    note = (copy.description or '').split('\n\n', 1)[0]  # 1er paragraphe = note de l'adaptation
    if created:
        message = f'Version adaptée prête : « {copy.title} ».'
    else:
        message = f'Tu as déjà une version adaptée : « {copy.title} ».'
    return EquipmentAdaptationResult(
        recipe_id=copy.id,
        original_recipe_id=copy.adapted_from_id or int(recipe_id),
        title=copy.title,
        image_url=copy.image_url,
        created=created,
        adapted_for=[catalog.label(k) for k in copy.adapted_for_equipment or []],
        replaced=replaced,
        note=note[:300],
        message=message,
    )


# ---------------------------------------------------------------------------
# Plan de bataille : « je fais X et Y ce soir, dans quel ordre ? »
# ---------------------------------------------------------------------------
def battle_plan_for_chat(
    user: AbstractBaseUser,
    *,
    recipe_ids=None,
    basket_item_ids=None,
    meal_plan_id: int | None = None,
    servings: int | None = None,
    start_time: str | None = None,
    target_ready_time: str | None = None,
) -> BattlePlanResult:
    """Ordre des étapes de plusieurs recettes (déterministe, même service que POST /api/battle-plan/)."""
    from recipes.services.battle_plan_service import BattlePlanError, battle_plan_for_user

    try:
        plan = battle_plan_for_user(
            user,
            meal_plan_id=meal_plan_id,
            basket_item_ids=list(basket_item_ids or []),
            recipe_ids=list(recipe_ids or []),
            servings=servings,
            start_time=start_time,
            target_ready_time=target_ready_time,
            polish=False,  # le modèle du chat reformule déjà : pas de second appel LLM
        )
    except BattlePlanError as exc:
        raise ValueError(exc.message) from exc
    message = ' '.join(plan['summary'][:3])
    if len(plan['summary']) > 3:
        message += ' ' + plan['summary'][-1]
    return BattlePlanResult(**plan, message=message)
