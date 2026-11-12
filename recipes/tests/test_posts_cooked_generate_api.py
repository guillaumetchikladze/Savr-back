"""« J'ai cuisiné » avec seulement un nom de plat (ckd2 lot 2 ter) : la recette créée est complétée par l'IA
à partir du nom (LLM mocké), signalée « proposée par Tchikook » (`source_type = generated`), publique mais
absente de la recherche des autres tant qu'elle est vide, comptée dans le plafond mensuel sauf premium."""

from decimal import Decimal
from unittest import mock

import pytest

from recipes.models import Recipe, RecipeImportRequest, Step
from recipes.services.pydantic_models import RecipeFormalized, RecipeIngredientFormalized, StepFormalized
from recipes.tasks import process_cooked_recipe_generate

pytestmark = pytest.mark.django_db

COOKED_URL = '/api/posts/cooked/'
STATUS_URL = '/api/recipes/formalize/status/{}/'
EVENING = '2026-11-08T20:15:00+01:00'
GENERATE_TASK = 'recipes.tasks.process_cooked_recipe_generate'


def _generated(title='Gratin dauphinois de mamie'):
    return RecipeFormalized(
        title=title,
        description='Fondant',
        steps_summary='Trancher, napper, cuire.',
        prep_time=20,
        cook_time=60,
        servings=4,
        recipe_ingredients=[
            RecipeIngredientFormalized(ingredient_name='pommes de terre', quantity=Decimal('1000'), unit='g'),
            RecipeIngredientFormalized(ingredient_name='crème', quantity=Decimal('400'), unit='ml'),
        ],
        steps=[
            StepFormalized(order=1, instruction='Trancher les pommes de terre.'),
            StepFormalized(order=2, instruction='Napper de crème et cuire 1 h.'),
        ],
    )


def _llm(result=None, error=None):
    side = {'side_effect': error} if error else {'return_value': result or _generated()}
    return mock.patch('recipes.tasks.generate_recipe_from_idea', new=mock.AsyncMock(**side))


@pytest.fixture
def cooked(api, celery_delay, django_capture_on_commit_callbacks):
    def _post(user, **data):
        data.setdefault('cooked_at', EVENING)
        with django_capture_on_commit_callbacks(execute=True):
            return api.as_user(user).post(COOKED_URL, data, format='json')

    return _post


def _generate_calls(celery_delay):
    return [call for call in celery_delay.calls if call[0] == GENERATE_TASK]


def _search_ids(client, q):
    resp = client.get('/api/recipes/', {'search': q})
    assert resp.status_code == 200, resp.data
    rows = resp.data['results'] if isinstance(resp.data, dict) else resp.data
    return {row['id'] for row in rows}


# ---------------------------------------------------------------------------
# Nom seul → complétion en tâche, titre gardé, contenu signalé
# ---------------------------------------------------------------------------
def test_dish_name_only_queues_generation_and_keeps_the_name(cooked, user, settings, celery_delay):
    settings.FEATURES_REQUIRING_SUBSCRIPTION = ['ai']  # aucun droit `ai` demandé
    resp = cooked(user, label='Gratin de mamie')

    assert resp.status_code == 201, resp.data
    recipe = Recipe.objects.get(id=resp.data['recipe']['id'])
    assert recipe.is_public and recipe.title == 'Gratin de mamie' and not recipe.steps.exists()
    job = resp.data['recipe_job']
    assert job['kind'] == 'generate' and job['status'] == 'pending'
    req = RecipeImportRequest.objects.get(id=job['request_id'])
    assert req.recipe_id == recipe.id and req.payload['idea_text'] == 'Gratin de mamie'
    assert req.payload['keep_title'] is True and req.payload['quota'] == 'ai_recipe'
    assert _generate_calls(celery_delay) == [(GENERATE_TASK, (job['request_id'],), {})]


def test_generation_fills_the_recipe_marks_it_generated_and_keeps_the_title(cooked, api, user):
    resp = cooked(user, label='Gratin de mamie')
    request_id = resp.data['recipe_job']['request_id']

    with _llm() as llm:
        process_cooked_recipe_generate(request_id)

    llm.assert_awaited_once()
    assert llm.await_args.args[0] == 'Gratin de mamie'
    recipe = Recipe.objects.get(id=resp.data['recipe']['id'])
    assert recipe.title == 'Gratin de mamie' and recipe.is_public
    assert recipe.source_type == 'generated' and recipe.get_source_type_display() == 'Proposée par Tchikook'
    assert recipe.recipe_ingredients.count() == 2 and recipe.steps.count() == 2
    status = api.as_user(user).get(STATUS_URL.format(request_id))
    assert status.status_code == 200 and status.data['status'] == 'success'
    detail = api.as_user(user).get(f'/api/recipes/{recipe.id}/')
    assert detail.data['source_type'] == 'generated' and detail.data['title'] == 'Gratin de mamie'


@pytest.mark.parametrize('error', [ValueError('AI_API_KEY manquante'), RuntimeError('model down')])
def test_generation_failure_leaves_the_recipe_empty_and_openable(cooked, api, user, make_user, error):
    resp = cooked(user, label='Gratin de mamie')
    request_id = resp.data['recipe_job']['request_id']

    with _llm(error=error):
        process_cooked_recipe_generate(request_id)

    recipe = Recipe.objects.get(id=resp.data['recipe']['id'])
    assert not recipe.steps.exists() and recipe.source_type == 'user_created'
    assert RecipeImportRequest.objects.get(id=request_id).status == 'error'
    friend = make_user()
    assert api.as_user(friend).get(f'/api/recipes/{recipe.id}/').status_code == 200  # s'ouvre depuis le post
    assert recipe.id not in _search_ids(api.as_user(friend), 'Gratin')


def test_user_filled_recipe_is_never_overwritten(cooked, user):
    resp = cooked(user, label='Gratin de mamie')
    recipe = Recipe.objects.get(id=resp.data['recipe']['id'])
    Step.objects.create(recipe=recipe, order=1, instruction='Ma façon à moi.')

    with _llm():
        process_cooked_recipe_generate(resp.data['recipe_job']['request_id'])

    assert list(recipe.steps.values_list('instruction', flat=True)) == ['Ma façon à moi.']
    recipe.refresh_from_db()
    assert recipe.source_type == 'user_created'


def test_late_job_after_undo_does_nothing(cooked, api, user):
    resp = cooked(user, label='Gratin de mamie')
    request_id = resp.data['recipe_job']['request_id']
    assert api.as_user(user).delete(f'/api/posts/{resp.data["id"]}/?undo_cooked=true').status_code == 204

    with _llm() as llm:
        process_cooked_recipe_generate(request_id)

    llm.assert_not_awaited()
    assert not Recipe.objects.filter(title='Gratin de mamie').exists()
    assert not RecipeImportRequest.objects.exists()


def test_undo_while_generating_never_brings_the_recipe_back(cooked, api, user):
    resp = cooked(user, label='Gratin de mamie')
    request_id = resp.data['recipe_job']['request_id']

    def undo_meanwhile(*_args, **_kwargs):
        # Annuler pendant l'appel au modèle (la coroutine n'est attendue qu'après).
        api.as_user(user).delete(f'/api/posts/{resp.data["id"]}/?undo_cooked=true')

        async def answer():
            return _generated()

        return answer()

    with mock.patch('recipes.tasks.generate_recipe_from_idea', new=undo_meanwhile):
        process_cooked_recipe_generate(request_id)

    assert not Recipe.objects.filter(created_by=user).exists()
    assert not RecipeImportRequest.objects.exists()


# ---------------------------------------------------------------------------
# Recette réutilisée (même nom)
# ---------------------------------------------------------------------------
def test_reused_recipe_with_content_is_not_regenerated(cooked, user, make_recipe, celery_delay):
    mine = make_recipe(user, 'Gratin de mamie', ingredients=['pommes de terre'])

    resp = cooked(user, label='gratin de mamie')

    assert resp.data['recipe']['id'] == mine.id and resp.data['recipe_job'] is None
    assert _generate_calls(celery_delay) == [] and not RecipeImportRequest.objects.exists()


def test_reused_empty_recipe_is_completed(cooked, user, make_recipe, celery_delay):
    mine = make_recipe(user, 'Gratin de mamie')

    resp = cooked(user, label='Gratin de mamie')

    assert resp.data['recipe']['id'] == mine.id
    job = resp.data['recipe_job']
    assert job['kind'] == 'generate' and job['status'] == 'pending'
    assert RecipeImportRequest.objects.get(id=job['request_id']).payload['target_recipe_id'] == mine.id


def test_reused_recipe_with_a_running_completion_is_not_queued_twice(cooked, user, celery_delay):
    first = cooked(user, label='Gratin de mamie')
    again = cooked(user, label='Gratin de mamie', cooked_at='2026-11-10T20:00:00+01:00')

    assert again.data['recipe']['id'] == first.data['recipe']['id']
    assert again.data['recipe_job']['request_id'] == first.data['recipe_job']['request_id']
    assert len(_generate_calls(celery_delay)) == 1


def test_written_recipe_or_link_is_not_generated(cooked, user, celery_delay):
    written = cooked(user, label='Soupe', recipe_text='Des légumes, de l’eau, mixer.')
    assert written.data['recipe_job']['kind'] == 'format'
    assert _generate_calls(celery_delay) == []


# ---------------------------------------------------------------------------
# Recherche : une recette publique vide n'apparaît pas chez les autres
# ---------------------------------------------------------------------------
def test_empty_recipe_is_hidden_from_others_search_until_completed(cooked, api, user, make_user):
    resp = cooked(user, label='Gratin de mamie')
    recipe_id = resp.data['recipe']['id']
    friend = make_user()

    assert recipe_id in _search_ids(api.as_user(user), 'Gratin')  # la sienne, oui
    client = api.as_user(friend)
    assert recipe_id not in _search_ids(client, 'Gratin')
    assert recipe_id not in {r['id'] for r in client.get('/api/auth/search/', {'q': 'Gratin'}).data['recipes']}
    assert client.get(f'/api/recipes/{recipe_id}/').status_code == 200  # s'ouvre depuis le post

    with _llm():
        process_cooked_recipe_generate(resp.data['recipe_job']['request_id'])

    assert recipe_id in _search_ids(client, 'Gratin')
    assert recipe_id in {r['id'] for r in client.get('/api/auth/search/', {'q': 'Gratin'}).data['recipes']}


def test_global_search_never_lists_someone_elses_private_recipe(api, user, make_user, make_recipe):
    stranger = make_user()
    private = make_recipe(stranger, 'Gratin secret', ingredients=['crème'], is_public=False)
    public = make_recipe(stranger, 'Gratin public', ingredients=['crème'])

    ids = {r['id'] for r in api.as_user(user).get('/api/auth/search/', {'q': 'Gratin'}).data['recipes']}

    assert public.id in ids and private.id not in ids


# ---------------------------------------------------------------------------
# Plafond mensuel
# ---------------------------------------------------------------------------
def test_over_the_cap_the_recipe_stays_empty_and_skipped(cooked, user, settings, celery_delay):
    settings.AI_RECIPE_MONTHLY_CAP = 1
    assert cooked(user, label='Soupe').data['recipe_job']['status'] == 'pending'

    resp = cooked(user, label='Gratin de mamie')

    assert resp.status_code == 201
    assert resp.data['recipe_job'] == {
        'request_id': None,
        'kind': 'generate',
        'status': 'skipped',
        'code': 'ai_quota_exceeded',
    }
    assert not Recipe.objects.get(id=resp.data['recipe']['id']).steps.exists()
    assert len(_generate_calls(celery_delay)) == 1


def test_premium_is_not_capped(cooked, make_user, settings, celery_delay):
    settings.AI_RECIPE_MONTHLY_CAP = 1
    premium = make_user(plan='premium')
    cooked(premium, label='Soupe')

    assert cooked(premium, label='Gratin de mamie').data['recipe_job']['status'] == 'pending'
