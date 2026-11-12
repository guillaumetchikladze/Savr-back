"""ckd2 lot 2 bis : import du lien dès le collage (`POST /recipes/import-link/`), recette privée jusqu'au
post, ✕ / Annuler, et abonnés premium sans plafond de mise en forme IA (scraper et LLM mockés)."""

from decimal import Decimal
from unittest import mock

import pytest

from recipes.models import Post, Recipe, RecipeImportRequest
from recipes.services.pydantic_models import RecipeFormalized, RecipeIngredientFormalized, StepFormalized
from recipes.tasks import process_cooked_recipe_import, process_cooked_recipe_text

pytestmark = pytest.mark.django_db

IMPORT_LINK_URL = '/api/recipes/import-link/'
COOKED_URL = '/api/posts/cooked/'
FORMALIZE_URL = '/api/recipes/formalize/'
EVENING = '2026-11-08T20:15:00+01:00'
LINK = 'https://www.marmiton.org/recettes/pates-au-canard-123.aspx'
OTHER_LINK = 'https://cuisine.fr/risotto'
WRITTEN = 'Des pâtes, du canard, de la crème. Cuire les pâtes, poêler le canard, mélanger.'
IMPORT_TASK = 'recipes.tasks.process_cooked_recipe_import'


def _formalized(title='Pâtes au canard'):
    return RecipeFormalized(
        title=title,
        description='',
        steps_summary='Cuire, poêler.',
        prep_time=10,
        cook_time=20,
        servings=2,
        recipe_ingredients=[
            RecipeIngredientFormalized(ingredient_name='pâtes', quantity=Decimal('200'), unit='g'),
        ],
        steps=[StepFormalized(order=1, instruction='Cuire les pâtes.')],
    )


def _scraped():
    return {
        'title': 'Pâtes au canard',
        'description': '',
        'ingredients_text': '200 g de pâtes\n1 magret de canard\n20 cl de crème\n1 échalote',
        'instructions_text': 'Cuire les pâtes. Poêler le canard.',
        'image_path': '',
    }


def _run_import(request_id, title='Pâtes au canard'):
    with (
        mock.patch('recipes.tasks.import_recipe_from_url', return_value=(_scraped(), 'recipe_scrapers')),
        mock.patch('recipes.tasks.formalize_recipe', new=mock.AsyncMock(return_value=_formalized(title))),
    ):
        process_cooked_recipe_import(request_id)


def _recipe_tasks(celery_delay):
    return [call for call in celery_delay.calls if call[0].startswith('recipes.tasks.process_cooked')]


@pytest.fixture
def paste(api, celery_delay, django_capture_on_commit_callbacks):
    def _paste(user, url=LINK):
        with django_capture_on_commit_callbacks(execute=True):
            return api.as_user(user).post(IMPORT_LINK_URL, {'url': url}, format='json')

    return _paste


@pytest.fixture
def cooked(api, celery_delay, django_capture_on_commit_callbacks):
    def _post(user, **data):
        data.setdefault('cooked_at', EVENING)
        with django_capture_on_commit_callbacks(execute=True):
            return api.as_user(user).post(COOKED_URL, data, format='json')

    return _post


# ---------------------------------------------------------------------------
# Collage : recette privée « Recette de <domaine> », import lancé sans droit `ai`
# ---------------------------------------------------------------------------
def test_paste_creates_a_private_default_recipe_and_starts_the_import(paste, user, settings, celery_delay):
    settings.AI_PAYWALL_ENABLED = True  # gratuit : pas de 403

    resp = paste(user)

    assert resp.status_code == 201, resp.data
    data = resp.data
    recipe = Recipe.objects.get(id=data['recipe_id'])
    assert recipe.title == 'Recette de marmiton.org' and not recipe.is_public and recipe.created_by == user
    assert recipe.import_source_url == LINK and recipe.source_type == 'imported'
    assert data['status'] == 'pending' and data['host'] == 'marmiton.org' and not data['already_imported']
    assert data['recipe'] == {'id': recipe.id, 'title': 'Recette de marmiton.org', 'image_url': recipe.image_url}
    req = RecipeImportRequest.objects.get(id=data['request_id'])
    assert req.payload['job_type'] == 'cooked_link' and req.payload['keep_title'] is False
    assert req.payload['quota'] == 'ai_recipe'
    assert _recipe_tasks(celery_delay) == [(IMPORT_TASK, (data['request_id'],), {})]


def test_unposted_recipe_is_invisible_to_others(paste, api, user, make_user):
    recipe_id = paste(user).data['recipe_id']
    stranger = api.as_user(make_user())

    assert stranger.get(f'/api/recipes/{recipe_id}/').status_code == 404
    # Un autre qui colle le même lien n'hérite pas de la recette privée : il a la sienne.
    other = paste(make_user())
    assert other.status_code == 201 and other.data['recipe_id'] != recipe_id


def test_import_end_is_seen_by_polling_with_the_imported_title(paste, api, user):
    data = paste(user).data

    _run_import(data['request_id'])

    status = api.as_user(user).get(f'/api/recipes/formalize/status/{data["request_id"]}/').json()
    assert status['status'] == 'success' and status['recipe_id'] == data['recipe_id']
    assert status['recipe']['title'] == 'Pâtes au canard'
    assert not Recipe.objects.get(id=data['recipe_id']).is_public  # toujours pas postée


def test_paste_again_reuses_the_recipe_and_its_running_import(paste, user, celery_delay):
    first = paste(user).data
    again = paste(user)

    assert again.status_code == 200
    assert again.data['recipe_id'] == first['recipe_id'] and again.data['request_id'] == first['request_id']
    assert again.data['status'] == 'pending'
    assert len(_recipe_tasks(celery_delay)) == 1

    _run_import(first['request_id'])
    done = paste(user).data
    assert done['status'] == 'success' and done['request_id'] is None and done['already_imported']
    assert done['recipe']['title'] == 'Pâtes au canard'


def test_link_already_imported_by_someone_is_reused_without_a_new_import(
    paste, user, make_user, make_recipe, celery_delay
):
    imported = make_recipe(make_user(), 'Pâtes au canard', import_source_url=LINK, source_type='imported')

    resp = paste(user)

    assert resp.status_code == 200
    assert resp.data['recipe_id'] == imported.id and resp.data['status'] == 'success'
    assert resp.data['already_imported'] and resp.data['request_id'] is None
    assert _recipe_tasks(celery_delay) == [] and Recipe.objects.count() == 1


def test_failed_import_of_own_recipe_is_retried_on_paste(paste, user, celery_delay):
    first = paste(user).data
    with mock.patch('recipes.tasks.import_recipe_from_url', return_value=(None, 'generic')):
        process_cooked_recipe_import(first['request_id'])

    again = paste(user).data

    assert again['recipe_id'] == first['recipe_id'] and again['status'] == 'pending'
    assert again['request_id'] != first['request_id']


@pytest.mark.parametrize('url', ['', 'marmiton', 'ftp://marmiton.org/x', 'javascript:alert(1)'])
def test_invalid_link_is_refused(paste, user, url):
    resp = paste(user, url)

    assert resp.status_code == 400 and resp.data['code'] == 'invalid_import_url'
    assert not Recipe.objects.exists()


def test_over_the_cap_the_recipe_is_kept_with_its_default_name_and_link(paste, user, settings, celery_delay):
    settings.AI_RECIPE_MONTHLY_CAP = 1
    paste(user, OTHER_LINK)

    resp = paste(user)

    assert resp.status_code == 201
    assert resp.data['status'] == 'skipped' and resp.data['code'] == 'ai_quota_exceeded'
    assert resp.data['request_id'] is None
    recipe = Recipe.objects.get(id=resp.data['recipe_id'])
    assert recipe.title == 'Recette de marmiton.org' and recipe.import_source_url == LINK
    assert len(_recipe_tasks(celery_delay)) == 1
    from recipes.services.ai_recipe_quota import used_this_month

    assert used_this_month(user) == 1  # la recette gardée telle quelle n'est pas décomptée


# ---------------------------------------------------------------------------
# Valider avec recipe_id : publiée, titre suivi par recipe_job, règle du nom choisi
# ---------------------------------------------------------------------------
def test_post_while_importing_publishes_the_recipe_and_follows_the_import(paste, cooked, api, user):
    data = paste(user).data

    resp = cooked(user, recipe_id=data['recipe_id'])

    assert resp.status_code == 201, resp.data
    assert resp.data['recipe_job'] == {'request_id': data['request_id'], 'kind': 'import', 'status': 'pending'}
    assert resp.data['display_title'] == 'Recette de marmiton.org'
    assert Recipe.objects.get(id=data['recipe_id']).is_public

    _run_import(data['request_id'])

    feed = api.as_user(user).get('/api/posts/?is_published=true').json()['results']
    assert feed[0]['id'] == resp.data['id'] and feed[0]['display_title'] == 'Pâtes au canard'


def test_default_name_sent_as_label_is_not_a_chosen_name(paste, cooked, user):
    data = paste(user).data

    resp = cooked(user, recipe_id=data['recipe_id'], label='Recette de marmiton.org')
    _run_import(data['request_id'])

    post = Post.objects.get(id=resp.data['id'])
    assert post.label == ''
    assert Recipe.objects.get(id=data['recipe_id']).title == 'Pâtes au canard'


def test_name_the_user_gave_is_kept_after_the_import(paste, cooked, api, user):
    data = paste(user).data

    resp = cooked(user, recipe_id=data['recipe_id'], label='Les pâtes de mamie')
    _run_import(data['request_id'])

    feed = api.as_user(user).get('/api/posts/?is_published=true').json()['results']
    assert feed[0]['id'] == resp.data['id'] and feed[0]['display_title'] == 'Les pâtes de mamie'


def test_post_after_the_import_has_no_job_to_follow(paste, cooked, user):
    data = paste(user).data
    _run_import(data['request_id'])

    resp = cooked(user, recipe_id=data['recipe_id'])

    assert resp.data['recipe_job'] is None and resp.data['display_title'] == 'Pâtes au canard'


def test_fallback_import_url_finds_the_pasted_recipe(paste, cooked, user, celery_delay):
    data = paste(user).data

    resp = cooked(user, import_url=LINK, label='Recette de marmiton.org')

    assert resp.data['recipe']['id'] == data['recipe_id']
    assert resp.data['recipe_job']['request_id'] == data['request_id']
    assert Recipe.objects.count() == 1 and Recipe.objects.get().is_public
    assert len(_recipe_tasks(celery_delay)) == 1


def test_undo_removes_the_pasted_recipe_like_a_recipe_created_by_the_log(paste, cooked, api, user, make_recipe):
    data = paste(user).data
    resp = cooked(user, recipe_id=data['recipe_id'])

    assert api.as_user(user).delete(f'/api/posts/{resp.data["id"]}/?undo_cooked=true').status_code == 204

    assert not Recipe.objects.filter(id=data['recipe_id']).exists()
    assert not RecipeImportRequest.objects.exists()  # son import s'arrête avec elle

    # Une recette déjà postée, recollée puis repostée, n'est jamais supprimée par Annuler.
    posted = paste(user, OTHER_LINK).data['recipe_id']
    cooked(user, recipe_id=posted)
    again = cooked(user, recipe_id=posted, cooked_at='2026-11-09T20:00:00+01:00')
    api.as_user(user).delete(f'/api/posts/{again.data["id"]}/?undo_cooked=true')
    assert Recipe.objects.filter(id=posted, is_public=True).exists()


# ---------------------------------------------------------------------------
# ✕ / autre lien : la recette jamais postée part, jamais une recette postée ou à un autre
# ---------------------------------------------------------------------------
def test_discard_deletes_an_unposted_pasted_recipe_and_stops_its_import(paste, api, user):
    data = paste(user).data

    resp = api.as_user(user).delete(f'{IMPORT_LINK_URL}{data["recipe_id"]}/')

    assert resp.status_code == 204
    assert not Recipe.objects.exists() and not RecipeImportRequest.objects.exists()
    _run_import(data['request_id'])  # la tâche arrive après : rien ne revient
    assert not Recipe.objects.exists()


def test_discard_never_touches_posted_foreign_or_ordinary_recipes(paste, cooked, api, user, make_user, make_recipe):
    posted = paste(user).data['recipe_id']
    cooked(user, recipe_id=posted)
    private_own = make_recipe(user, 'Ma quiche', is_public=False)
    foreign = paste(make_user(), OTHER_LINK).data['recipe_id']
    client = api.as_user(user)

    for rid in (posted, private_own.id, foreign, 999999):
        assert client.delete(f'{IMPORT_LINK_URL}{rid}/').status_code == 204

    assert Recipe.objects.filter(id__in=[posted, private_own.id, foreign]).count() == 3


def test_discard_works_over_the_cap(paste, api, user, settings):
    settings.AI_RECIPE_MONTHLY_CAP = 1
    paste(user, OTHER_LINK)
    skipped = paste(user).data
    assert skipped['status'] == 'skipped'

    api.as_user(user).delete(f'{IMPORT_LINK_URL}{skipped["recipe_id"]}/')

    assert not Recipe.objects.filter(id=skipped['recipe_id']).exists()


# ---------------------------------------------------------------------------
# Premium : aucun plafond (formalize, « J'ai cuisiné », collage)
# ---------------------------------------------------------------------------
def test_premium_has_no_cap_anywhere(api, paste, cooked, make_user, settings, celery_delay):
    settings.AI_RECIPE_MONTHLY_CAP = 1
    premium = make_user(plan='premium')
    free = make_user()
    for u in (premium, free):
        RecipeImportRequest.objects.create(user=u, payload={'quota': 'ai_recipe'})  # plafond atteint

    assert api.as_user(premium).post(FORMALIZE_URL, {'raw_text': WRITTEN}, format='json').status_code == 202
    assert cooked(premium, label='Soupe', recipe_text=WRITTEN).data['recipe_job']['status'] == 'pending'
    assert cooked(premium, import_url=OTHER_LINK).data['recipe_job']['status'] == 'pending'
    assert paste(premium).data['status'] == 'pending'

    assert api.as_user(free).post(FORMALIZE_URL, {'raw_text': WRITTEN}, format='json').status_code == 429
    assert cooked(free, label='Soupe', recipe_text=WRITTEN).data['recipe_job']['status'] == 'skipped'
    assert paste(free).data['status'] == 'skipped'


def test_premium_formatting_runs_past_the_cap(cooked, make_user, settings):
    settings.AI_RECIPE_MONTHLY_CAP = 1
    premium = make_user(plan='premium')
    cooked(premium, label='Soupe', recipe_text=WRITTEN)
    resp = cooked(premium, label='Pâtes', recipe_text=WRITTEN, cooked_at='2026-11-09T20:00:00+01:00')

    with mock.patch('recipes.tasks.formalize_recipe', new=mock.AsyncMock(return_value=_formalized())):
        process_cooked_recipe_text(resp.data['recipe_job']['request_id'])

    assert Recipe.objects.get(id=resp.data['recipe']['id']).steps.count() == 1
