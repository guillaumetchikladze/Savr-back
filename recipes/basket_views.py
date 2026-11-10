"""
API du panier « Cette semaine » (envies non datées). Contrat front : `.claude-work/specs/basket.md`.

    GET    /basket/?week=YYYY-MM-DD          semaine (défaut : courante, fuseau de l'utilisateur)
    POST   /basket/                          {recipe_id | label, servings?, week?, guest_count?, excluded_household_ids?,
                                              date?, meal_time?}  date → envie datée tout de suite (tout ou rien)
    PATCH  /basket/{id}/                     {servings?, label?, guest_count?, excluded_household_ids?}
    POST   /basket/diners/                   {week?, guest_count?, excluded_household_ids?}  en-tête « Pour N »
    DELETE /basket/{id}/
    POST   /basket/reorder/                  {item_ids, week?}
    POST   /basket/{id}/schedule/            {date, meal_time? | meal_plan_id}  → repas planifié normal
    DELETE /basket/{id}/schedule/            → de nouveau sans date
    POST   /basket/carry-over/               {from_week?, to_week?, item_ids?}
    POST   /basket/shopping-list/            {item_ids? | week?, shopping_list_id? | name?}
    GET|POST /basket/{id}/sub-recipe-modes/  {modes: {recipe_ingredient_id: "buy" | "make"}, replace?}

Cuisiner une envie : `POST /posts/cooked/` avec `basket_item_id` (flux « J'ai cuisiné » canonique).
« À mon tour » : `POST|DELETE /posts/{id}/my-turn/` (PostViewSet).
"""

from __future__ import annotations

from datetime import timedelta
from functools import wraps

from django.db import transaction
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.permissions import IsValidated

from .models import BasketItem
from .serializers import ShoppingListSerializer, UserLightSerializer
from .services import basket_service as basket
from .services.basket_service import BasketError
from .services.diners_service import basket_item_composition, context_cache, diners_payload


def _basket_errors(view):
    @wraps(view)
    def wrapper(self, request, *args, **kwargs):
        try:
            return view(self, request, *args, **kwargs)
        except BasketError as exc:
            return Response({'error': exc.message, 'code': exc.code}, status=exc.status_code)

    return wrapper


def basket_item_payload(
    item, request, *, week_now=None, cooked_posts: dict | None = None, context: dict | None = None
) -> dict:
    week_now = week_now or basket.current_week_start(request.user)
    recipe = item.recipe if item.recipe_id else None
    meal_plan = item.meal_plan if item.meal_plan_id else None
    original = item.inspired_by_post if item.inspired_by_post_id else None
    if cooked_posts is None:
        cooked_posts = basket.cooked_post_ids([item])
    context = context if context is not None else {'request': request}
    composition = basket_item_composition(item, context_cache(context))
    return {
        'id': item.id,
        'week_start': item.week_start.isoformat(),
        'position': item.position,
        'status': basket.item_status(item, week_now),
        'source': 'my_turn' if item.inspired_by_post_id else 'manual',
        'display_title': item.display_title,
        'label': item.label,
        'servings': item.servings,
        'guest_count': item.guest_count,
        'excluded_household_ids': list(item.excluded_household_ids or []),
        'diners': diners_payload(composition, context),
        'servings_suggested': composition.total,
        'recipe': (
            {
                'id': recipe.id,
                'title': recipe.title,
                'image_url': recipe.image_url,
                'servings': recipe.servings,
                'prep_time': recipe.prep_time,
                'cook_time': recipe.cook_time,
            }
            if recipe
            else None
        ),
        'recipe_batch_id': item.recipe_batch_id,
        # Choix acheter / faire maison explicites (sous-recettes) ; absent = default_mode de la ligne.
        'sub_recipe_modes': dict(item.recipe_batch.sub_recipe_modes or {}) if item.recipe_batch_id else {},
        'meal_plan': (
            {
                'id': meal_plan.id,
                'date': meal_plan.date.isoformat(),
                'meal_time': meal_plan.meal_time,
                'slot_key': meal_plan.slot_key,
                'custom_label': meal_plan.custom_label,
                'confirmed': meal_plan.confirmed,
            }
            if meal_plan
            else None
        ),
        'cooked_post_id': cooked_posts.get(meal_plan.id) if meal_plan else None,
        'inspired_by': (
            {
                'post_id': original.id,
                'user': UserLightSerializer(original.user, context=context).data if original.user_id else None,
            }
            if original
            else None
        ),
        'carried_over_to_id': item.carried_over_to_id,
        'created_at': item.created_at.isoformat(),
    }


def _items_payload(items, request, week_now, context: dict | None = None) -> list[dict]:
    cooked_posts = basket.cooked_post_ids(items)
    context = context if context is not None else {'request': request}
    return [
        basket_item_payload(i, request, week_now=week_now, cooked_posts=cooked_posts, context=context) for i in items
    ]


class BasketViewSet(viewsets.ViewSet):
    """Panier « Cette semaine » : envies sans date, datables, cuisinables, reportables."""

    permission_classes = [IsValidated]  # compte validé (liste d'attente exclue)

    def _week_payload(self, request, week):
        user = request.user
        week_now = basket.current_week_start(user)
        items = basket.week_items(user, week)
        context = {'request': request}
        payload = _items_payload(items, request, week_now, context)
        counts = {s: 0 for s in ('todo', 'planned', 'cooked', 'carried_over', 'not_done')}
        for entry in payload:
            counts[entry['status']] += 1
        data = {
            'week_start': week.isoformat(),
            'week_end': (week + timedelta(days=6)).isoformat(),
            'is_current_week': week == week_now,
            'items': payload,
            'counts': counts,
            'diners': self._week_diners(user, week, context),
            'previous_week': None,
        }
        if week == week_now:
            previous = week - timedelta(days=7)
            data['previous_week'] = {
                'week_start': previous.isoformat(),
                'open_count': basket.open_items_count(user, previous),
            }
        return data

    @staticmethod
    def _week_diners(user, week, context) -> dict:
        """En-tête « Pour N » : composition de la semaine (celle que prend une nouvelle envie)."""
        guests, excluded = basket.week_composition(user, week)
        item = BasketItem(user=user, guest_count=guests, excluded_household_ids=excluded)
        composition = basket_item_composition(item, context_cache(context))
        return {
            **diners_payload(composition, context),
            'excluded_household_ids': excluded,
            'servings_suggested': composition.total,
        }

    @_basket_errors
    def list(self, request):
        week = basket.parse_week(request.query_params.get('week'), request.user)
        return Response(self._week_payload(request, week))

    @_basket_errors
    def create(self, request):
        data = request.data
        # `date` (+ `meal_time`, défaut dîner) : ajout + datation en une fois (cartes « Complète ma semaine »).
        day = data.get('date')
        with transaction.atomic():
            item = basket.add_item(
                request.user,
                recipe_id=data.get('recipe_id'),
                label=data.get('label'),
                servings=data.get('servings'),
                week=day or data.get('week'),
                guest_count=data.get('guest_count'),
                excluded_household_ids=data.get('excluded_household_ids'),
            )
            if day:
                basket.schedule_item(request.user, item, date=day, meal_time=data.get('meal_time'))
        item = basket.get_item(request.user, item.id)
        return Response(basket_item_payload(item, request), status=status.HTTP_201_CREATED)

    @_basket_errors
    def retrieve(self, request, pk=None):
        return Response(basket_item_payload(basket.get_item(request.user, pk), request))

    @_basket_errors
    def partial_update(self, request, pk=None):
        item = basket.get_item(request.user, pk)
        fields = ('servings', 'label', 'guest_count', 'excluded_household_ids')
        kwargs = {k: request.data.get(k) for k in fields if k in request.data}
        basket.update_item(item, **kwargs)
        return Response(basket_item_payload(basket.get_item(request.user, pk), request))

    @_basket_errors
    def destroy(self, request, pk=None):
        basket.remove_item(basket.get_item(request.user, pk))
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['get', 'post'], url_path='sub-recipe-modes')
    @_basket_errors
    def sub_recipe_modes(self, request, pk=None):
        """Acheter / faire maison par sous-recette pour cette envie (batch créé à la demande)."""
        from .services.sub_recipe_service import SubRecipeError
        from .sub_recipe_views import apply_modes, batch_modes_payload

        item = basket.get_item(request.user, pk)
        if not item.recipe_id:
            raise BasketError('Envie sans recette.', 400, 'no_recipe')
        batch = basket.ensure_batch(item)
        if request.method == 'POST':
            body = request.data if isinstance(request.data, dict) else {}
            try:
                apply_modes(batch, {'modes': body.get('modes'), 'replace': body.get('replace')})
            except SubRecipeError as exc:
                raise BasketError(exc.message, exc.status_code, exc.code) from exc
        return Response(batch_modes_payload(batch, request.user))

    @action(detail=False, methods=['post'])
    @_basket_errors
    def reorder(self, request):
        week = basket.parse_week(request.data.get('week'), request.user)
        basket.reorder(request.user, week, request.data.get('item_ids'))
        return Response(self._week_payload(request, week))

    @action(detail=True, methods=['post', 'delete'])
    @_basket_errors
    def schedule(self, request, pk=None):
        item = basket.get_item(request.user, pk)
        if request.method == 'DELETE':
            basket.unschedule_item(item)
        else:
            data = request.data
            if not data.get('meal_plan_id') and not data.get('date'):
                raise BasketError('date or meal_plan_id is required')
            basket.schedule_item(
                request.user,
                item,
                date=data.get('date'),
                meal_time=data.get('meal_time'),
                meal_plan_id=data.get('meal_plan_id'),
            )
        return Response(basket_item_payload(basket.get_item(request.user, pk), request))

    @action(detail=False, methods=['post'])
    @_basket_errors
    def diners(self, request):
        """En-tête « Pour N » : stepper invités / membre du foyer décoché pour toutes les envies non datées."""
        data = request.data
        week = basket.parse_week(data.get('week'), request.user)
        kwargs = {k: data.get(k) for k in ('guest_count', 'excluded_household_ids') if k in data}
        basket.set_week_diners(request.user, week, **kwargs)
        return Response(self._week_payload(request, week))

    @action(detail=False, methods=['post'], url_path='carry-over')
    @_basket_errors
    def carry_over(self, request):
        data = request.data
        created = basket.carry_over(
            request.user,
            from_week=data.get('from_week'),
            to_week=data.get('to_week'),
            item_ids=data.get('item_ids'),
        )
        week_now = basket.current_week_start(request.user)
        target = created[0].week_start if created else week_now
        return Response(
            {'carried_over_count': len(created), 'week': self._week_payload(request, target)},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @action(detail=False, methods=['post'], url_path='shopping-list')
    @_basket_errors
    def shopping_list(self, request):
        data = request.data
        user = request.user
        items = basket.items_for_shopping(user, item_ids=data.get('item_ids'), week=data.get('week'))
        result = basket.add_to_shopping_list(
            user, items, shopping_list_id=data.get('shopping_list_id'), name=data.get('name')
        )
        return Response(
            {
                'shopping_list': ShoppingListSerializer(result.shopping_list, context={'request': request}).data,
                'created_list': result.created_list,
                'added_item_ids': result.added_item_ids,
                'skipped': result.skipped,
            },
            status=status.HTTP_201_CREATED if result.created_list else status.HTTP_200_OK,
        )
