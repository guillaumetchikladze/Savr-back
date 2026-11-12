"""Un menu (ckd2 lot 3c) : `dishes[]` dans POST /posts/cooked/ — un repas, un post, une recette par plat
(trouvée, lien, écrite, nom seul) avec son « Ce que tu as changé » ; compatible avec le corps simple."""

from datetime import date

import pytest

from recipes.models import MealPlan, MealPlanRecipeBatch, Post, Recipe

pytestmark = pytest.mark.django_db

COOKED_URL = '/api/posts/cooked/'
FEED_URL = '/api/posts/?is_published=true'
EVENING = '2026-11-08T20:15:00+01:00'
LINK = 'https://www.marmiton.org/recettes/tarte-123.aspx'


@pytest.fixture
def cooked(api, celery_delay, django_capture_on_commit_callbacks):
    def _post(user, **data):
        data.setdefault('cooked_at', EVENING)
        with django_capture_on_commit_callbacks(execute=True):
            return api.as_user(user).post(COOKED_URL, data, format='json')

    return _post


@pytest.fixture
def lea(make_user):
    return make_user('lea', first_name='Léa')


def _batches(resp):
    post = Post.objects.get(id=resp.data['id'])
    return list(
        MealPlanRecipeBatch.objects.filter(meal_plan=post.meal_plan)
        .select_related('recipe_batch__recipe')
        .order_by('order')
    )


def test_menu_is_one_meal_one_post_with_a_recipe_per_dish_in_order(cooked, user, lea, make_recipe):
    pasta = make_recipe(lea, 'Pâtes au canard')
    tart = make_recipe(user, 'Tarte aux pommes')

    resp = cooked(
        user,
        dishes=[
            {'recipe_id': pasta.id, 'changes': 'Des pommes de terre au lieu des pâtes'},
            {'recipe_id': tart.id},
        ],
    )

    assert resp.status_code == 201, resp.data
    assert Post.objects.count() == 1 and MealPlan.objects.count() == 1
    mprbs = _batches(resp)
    assert [m.recipe_batch.recipe_id for m in mprbs] == [pasta.id, tart.id]
    assert [m.changes for m in mprbs] == ['Des pommes de terre au lieu des pâtes', '']
    assert all(m.recipe_batch.is_cooked for m in mprbs)
    assert resp.data['display_title'] == 'Pâtes au canard et Tarte aux pommes'  # « Le repas » vide
    assert [d['recipe']['id'] for d in resp.data['dishes']] == [pasta.id, tart.id]
    assert resp.data['dishes'][0]['author']['first_name'] == 'Léa'
    post = Post.objects.get(id=resp.data['id'])
    assert post.recipe_batch.recipe_id == pasta.id and post.meal_plan.confirmed


def test_meal_title_names_the_post(cooked, user, make_recipe):
    resp = cooked(
        user,
        label='Dîner du dimanche',
        dishes=[{'recipe_id': make_recipe(user, 'Velouté').id}, {'recipe_id': make_recipe(user, 'Tarte').id}],
    )

    assert resp.data['display_title'] == 'Dîner du dimanche'
    assert len(resp.data['dishes']) == 2


def test_each_dish_follows_the_lot_2_rules(cooked, user, celery_delay):
    resp = cooked(
        user,
        dishes=[
            {'label': 'Velouté de potiron', 'changes': 'Plus de sel'},  # nom seul : recette proposée
            {'recipe_text': 'Des pommes, de la pâte.', 'label': 'Tarte aux pommes', 'changes': 'x'},
            {'import_url': LINK, 'changes': 'Sans sucre'},
        ],
    )

    assert resp.status_code == 201, resp.data
    titles = [m.recipe_batch.recipe.title for m in _batches(resp)]
    assert titles == ['Velouté de potiron', 'Tarte aux pommes', 'Recette de marmiton.org']
    assert [m.changes for m in _batches(resp)] == ['', '', 'Sans sucre']  # sa recette : rien à changer
    kinds = [j['kind'] for j in resp.data['recipe_jobs']]
    assert kinds == ['generate', 'format', 'import']
    assert {j['recipe_id'] for j in resp.data['recipe_jobs']} == set(
        Recipe.objects.filter(created_by=user).values_list('id', flat=True)
    )
    assert resp.data['recipe_job']['kind'] == 'generate'


def test_undo_removes_the_whole_menu_and_its_new_recipes(cooked, api, user, make_recipe):
    kept = make_recipe(user, 'Tarte aux pommes', ingredients=['pommes'])
    resp = cooked(user, dishes=[{'label': 'Velouté'}, {'recipe_id': kept.id, 'changes': 'Moins de sucre'}])

    assert api.as_user(user).delete(f'/api/posts/{resp.data["id"]}/?undo_cooked=true').status_code == 204
    assert not Post.objects.exists() and not MealPlan.objects.exists()
    assert list(Recipe.objects.values_list('id', flat=True)) == [kept.id]


def test_planned_meal_gets_the_extra_dish_and_undo_restores_it(cooked, api, user, make_recipe, make_meal_plan):
    risotto = make_recipe(user, 'Risotto')
    salad = make_recipe(user, 'Salade')
    mp = make_meal_plan(user, day=date(2026, 11, 8), meal_time='dinner', recipes=[risotto])

    resp = cooked(
        user,
        meal_plan_id=mp.id,
        dishes=[{'recipe_id': risotto.id, 'changes': 'Avec du citron'}, {'recipe_id': salad.id}],
    )

    assert resp.status_code == 201, resp.data
    assert [m.recipe_batch.recipe_id for m in _batches(resp)] == [risotto.id, salad.id]
    assert api.as_user(user).delete(f'/api/posts/{resp.data["id"]}/?undo_cooked=true').status_code == 204
    mprbs = list(mp.meal_plan_recipe_batches.all())
    assert [(m.recipe_batch.recipe_id, m.changes) for m in mprbs] == [(risotto.id, '')]
    mp.refresh_from_db()
    assert not mp.confirmed


def test_single_dish_list_is_the_simple_body(cooked, user, make_recipe):
    pasta = make_recipe(user, 'Pâtes au canard')

    resp = cooked(user, dishes=[{'recipe_id': pasta.id, 'changes': 'Sans crème'}, {}])

    assert resp.status_code == 201, resp.data
    [mprb] = _batches(resp)
    assert mprb.recipe_batch.recipe_id == pasta.id and mprb.changes == 'Sans crème'
    assert resp.data['recipe_jobs'] is None


@pytest.mark.parametrize(
    ('dishes', 'code'),
    [
        ('pas une liste', 'invalid_dishes'),
        ([{'recipe_id': 1}, 'x'], 'invalid_dishes'),
        ([{'label': f'Plat {i}'} for i in range(9)], 'too_many_dishes'),
        ([{'label': 'Soupe'}, {'recipe_id': 999999}], 'recipe_not_found'),
        ([{'label': 'Soupe'}, {'import_url': 'pas un lien'}], 'invalid_import_url'),
    ],
)
def test_bad_menus_are_refused_before_writing(cooked, user, dishes, code):
    resp = cooked(user, dishes=dishes)

    assert resp.status_code in (400, 404) and resp.data['code'] == code
    assert not Post.objects.exists() and not Recipe.objects.exists()


def test_menu_audience_guests_and_feed(cooked, api, user, lea, follow, make_recipe):
    follow(user, lea)
    follow(lea, user)
    shared = cooked(
        user,
        dishes=[{'recipe_id': make_recipe(user, 'Velouté').id, 'changes': 'Sans crème'}, {'label': 'Tarte'}],
        guest_ids=[lea.id],
    )
    private = cooked(
        user,
        cooked_at='2026-11-09T20:00:00+01:00',
        publish=False,
        dishes=[{'recipe_id': make_recipe(user, 'Quiche').id, 'changes': 'secret'}, {'label': 'Salade'}],
    )

    feed = api.as_user(lea).get(FEED_URL).json()
    feed = feed.get('results', feed) if isinstance(feed, dict) else feed
    ids = [p['id'] for p in feed]
    assert shared.data['id'] in ids and private.data['id'] not in ids
    seen = next(p for p in feed if p['id'] == shared.data['id'])
    assert [d['changes'] for d in seen['dishes']] == ['Sans crème', '']
    assert [g['id'] for g in seen['guests']] == [lea.id]
    assert api.as_user(user).get('/api/badges/me/').status_code == 200
