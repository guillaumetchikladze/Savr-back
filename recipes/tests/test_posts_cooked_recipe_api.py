"""« J'ai cuisiné » crée toujours une recette (ckd2 lot 2) : nom seul, « Ta recette » écrite, lien collé,
mise en forme / import en tâche (LLM et scraper mockés : pas de clé IA en local), plafond mensuel."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.utils import timezone

from recipes.models import Post, Recipe, RecipeImportRequest
from recipes.services.pydantic_models import RecipeFormalized, RecipeIngredientFormalized, StepFormalized
from recipes.tasks import process_cooked_recipe_import, process_cooked_recipe_text

pytestmark = pytest.mark.django_db

COOKED_URL = '/api/posts/cooked/'
FORMALIZE_URL = '/api/recipes/formalize/'
EVENING = '2026-11-08T20:15:00+01:00'
LINK = 'https://www.marmiton.org/recettes/pates-au-canard-123.aspx'
WRITTEN = 'Des pâtes, du canard, de la crème. Cuire les pâtes, poêler le canard, mélanger.'
TEXT_TASK = 'recipes.tasks.process_cooked_recipe_text'
IMPORT_TASK = 'recipes.tasks.process_cooked_recipe_import'


def _formalized(title='Pâtes au canard'):
    return RecipeFormalized(
        title=title,
        description='Du dimanche',
        steps_summary='Cuire, poêler, mélanger.',
        prep_time=10,
        cook_time=20,
        servings=2,
        recipe_ingredients=[
            RecipeIngredientFormalized(ingredient_name='pâtes', quantity=Decimal('200'), unit='g'),
            RecipeIngredientFormalized(ingredient_name='magret de canard', quantity=Decimal('300'), unit='g'),
        ],
        steps=[
            StepFormalized(order=1, instruction='Cuire les pâtes.'),
            StepFormalized(order=2, instruction='Poêler le canard.'),
        ],
    )


def _llm(result=None, error=None):
    side = {'side_effect': error} if error else {'return_value': result or _formalized()}
    return mock.patch('recipes.tasks.formalize_recipe', new=mock.AsyncMock(**side))


@pytest.fixture
def cooked(api, celery_delay, django_capture_on_commit_callbacks):
    """POST /posts/cooked/ en exécutant les envois de tâches faits au commit (capturés par celery_delay)."""

    def _post(user, **data):
        data.setdefault('cooked_at', EVENING)
        with django_capture_on_commit_callbacks(execute=True):
            return api.as_user(user).post(COOKED_URL, data, format='json')

    return _post


def _recipe_tasks(celery_delay):
    """Tâches recettes envoyées (les badges / stats en envoient aussi)."""
    return [call for call in celery_delay.calls if call[0].startswith('recipes.tasks.process_cooked')]


def _recipe_of(resp) -> Recipe:
    return Recipe.objects.get(id=resp.data['recipe']['id'])


# ---------------------------------------------------------------------------
# Nom seul : recette au nom du plat (plus de post « resto » sans recette)
# ---------------------------------------------------------------------------
def test_label_only_creates_a_recipe_named_like_the_dish(cooked, user, celery_delay):
    resp = cooked(user, label='Pâtes au pesto')

    assert resp.status_code == 201, resp.data
    recipe = _recipe_of(resp)
    assert recipe.title == 'Pâtes au pesto' and recipe.created_by == user and recipe.is_public
    assert recipe.source_type == 'user_created' and not recipe.steps.exists()
    assert resp.data['display_title'] == 'Pâtes au pesto'
    assert resp.data['recipe_job']['kind'] == 'generate'  # complétée par l'IA (lot 2 ter)
    post = Post.objects.get(id=resp.data['id'])
    assert post.recipe_batch.recipe_id == recipe.id and post.recipe_batch.is_cooked


def test_same_dish_cooked_again_reuses_own_recipe_not_someone_elses(cooked, user, make_user, make_recipe):
    stranger = make_user()
    make_recipe(stranger, 'Pâtes au pesto')

    first = cooked(user, label='Pâtes au pesto')
    again = cooked(user, label='pâtes au pesto', cooked_at='2026-11-10T20:00:00+01:00')
    same_day = cooked(user, label='Pâtes au pesto')

    assert first.data['recipe']['id'] == again.data['recipe']['id']
    assert Recipe.objects.filter(created_by=user).count() == 1
    assert same_day.status_code == 200 and same_day.data['id'] == first.data['id']


def test_existing_meal_plan_without_recipe_gets_one_from_its_dish(cooked, user, make_meal_plan):
    mp = make_meal_plan(user, custom_label='Raclette')

    resp = cooked(user, meal_plan_id=mp.id)

    assert resp.status_code == 201, resp.data
    assert _recipe_of(resp).title == 'Raclette'
    assert mp.meal_plan_recipe_batches.count() == 1


def test_undo_removes_the_recipe_created_by_the_log_only(cooked, api, user, make_recipe):
    kept = make_recipe(user, 'Quiche', ingredients=['œufs'])
    created = cooked(user, label='Soupe de potiron', recipe_text=WRITTEN)
    reused = cooked(user, label='Quiche')
    client = api.as_user(user)

    for resp in (created, reused):
        assert client.delete(f'/api/posts/{resp.data["id"]}/?undo_cooked=true').status_code == 204

    assert list(Recipe.objects.values_list('id', flat=True)) == [kept.id]
    assert not RecipeImportRequest.objects.exists()  # mise en forme abandonnée avec sa recette


# ---------------------------------------------------------------------------
# « Ta recette » écrite → mise en forme IA en tâche, texte brut gardé en secours
# ---------------------------------------------------------------------------
def test_written_recipe_is_kept_raw_and_queued_for_formatting(cooked, user, celery_delay):
    resp = cooked(user, label='Pâtes au canard', recipe_text=WRITTEN)

    assert resp.status_code == 201, resp.data
    recipe = _recipe_of(resp)
    assert recipe.title == 'Pâtes au canard'
    assert list(recipe.steps.values_list('instruction', flat=True)) == [WRITTEN]
    job = resp.data['recipe_job']
    assert job['kind'] == 'format' and job['status'] == 'pending'
    req = RecipeImportRequest.objects.get(id=job['request_id'])
    assert req.recipe_id == recipe.id and req.payload['raw_text'] == WRITTEN and req.payload['keep_title'] is True
    assert _recipe_tasks(celery_delay) == [(TEXT_TASK, (job['request_id'],), {})]


def test_formatting_fills_the_recipe_and_keeps_the_users_name(cooked, user, api):
    resp = cooked(user, label='Les pâtes de mamie', recipe_text=WRITTEN)
    request_id = resp.data['recipe_job']['request_id']

    with _llm(_formalized('Pâtes au canard')) as llm:
        process_cooked_recipe_text(request_id)

    assert llm.await_args.kwargs['raw_text'] == WRITTEN
    recipe = _recipe_of(resp)
    assert recipe.title == 'Les pâtes de mamie'
    assert recipe.recipe_ingredients.count() == 2
    assert list(recipe.steps.order_by('order').values_list('instruction', flat=True)) == [
        'Cuire les pâtes.',
        'Poêler le canard.',
    ]
    status = api.as_user(user).get(f'/api/recipes/formalize/status/{request_id}/').json()
    assert status['status'] == 'success' and status['recipe_id'] == recipe.id


def test_written_recipe_without_name_takes_the_ai_title(cooked, user):
    resp = cooked(user, recipe_text=WRITTEN)
    assert resp.status_code == 201, resp.data
    assert _recipe_of(resp).title == 'Ta recette' and resp.data['recipe_job']['kind'] == 'format'

    with _llm(_formalized('Pâtes au canard')):
        process_cooked_recipe_text(resp.data['recipe_job']['request_id'])

    assert _recipe_of(resp).title == 'Pâtes au canard'


@pytest.mark.parametrize('error', [ValueError('AI_API_KEY doit être configuré'), RuntimeError('LLM down')])
def test_formatting_failure_keeps_the_written_text(cooked, user, error):
    resp = cooked(user, label='Pâtes au canard', recipe_text=WRITTEN)
    request_id = resp.data['recipe_job']['request_id']

    with _llm(error=error):
        process_cooked_recipe_text(request_id)

    recipe = _recipe_of(resp)
    assert recipe.title == 'Pâtes au canard'
    assert list(recipe.steps.values_list('instruction', flat=True)) == [WRITTEN]
    req = RecipeImportRequest.objects.get(id=request_id)
    assert req.status == RecipeImportRequest.STATUS_ERROR and 'telle que tu l’as écrite' in req.error_message


def test_task_after_undo_never_brings_the_recipe_back(cooked, api, user):
    resp = cooked(user, label='Pâtes au canard', recipe_text=WRITTEN)
    request_id = resp.data['recipe_job']['request_id']
    api.as_user(user).delete(f'/api/posts/{resp.data["id"]}/?undo_cooked=true')

    with _llm() as llm:
        process_cooked_recipe_text(request_id)

    llm.assert_not_awaited()
    assert not Recipe.objects.exists()


def test_recipe_text_is_ignored_when_a_recipe_is_chosen(cooked, user, make_recipe, celery_delay):
    recipe = make_recipe(user, 'Quiche')

    resp = cooked(user, recipe_id=recipe.id, recipe_text=WRITTEN, import_url=LINK)

    assert resp.data['recipe']['id'] == recipe.id and resp.data['recipe_job'] is None
    assert _recipe_tasks(celery_delay) == [] and Recipe.objects.count() == 1


# ---------------------------------------------------------------------------
# Lien collé → import par le back (sans droit `ai`), nom par défaut renommé
# ---------------------------------------------------------------------------
def test_link_posts_at_once_under_the_default_name_without_ai_feature(cooked, user, settings, celery_delay):
    settings.AI_PAYWALL_ENABLED = True  # utilisateur gratuit : plus de 403 sur le lien collé
    # L'app envoie le nom par défaut comme label : ce n'est pas un nom choisi.
    resp = cooked(user, label='Recette de marmiton.org', import_url=LINK)

    assert resp.status_code == 201, resp.data
    recipe = _recipe_of(resp)
    assert recipe.title == 'Recette de marmiton.org' and recipe.source_type == 'imported'
    assert recipe.import_source_url == LINK
    assert resp.data['label'] == '' and resp.data['display_title'] == 'Recette de marmiton.org'
    job = resp.data['recipe_job']
    assert job['kind'] == 'import' and job['status'] == 'pending'
    assert RecipeImportRequest.objects.get(id=job['request_id']).payload['keep_title'] is False
    assert _recipe_tasks(celery_delay) == [(IMPORT_TASK, (job['request_id'],), {})]


def _scraped():
    return {
        'title': 'Pâtes au canard',
        'description': '',
        'ingredients_text': '200 g de pâtes\n1 magret de canard\n20 cl de crème\n1 échalote',
        'instructions_text': 'Cuire les pâtes. Poêler le canard.',
        'image_path': '',
    }


def test_import_renames_the_default_name_and_the_post_follows(cooked, api, user):
    resp = cooked(user, import_url=LINK)
    request_id = resp.data['recipe_job']['request_id']

    with (
        mock.patch('recipes.tasks.import_recipe_from_url', return_value=(_scraped(), 'recipe_scrapers')) as scraper,
        _llm(_formalized('Pâtes au canard')),
    ):
        process_cooked_recipe_import(request_id)

    scraper.assert_called_once_with(LINK)
    recipe = _recipe_of(resp)
    assert recipe.title == 'Pâtes au canard' and recipe.import_source_url == LINK
    assert recipe.steps.count() == 2 and recipe.source_type == 'imported'
    feed = api.as_user(user).get('/api/posts/?is_published=true').json()['results']
    assert feed[0]['id'] == resp.data['id'] and feed[0]['display_title'] == 'Pâtes au canard'


def test_import_keeps_the_name_the_user_gave(cooked, user):
    resp = cooked(user, label='Gratin de pommes de terre', import_url=LINK)
    assert _recipe_of(resp).title == 'Gratin de pommes de terre'

    with (
        mock.patch('recipes.tasks.import_recipe_from_url', return_value=(_scraped(), 'recipe_scrapers')),
        _llm(_formalized('Pâtes au canard')),
    ):
        process_cooked_recipe_import(resp.data['recipe_job']['request_id'])

    recipe = _recipe_of(resp)
    assert recipe.title == 'Gratin de pommes de terre' and recipe.steps.count() == 2
    assert resp.data['display_title'] == 'Gratin de pommes de terre'


def test_import_failure_keeps_default_name_and_link(cooked, user):
    resp = cooked(user, import_url=LINK)
    request_id = resp.data['recipe_job']['request_id']

    with mock.patch('recipes.tasks.import_recipe_from_url', return_value=(None, 'generic')), _llm() as llm:
        process_cooked_recipe_import(request_id)

    llm.assert_not_awaited()
    recipe = _recipe_of(resp)
    assert recipe.title == 'Recette de marmiton.org' and recipe.import_source_url == LINK
    assert RecipeImportRequest.objects.get(id=request_id).status == RecipeImportRequest.STATUS_ERROR


def test_link_already_imported_is_linked_at_once(cooked, user, make_user, make_recipe, celery_delay):
    imported = make_recipe(make_user(), 'Pâtes au canard', import_source_url=LINK, source_type='imported')

    resp = cooked(user, label='Recette de marmiton.org', import_url=LINK)

    assert resp.data['recipe']['id'] == imported.id and resp.data['recipe_job'] is None
    assert resp.data['display_title'] == 'Pâtes au canard'
    assert _recipe_tasks(celery_delay) == [] and Recipe.objects.count() == 1


@pytest.mark.parametrize('url', ['marmiton', 'ftp://marmiton.org/x', 'javascript:alert(1)'])
def test_invalid_link_is_refused(cooked, user, url):
    resp = cooked(user, label='Pâtes', import_url=url)

    assert resp.status_code == 400 and resp.data['code'] == 'invalid_import_url'
    assert not Post.objects.exists() and not Recipe.objects.exists()


# ---------------------------------------------------------------------------
# Plafond mensuel : formalize ouvert à tous, 429 au-delà ; « J'ai cuisiné » garde le brut
# ---------------------------------------------------------------------------
def test_formalize_is_open_without_ai_feature(api, user, settings, celery_delay):
    settings.AI_PAYWALL_ENABLED = True

    resp = api.as_user(user).post(FORMALIZE_URL, {'title': 'Soupe', 'raw_text': WRITTEN}, format='json')

    assert resp.status_code == 202, resp.data
    assert RecipeImportRequest.objects.get().payload['quota'] == 'ai_recipe'


def test_monthly_cap_is_shared_and_blocks_formalize_with_429(api, cooked, user, make_user, settings, celery_delay):
    settings.AI_RECIPE_MONTHLY_CAP = 2
    client = api.as_user(user)
    old = RecipeImportRequest.objects.create(user=user, payload={'quota': 'ai_recipe'})
    RecipeImportRequest.objects.filter(id=old.id).update(created_at=timezone.now() - timedelta(days=40))
    RecipeImportRequest.objects.create(user=user, payload={'url': LINK})  # import_from_url : hors plafond

    assert client.post(FORMALIZE_URL, {'raw_text': WRITTEN}, format='json').status_code == 202
    assert cooked(user, label='Soupe', recipe_text=WRITTEN).data['recipe_job']['status'] == 'pending'
    blocked = client.post(FORMALIZE_URL, {'raw_text': WRITTEN}, format='json')
    other_user = api.as_user(make_user()).post(FORMALIZE_URL, {'raw_text': WRITTEN}, format='json')

    assert blocked.status_code == 429
    assert blocked.data['code'] == 'ai_quota_exceeded' and blocked.data['limit'] == 2
    assert 'mises en forme' in blocked.data['error'] and blocked.data['resets_on'].endswith('-01')
    assert other_user.status_code == 202


def test_over_the_cap_the_post_keeps_the_raw_recipe(cooked, user, settings, celery_delay):
    settings.AI_RECIPE_MONTHLY_CAP = 1
    cooked(user, label='Soupe', recipe_text=WRITTEN)

    text = cooked(user, label='Pâtes au canard', recipe_text=WRITTEN)
    link = cooked(user, import_url=LINK)

    assert text.status_code == 201 and link.status_code == 201
    assert text.data['recipe_job'] == {
        'request_id': None,
        'kind': 'format',
        'status': 'skipped',
        'code': 'ai_quota_exceeded',
    }
    assert link.data['recipe_job']['status'] == 'skipped'
    assert list(_recipe_of(text).steps.values_list('instruction', flat=True)) == [WRITTEN]
    assert _recipe_of(link).title == 'Recette de marmiton.org' and _recipe_of(link).import_source_url == LINK
    assert len(_recipe_tasks(celery_delay)) == 1


def test_cap_zero_means_unlimited(api, user, settings, celery_delay):
    settings.AI_RECIPE_MONTHLY_CAP = 0
    for _ in range(3):
        RecipeImportRequest.objects.create(user=user, payload={'quota': 'ai_recipe'})

    assert api.as_user(user).post(FORMALIZE_URL, {'raw_text': WRITTEN}, format='json').status_code == 202


def test_broker_down_leaves_the_raw_recipe_and_does_not_count(cooked, user):
    with mock.patch('celery.app.task.Task.apply_async', side_effect=ConnectionError('redis down')):
        resp = cooked(user, label='Soupe', recipe_text=WRITTEN)

    assert resp.status_code == 201
    req = RecipeImportRequest.objects.get(id=resp.data['recipe_job']['request_id'])
    assert req.status == RecipeImportRequest.STATUS_ERROR and req.error_message == 'queue_unavailable'
    from recipes.services.ai_recipe_quota import used_this_month

    assert used_this_month(user) == 0
