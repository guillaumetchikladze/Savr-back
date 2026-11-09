"""
« Plan de bataille » (contrat front : .claude-work/specs/battle-plan.md).

    POST /api/battle-plan/   {meal_plan_id} | {basket_item_ids: [...]} | {recipe_ids: [...]}
                             + servings, start_time | target_ready_time (« 20:00 » ou ISO), polish (défaut true),
                             sub_recipe_modes ({recipe_ingredient_id: "make" | "buy"}, surcharge les choix des batches)

Lecture seule : rien n'est écrit en base. Inaccessible → 404.
"""

from __future__ import annotations

from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from accounts.permissions import IsValidated

from .services.battle_plan_service import BattlePlanError, battle_plan_for_user


def _falsy(value) -> bool:
    return str(value).strip().lower() in ('0', 'false', 'no')


@api_view(['POST'])
@permission_classes([IsValidated])
def battle_plan_view(request):
    data = request.data if isinstance(request.data, dict) else {}
    try:
        plan = battle_plan_for_user(
            request.user,
            meal_plan_id=data.get('meal_plan_id'),
            basket_item_ids=data.get('basket_item_ids'),
            recipe_ids=data.get('recipe_ids'),
            servings=data.get('servings'),
            start_time=data.get('start_time'),
            target_ready_time=data.get('target_ready_time'),
            polish=not _falsy(data.get('polish', True)),
            sub_recipe_modes=data.get('sub_recipe_modes'),
        )
    except BattlePlanError as exc:
        return Response({'error': exc.message, 'code': exc.code}, status=exc.status_code)
    return Response(plan)
