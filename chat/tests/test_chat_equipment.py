"""Agent v2 × « Ta cuisine » : équipements dans le contexte, règle du prompt, tool `adapt_recipe_equipment` (LLM mocké)."""

import json
from unittest import mock

import pytest

from chat.services import kitchen_tools
from chat.services.session_context import build_profile_context
from recipes.models import Step
from recipes.services.equipment_service import set_equipment

pytestmark = pytest.mark.django_db


def test_context_mentions_missing_equipment_and_tool_returns_an_adaptation_card(user, make_user, make_recipe):
    assert 'équipements non renseignés' in build_profile_context(user)
    set_equipment(user, {'equipment': [{'key': 'plaques', 'burners': 2}, {'key': 'airfryer'}]})

    ctx = build_profile_context(user)
    assert 'Cuisine (la tienne) : plaques de cuisson (2 feux), airfryer.' in ctx
    assert "Tu n'as pas : un four" in ctx and "ou propose l'adaptation" in ctx

    cake = make_recipe(make_user('chef'), 'Gâteau', servings=2)
    Step.objects.create(recipe=cake, order=1, instruction='Enfourner 30 min à 180 °C.')
    answer = {
        'title': "Gâteau à l'airfryer",
        'note': "Cuit à l'airfryer au lieu du four.",
        'steps': [{'title': 'Cuisson', 'instruction': "Cuis 25 min à l'airfryer à 160 °C.", 'timer_duration': 25}],
    }
    with mock.patch('recipes.services.equipment_adaptation._call_llm', return_value=json.dumps(answer)) as llm:
        card = kitchen_tools.adapt_recipe_equipment_for_chat(user, recipe_id=cake.id)
        again = kitchen_tools.adapt_recipe_equipment_for_chat(user, recipe_id=cake.id)
    assert llm.call_count == 1
    assert card.card_type == 'recipe_adaptation' and card.created is True and card.original_recipe_id == cake.id
    assert card.replaced == ['Four'] and card.note == "Cuit à l'airfryer au lieu du four."
    assert again.recipe_id == card.recipe_id and again.created is False
    with pytest.raises(ValueError):
        kitchen_tools.adapt_recipe_equipment_for_chat(user, recipe_id=999999)


def test_agent_registers_the_tool_and_the_equipment_rule(user):
    from chat.services.agent_factory import AGENT_INSTRUCTIONS, AGENT_TOOLS

    assert 'adapt_recipe_equipment' in {t.name for t in AGENT_TOOLS}
    assert 'ne propose pas de plat qui demande un équipement absent' in AGENT_INSTRUCTIONS
