"""Tâches d'import / génération de recettes (LLM et scraper mockés, exécution synchrone)."""

from decimal import Decimal
from unittest import mock

import pytest

from recipes.models import Recipe, RecipeImportRequest
from recipes.services.pydantic_models import (
    RecipeFormalized,
    RecipeIngredientFormalized,
    StepFormalized,
)
from recipes.tasks import process_recipe_generate_from_idea, process_recipe_import_from_url

pytestmark = pytest.mark.django_db


def _formalized(title='Gratin de courgettes'):
    return RecipeFormalized(
        title=title,
        description='Simple et rapide',
        steps_summary='Couper, gratiner.',
        prep_time=15,
        cook_time=30,
        servings=4,
        recipe_ingredients=[
            RecipeIngredientFormalized(ingredient_name='courgette', quantity=Decimal('600'), unit='g'),
            RecipeIngredientFormalized(ingredient_name='fromage de chèvre', quantity=Decimal('150'), unit='g'),
        ],
        steps=[
            StepFormalized(order=1, instruction='Couper les courgettes.'),
            StepFormalized(order=2, instruction='Gratiner 30 min.', has_timer=True, timer_duration=30),
        ],
    )


def _request(user, payload):
    return RecipeImportRequest.objects.create(user=user, payload=payload, status=RecipeImportRequest.STATUS_PENDING)


def test_generate_from_idea_creates_recipe(user):
    req = _request(user, {'idea_text': 'gratin courgettes chèvre', 'servings': 4, 'job_type': 'generate'})

    with mock.patch('recipes.tasks.generate_recipe_from_idea', new=mock.AsyncMock(return_value=_formalized())) as llm:
        process_recipe_generate_from_idea(str(req.id))

    llm.assert_awaited_once_with('gratin courgettes chèvre', 4)
    req.refresh_from_db()
    assert req.status == RecipeImportRequest.STATUS_SUCCESS
    recipe = req.recipe
    assert recipe.created_by == user and recipe.title == 'Gratin de courgettes'
    assert recipe.recipe_ingredients.count() == 2
    assert recipe.steps.count() == 2
    assert req.payload['import_progress'] == {'step': 'DONE', 'percent': 100}


def test_generate_from_idea_failure_marks_request_as_error(user):
    req = _request(user, {'idea_text': 'gratin courgettes chèvre'})

    with (
        mock.patch('recipes.tasks.generate_recipe_from_idea', new=mock.AsyncMock(side_effect=RuntimeError('LLM down'))),
        pytest.raises(RuntimeError),
    ):
        process_recipe_generate_from_idea(str(req.id))

    req.refresh_from_db()
    assert req.status == RecipeImportRequest.STATUS_ERROR
    assert 'LLM down' in req.error_message
    assert not Recipe.objects.exists()


def test_import_from_url_end_to_end_with_mocked_scraper_and_llm(user):
    url = 'https://www.example-recettes.test/gratin'
    req = _request(user, {'url': url, 'source_type': 'imported'})
    scraped = {
        'title': 'Gratin',
        'description': '',
        'ingredients_text': '600 g de courgettes\n150 g de chèvre\n20 cl de crème\n1 gousse d’ail',
        'instructions_text': 'Couper. Gratiner.',
        'image_path': '',
    }

    with (
        mock.patch('recipes.tasks.import_recipe_from_url', return_value=(scraped, 'recipe_scrapers')),
        mock.patch('recipes.tasks.formalize_recipe', new=mock.AsyncMock(return_value=_formalized('Gratin importé'))),
    ):
        process_recipe_import_from_url(str(req.id))

    req.refresh_from_db()
    assert req.status == RecipeImportRequest.STATUS_SUCCESS, req.error_message
    assert req.recipe.title == 'Gratin importé'
    assert req.recipe.import_source_url
    assert req.payload['import_extractor'] == 'recipe_scrapers'


def test_import_from_url_without_extractable_recipe_fails_gracefully(user):
    req = _request(user, {'url': 'https://www.example-recettes.test/rien'})

    with (
        mock.patch('recipes.tasks.import_recipe_from_url', return_value=(None, 'generic')),
        mock.patch('recipes.tasks.formalize_recipe') as llm,
    ):
        process_recipe_import_from_url(str(req.id))

    llm.assert_not_called()
    req.refresh_from_db()
    assert req.status == RecipeImportRequest.STATUS_ERROR
    assert req.payload['import_progress']['step'] == 'ERROR'
