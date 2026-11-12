"""prv1 : toutes les recettes sont publiques par défaut, quel que soit le chemin de création ; le client ne peut
plus en créer une privée. Les recettes déjà privées le restent (pas de migration) tant qu'on ne les modifie pas."""

from decimal import Decimal

import pytest

from recipes.models import Recipe
from recipes.services.formalization_pipeline import create_recipe_from_formalized
from recipes.services.pydantic_models import RecipeFormalized, RecipeIngredientFormalized, StepFormalized

pytestmark = pytest.mark.django_db


def _formalized(title='Tarte aux pommes'):
    return RecipeFormalized(
        title=title,
        description='',
        steps_summary='Cuire.',
        prep_time=10,
        cook_time=30,
        servings=4,
        recipe_ingredients=[RecipeIngredientFormalized(ingredient_name='pommes', quantity=Decimal('4'), unit='piece')],
        steps=[StepFormalized(order=1, instruction='Cuire la tarte.')],
    )


def test_manual_create_is_public_even_when_private_is_asked(api, user):
    resp = api.as_user(user).post(
        '/api/recipes/',
        {'title': 'Crêpes', 'is_public': False, 'source_type': 'user_created', 'steps': [], 'ingredients': []},
        format='json',
    )

    assert resp.status_code == 201, resp.data
    assert Recipe.objects.get(id=resp.data['id']).is_public is True


def test_editor_save_makes_the_recipe_public(api, user, make_recipe):
    recipe = make_recipe(user, 'Quiche', is_public=False)

    resp = api.as_user(user).put(
        f'/api/recipes/{recipe.id}/edit/', {'title': 'Quiche lorraine', 'is_public': False}, format='json'
    )

    assert resp.status_code == 200, resp.data
    assert Recipe.objects.get(id=recipe.id).is_public is True


@pytest.mark.parametrize('source_type', ['imported', 'user_created', 'generated', 'instagram'])
def test_import_and_ai_paths_create_public_recipes(user, source_type):
    recipe = create_recipe_from_formalized(_formalized(), {'source_type': source_type, 'is_public': False}, user)

    assert recipe.is_public is True and recipe.created_by == user


def test_existing_private_recipe_stays_private_and_visible_to_its_owner_only(api, user, make_user, make_recipe):
    recipe = make_recipe(user, 'Mon secret', is_public=False)

    assert api.as_user(user).get(f'/api/recipes/{recipe.id}/').data['is_public'] is False
    assert api.as_user(make_user()).get(f'/api/recipes/{recipe.id}/').status_code == 404
