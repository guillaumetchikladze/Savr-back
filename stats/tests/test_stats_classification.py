"""Classification ingrédients (seed + LLM mocké) et normalisation des cuisines (synonymes + LLM mocké)."""

from unittest import mock

import pytest
from django.core.management import call_command

from recipes.models import Ingredient, Recipe
from stats.cuisines import normalize_cuisine_styles
from stats.models import IngredientProfile
from stats.services import classification
from stats.services.classification import (
    IngredientClass,
    IngredientClassBatch,
    RecipeCuisine,
    RecipeCuisineBatch,
)

pytestmark = pytest.mark.django_db


def _fake_ingredient_llm(calls):
    def fake(system, prompt, schema):
        calls.append(prompt)
        assert schema is IngredientClassBatch
        items = []
        for line in prompt.split('Ingrédients (id<TAB>nom) :\n', 1)[1].splitlines():
            iid, name = line.split('\t')
            if name == 'kimchi':
                items.append(IngredientClass(id=int(iid), food_group='legumes', is_plant=True, is_basic_staple=False))
            elif name == 'yuzu':
                items.append(IngredientClass(id=int(iid), food_group='bogus', is_plant=True, is_basic_staple=False))
        return IngredientClassBatch(items=items)

    return fake


def test_classify_command_seed_llm_idempotent_and_validated_untouched():
    for name in ('sel', 'tomates', 'kimchi', 'yuzu', 'durian'):
        Ingredient.objects.create(name=name)
    validated = IngredientProfile.objects.create(
        ingredient=Ingredient.objects.create(name='miso'), food_group='autres', validated=True, source='manual'
    )
    calls = []

    with mock.patch.object(classification, '_call_llm', side_effect=_fake_ingredient_llm(calls)):
        call_command('classify_ingredients', '--only-missing', '--batch-size', '2')

    profiles = {p.ingredient.name: p for p in IngredientProfile.objects.select_related('ingredient')}
    assert profiles['sel'].is_basic_staple and profiles['sel'].source == 'manual'  # seed, sans LLM
    assert profiles['tomates'].food_group == 'legumes' and profiles['tomates'].is_plant
    assert profiles['kimchi'].source == 'llm' and profiles['kimchi'].food_group == 'legumes'
    assert profiles['yuzu'].food_group == 'autres'  # code hors liste → repli
    assert 'durian' not in profiles  # oublié par le LLM → repris au prochain passage
    assert profiles['miso'].food_group == 'autres' and validated.validated
    assert len(calls) == 2  # kimchi, yuzu, durian en lots de 2 ; sel/tomates/miso jamais envoyés

    calls.clear()
    with mock.patch.object(classification, '_call_llm', side_effect=_fake_ingredient_llm(calls)):
        call_command('classify_ingredients', '--only-missing')
    assert len(calls) == 1 and 'durian' in calls[0] and 'kimchi' not in calls[0]


def test_classify_failed_batch_does_not_stop_the_run():
    Ingredient.objects.create(name='kimchi')
    with mock.patch.object(classification, '_call_llm', side_effect=RuntimeError('quota')):
        report = classification.classify_ingredients(list(Ingredient.objects.all()))
    assert report.failed_batches == 1 and not IngredientProfile.objects.exists()


def test_new_ingredient_schedules_debounced_classification(django_capture_on_commit_callbacks, celery_delay):
    with django_capture_on_commit_callbacks(execute=True):
        Ingredient.objects.create(name='sumac')
        Ingredient.objects.create(name='za’atar')
    assert [c[0] for c in celery_delay.calls] == ['stats.tasks.classify_new_ingredients']


@pytest.mark.parametrize(
    ('styles', 'expected'),
    [
        (['Française'], 'francaise'),
        (['familiale', 'cuisine italienne du sud'], 'italienne'),
        (['Tex-Mex'], 'mexicaine'),
        (['thaïlandaise'], 'thai'),
        (['familiale', 'réconfortante'], None),
        ([], None),
    ],
)
def test_cuisine_synonyms(styles, expected):
    assert normalize_cuisine_styles(styles) == expected


def test_normalize_cuisines_command_llm_fallback(user, make_recipe):
    tagged = make_recipe(user, 'Bœuf bourguignon', search_context_tags={'cuisine_style': ['bourguignonne']})
    vague = make_recipe(user, 'Bibimbap', search_context_tags={'cuisine_style': ['familiale']})
    untagged = make_recipe(user, 'Truc')

    def fake(system, prompt, schema):
        assert schema is RecipeCuisineBatch and 'Bœuf' not in prompt
        return RecipeCuisineBatch(
            items=[RecipeCuisine(id=vague.id, cuisine='coreenne'), RecipeCuisine(id=untagged.id, cuisine='???')]
        )

    with mock.patch.object(classification, '_call_llm', side_effect=fake):
        call_command('normalize_cuisines', '--only-missing')

    assert dict(Recipe.objects.values_list('id', 'cuisine')) == {
        tagged.id: 'francaise',
        vague.id: 'coreenne',
        untagged.id: 'autre',
    }
