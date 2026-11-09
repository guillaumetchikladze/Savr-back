"""Fiche produit des courses : cache en base, génération LLM (mockée), repli de modèle, échecs, permissions."""

import json
import logging
from unittest import mock

import httpx
import openai
import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.test.utils import override_settings

from recipes.models import Ingredient, IngredientInfo, ShoppingListItem
from recipes.services import ingredient_info as service

pytestmark = pytest.mark.django_db

GOOD = {
    'what': "Le céleri-rave est la grosse racine d'une variété de céleri. Sa chair est ferme et parfumée.",
    'uses': 'Râpé en rémoulade, en purée ou rôti au four.',
    'aisle': 'Fruits et légumes.',
    'choose': 'Prends-le lourd et ferme, sans taches molles.',
    'keep': "Garde-le au frais dans le bac à légumes jusqu'à deux semaines.",
    'substitutes': ['Panais', 'Navet', 'Céleri branche'],
}


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def ingredient(db):
    return Ingredient.objects.create(name='céleri-rave')


def _url(ingredient_id):
    return f'/api/ingredients/{ingredient_id}/info/'


def _llm(*responses):
    return mock.patch.object(service, '_call_llm', side_effect=list(responses))


def _store(ingredient, **extra):
    data = {**GOOD, 'aisle': 'Fruits et légumes', 'source': 'manual', **extra}
    return IngredientInfo.objects.create(ingredient=ingredient, **data)


# ---------------------------------------------------------------------------
# Cache et génération
# ---------------------------------------------------------------------------
def test_cached_info_is_returned_without_llm(api, user, ingredient):
    _store(ingredient)
    with mock.patch.object(service, '_call_llm') as llm:
        resp = api.as_user(user).get(_url(ingredient.id))
    assert resp.status_code == 200
    llm.assert_not_called()
    assert resp.data['cached'] is True
    assert resp.data['ingredient'] == {'id': ingredient.id, 'name': 'céleri-rave', 'category': None}
    assert resp.data['aisle'] == 'Fruits et légumes'
    assert resp.data['substitutes'] == ['Panais', 'Navet', 'Céleri branche']
    assert resp.data['source'] == 'manual'


@override_settings(INGREDIENT_INFO_MODEL='gemini-3.5-flash', CHAT_AI_MODEL='gemini-2.5-flash')
def test_missing_info_is_generated_stored_and_shared(api, user, make_user, ingredient):
    with _llm(json.dumps(GOOD)) as llm:
        resp = api.as_user(user).get(_url(ingredient.id))
    assert resp.status_code == 200, resp.data
    assert resp.data['cached'] is False
    assert resp.data['model'] == 'gemini-3.5-flash'
    assert resp.data['aisle'] == 'Fruits et légumes'  # point final retiré
    assert llm.call_count == 1
    model, _system, prompt, timeout = llm.call_args.args
    assert model == 'gemini-3.5-flash' and 'céleri-rave' in prompt and 0 < timeout <= 8

    info = IngredientInfo.objects.get(ingredient=ingredient)
    assert (info.source, info.model) == ('llm', 'gemini-3.5-flash')
    assert info.generated_at is not None

    other = make_user()
    with mock.patch.object(service, '_call_llm') as llm2:
        again = api.as_user(other).get(_url(ingredient.id))
    llm2.assert_not_called()
    assert again.data['cached'] is True and again.data['what'] == GOOD['what']


def test_shopping_list_item_route_requires_membership(api, user, make_user, make_shopping_list, ingredient):
    _store(ingredient)
    sl = make_shopping_list(user)
    item = ShoppingListItem.objects.create(shopping_list=sl, ingredient=ingredient)
    resp = api.as_user(user).get(f'/api/shopping-list-items/{item.id}/info/')
    assert resp.status_code == 200
    assert resp.data['ingredient']['id'] == ingredient.id

    stranger = make_user()
    assert api.as_user(stranger).get(f'/api/shopping-list-items/{item.id}/info/').status_code == 404
    # La fiche d'un ingrédient reste lisible par tout utilisateur connecté.
    assert api.as_user(stranger).get(_url(ingredient.id)).status_code == 200


def test_permissions(api, make_user, ingredient):
    assert api.get(_url(ingredient.id)).status_code in (401, 403)
    unvalidated = make_user(validated=False)
    assert api.as_user(unvalidated).get(_url(ingredient.id)).status_code in (401, 403)
    assert api.as_user(make_user()).get(_url(999999)).status_code == 404


# ---------------------------------------------------------------------------
# Repli de modèle
# ---------------------------------------------------------------------------
@override_settings(INGREDIENT_INFO_MODEL='gemini-3.5-flash', CHAT_AI_MODEL='gemini-2.5-flash')
def test_rejected_model_falls_back_to_chat_model(api, user, make_user, ingredient, caplog):
    caplog.set_level(logging.WARNING, logger='recipes.services.ingredient_info')
    with _llm(service.ModelRejected('models/gemini-3.5-flash is not found'), json.dumps(GOOD)) as llm:
        resp = api.as_user(user).get(_url(ingredient.id))
    assert resp.status_code == 200
    assert [c.args[0] for c in llm.call_args_list] == ['gemini-3.5-flash', 'gemini-2.5-flash']
    assert IngredientInfo.objects.get(ingredient=ingredient).model == 'gemini-2.5-flash'
    assert any('refusé' in r.getMessage() and 'gemini-2.5-flash' in r.getMessage() for r in caplog.records)

    # Le refus est retenu : l'ingrédient suivant part directement sur le modèle du chat.
    other = Ingredient.objects.create(name='panais')
    with _llm(
        json.dumps({**GOOD, 'what': 'Le panais est une racine blanche au goût doux et légèrement sucré.'})
    ) as llm:
        assert api.as_user(make_user()).get(_url(other.id)).status_code == 200
    assert [c.args[0] for c in llm.call_args_list] == ['gemini-2.5-flash']


def test_provider_errors_are_classified():
    request = httpx.Request('POST', 'https://example.invalid/v1/chat/completions')
    not_found = openai.NotFoundError('model not found', response=httpx.Response(404, request=request), body=None)
    bad_model = openai.BadRequestError(
        'Invalid model name: gemini-3.5-flash', response=httpx.Response(400, request=request), body=None
    )
    bad_other = openai.BadRequestError(
        'response_format invalid', response=httpx.Response(400, request=request), body=None
    )
    assert service.is_model_rejection(not_found)
    assert service.is_model_rejection(bad_model)
    assert not service.is_model_rejection(bad_other)
    assert not service.is_model_rejection(TimeoutError())


# ---------------------------------------------------------------------------
# Échecs → 503, rien n'est stocké
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    'raw',
    [
        'pas du json',
        json.dumps({**GOOD, 'what': ''}),
        json.dumps({**GOOD, 'aisle': None}),
        json.dumps({**GOOD, 'substitutes': 'panais'}),
        json.dumps({**GOOD, 'what': 'x' * 600}),
        json.dumps({**GOOD, 'what': 'Le céleri-rave est très bon pour la santé et peu calorique.'}),
        json.dumps({**GOOD, 'uses': 'Idéal pour un régime minceur.'}),
        json.dumps({**GOOD, 'keep': 'Riche en vitamines, garde-le au frais.'}),
        json.dumps({**GOOD, 'uses': 'Idéal pour maigrir, en salade.'}),
        json.dumps({**GOOD, 'what': 'Le céleri-rave est une racine. Elle est bonne pour la santé et la ligne.'}),
        json.dumps({**GOOD, 'keep': 'Environ 40 kcal pour 100 g, garde-le au frais.'}),
        json.dumps({**GOOD, 'uses': 'Parfait pour une cure détox.'}),
        json.dumps({**GOOD, 'what': 'Une racine ferme et parfumée, qui aide contre le cholestérol.'}),
        json.dumps({**GOOD, 'substitutes': ['Panais', 'Navet détox']}),
        json.dumps({**GOOD, 'uses': 'Parfait après une sortie Strava.'}),
    ],
)
def test_invalid_llm_output_returns_503_and_stores_nothing(api, user, ingredient, raw):
    with _llm(raw):
        resp = api.as_user(user).get(_url(ingredient.id))
    assert resp.status_code == 503
    assert resp.data['code'] == 'info_unavailable'
    assert resp['Retry-After']
    assert not IngredientInfo.objects.filter(ingredient=ingredient).exists()


TOFU = {
    'what': 'Le tofu est un caillé de lait de soja pressé en bloc. Il a un goût neutre et prend bien les saveurs.',
    'uses': 'Source de protéines végétales, il se poêle, se marine ou se mixe dans une sauce.',
    'aisle': 'Produits frais végétariens',
    'choose': 'Riche en protéines, le tofu ferme se tient mieux à la cuisson ; le soyeux sert aux desserts.',
    'keep': "Une fois ouvert, garde-le dans l'eau au frais 3 à 4 jours.",
    'substitutes': ['Tempeh', 'Seitan'],
}


def test_neutral_protein_mentions_are_accepted_and_stored(api, user):
    tofu = Ingredient.objects.create(name='tofu')
    with _llm(json.dumps(TOFU)):
        resp = api.as_user(user).get(_url(tofu.id))
    assert resp.status_code == 200, resp.data
    info = IngredientInfo.objects.get(ingredient=tofu)
    assert 'protéines végétales' in info.uses and 'Riche en protéines' in info.choose


@pytest.mark.parametrize(
    'text',
    ['Coupe-le en régime de bananes', 'Choisis-le bien sain', 'Lave-le soigneusement', 'Une valeur sûre'],
)
def test_culinary_wording_is_not_mistaken_for_health_claims(text):
    assert not service.FORBIDDEN_RE.search(text)


def test_llm_error_returns_503_then_cooldown_avoids_new_calls(api, user, ingredient):
    with _llm(TimeoutError('lent')):
        assert api.as_user(user).get(_url(ingredient.id)).status_code == 503
    with mock.patch.object(service, '_call_llm') as llm:
        assert api.as_user(user).get(_url(ingredient.id)).status_code == 503
    llm.assert_not_called()
    cache.clear()
    with _llm(json.dumps(GOOD)):
        assert api.as_user(user).get(_url(ingredient.id)).status_code == 200


@override_settings(INGREDIENT_INFO_MODEL='gemini-3.5-flash', CHAT_AI_MODEL='gemini-2.5-flash')
def test_both_models_rejected_returns_503(api, user, ingredient):
    with _llm(service.ModelRejected('a'), service.ModelRejected('b')):
        assert api.as_user(user).get(_url(ingredient.id)).status_code == 503
    assert not IngredientInfo.objects.exists()


def test_without_api_key_returns_503(api, user, ingredient):
    # settings_test vide AI_API_KEY : le vrai client refuse avant tout appel réseau.
    resp = api.as_user(user).get(_url(ingredient.id))
    assert resp.status_code == 503
    assert not IngredientInfo.objects.exists()


@override_settings(INGREDIENT_INFO_LLM_ENABLED=False)
def test_generation_disabled_returns_503_but_serves_cache(api, user, ingredient):
    with mock.patch.object(service, '_call_llm') as llm:
        assert api.as_user(user).get(_url(ingredient.id)).status_code == 503
        _store(ingredient)
        assert api.as_user(user).get(_url(ingredient.id)).status_code == 200
    llm.assert_not_called()


# ---------------------------------------------------------------------------
# Concurrence et limites
# ---------------------------------------------------------------------------
def test_waits_for_concurrent_generation_instead_of_calling_llm(ingredient):
    def _other_request_finishes(ingredient_id, deadline):
        return _store(ingredient)

    with (
        mock.patch.object(service, '_try_lock', return_value=False),
        mock.patch.object(service, '_wait_for_info', side_effect=_other_request_finishes),
        mock.patch.object(service, '_call_llm') as llm,
    ):
        info = service.generate(ingredient)
    llm.assert_not_called()
    assert info.ingredient_id == ingredient.id


@override_settings(INGREDIENT_INFO_LLM_TIMEOUT_SECONDS=0.3)
def test_concurrent_generation_that_never_finishes_is_503(ingredient):
    with mock.patch.object(service, '_try_lock', return_value=False), mock.patch.object(service, '_call_llm') as llm:
        with pytest.raises(service.InfoUnavailable):
            service.generate(ingredient)
    llm.assert_not_called()


def test_concurrent_insert_returns_existing_row(ingredient):
    def _racing_llm(*args):
        _store(ingredient, what='Écrite par une autre requête, un peu plus tôt que nous.')
        return json.dumps(GOOD)

    with mock.patch.object(service, '_call_llm', side_effect=_racing_llm):
        info, cached = service.get_or_generate(ingredient)
    assert info.what.startswith('Écrite par une autre requête')
    assert IngredientInfo.objects.filter(ingredient=ingredient).count() == 1
    assert service._try_lock(ingredient.id)  # verrou consultatif bien relâché
    service._unlock(ingredient.id)


@override_settings(INGREDIENT_INFO_GENERATIONS_PER_USER_HOUR=2)
def test_generation_is_rate_limited_per_user(api, user, make_user):
    ingredients = [Ingredient.objects.create(name=f'produit {i}') for i in range(3)]
    client = api.as_user(user)
    with _llm(*[json.dumps(GOOD)] * 3):
        codes = [client.get(_url(i.id)).status_code for i in ingredients]
    assert codes == [200, 200, 429]
    # Une fiche déjà en base reste servie ; un autre utilisateur a son propre quota.
    assert client.get(_url(ingredients[0].id)).status_code == 200
    with _llm(json.dumps(GOOD)):
        assert api.as_user(make_user()).get(_url(ingredients[2].id)).status_code == 200


# ---------------------------------------------------------------------------
# Pré-génération
# ---------------------------------------------------------------------------
def test_pregenerate_command_most_used_first(user, make_shopping_list, celery_delay):
    sl = make_shopping_list(user)
    popular, rare, unused = (Ingredient.objects.create(name=n) for n in ('oignon', 'kumquat', 'jamais'))
    for _ in range(2):
        ShoppingListItem.objects.create(shopping_list=make_shopping_list(user), ingredient=popular)
    ShoppingListItem.objects.create(shopping_list=sl, ingredient=rare)
    already = Ingredient.objects.create(name='déjà')
    ShoppingListItem.objects.create(shopping_list=sl, ingredient=already)
    _store(already)

    assert service.most_used_missing_ids(10) == [popular.id, rare.id]

    call_command('pregenerate_ingredient_info', '--limit', '10', '--batch-size', '1')
    assert [c[0] for c in celery_delay.calls] == ['recipes.tasks.generate_ingredient_info_batch_task'] * 2
    assert celery_delay.calls[0][1] == ([popular.id],)

    with _llm(json.dumps(GOOD), 'pas du json'):
        call_command('pregenerate_ingredient_info', '--limit', '10', '--sync')
    assert IngredientInfo.objects.filter(ingredient=popular).exists()
    assert not IngredientInfo.objects.filter(ingredient=rare).exists()
    assert not IngredientInfo.objects.filter(ingredient=unused).exists()
