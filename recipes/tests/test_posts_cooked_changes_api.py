"""« Ce que tu as changé » (ckd2 lot 3b) : stocké par recette du repas (`MealPlanRecipeBatch.changes`),
même audience que le post (privé si « toi seulement »), exposé sur le post et la page du plat, retiré
par Annuler, modifiable après coup (update-content)."""

from datetime import date

import pytest

from recipes.models import MealPlanRecipeBatch, Post

pytestmark = pytest.mark.django_db

COOKED_URL = '/api/posts/cooked/'
FEED_URL = '/api/posts/?is_published=true'
EVENING = '2026-11-08T20:15:00+01:00'
POTATOES = 'Des pommes de terre au lieu des pâtes.'


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


def _mprb(resp) -> MealPlanRecipeBatch:
    post = Post.objects.get(id=resp.data['id'])
    return MealPlanRecipeBatch.objects.get(meal_plan=post.meal_plan, recipe_batch=post.recipe_batch)


def _feed_post(api, viewer, post_id):
    results = api.as_user(viewer).get(FEED_URL).json()
    results = results.get('results', results) if isinstance(results, dict) else results
    return next((p for p in results if p['id'] == post_id), None)


def test_changes_go_on_the_followed_recipe_and_the_dish_keeps_its_own_name(cooked, user, lea, make_recipe):
    pasta = make_recipe(lea, 'Pâtes au canard')

    resp = cooked(user, recipe_id=pasta.id, label='Gratin de pommes de terre', changes=f'  {POTATOES}  ')

    assert resp.status_code == 201, resp.data
    assert _mprb(resp).changes == POTATOES
    assert resp.data['display_title'] == 'Gratin de pommes de terre'  # le post s'appelle comme le plat
    [dish] = resp.data['dishes']
    assert dish['recipe'] == {'id': pasta.id, 'title': 'Pâtes au canard', 'image_url': None}
    assert dish['changes'] == POTATOES
    assert dish['author'] == {'id': lea.id, 'username': 'lea', 'first_name': 'Léa'}  # « D'après Léa »


def test_own_recipe_has_no_author_and_no_changes_means_empty(cooked, user, make_recipe):
    risotto = make_recipe(user, 'Risotto aux asperges')
    with_changes = cooked(user, recipe_id=risotto.id, changes='Sans parmesan, avec du citron.')
    plain = cooked(user, recipe_id=make_recipe(user, 'Quiche').id)

    assert with_changes.data['dishes'][0]['changes'] == 'Sans parmesan, avec du citron.'
    assert with_changes.data['dishes'][0]['author'] is None
    assert plain.data['dishes'][0]['changes'] == '' and plain.data['dishes'][0]['author'] is None


def test_changes_are_capped_trimmed_and_must_be_text(cooked, user, make_recipe):
    recipe = make_recipe(user, 'Tarte aux pommes')

    long = cooked(user, recipe_id=recipe.id, changes='x' * 900)
    bad = cooked(user, recipe_id=make_recipe(user, 'Soupe').id, changes=['pas', 'un texte'])

    assert long.status_code == 201 and len(_mprb(long).changes) == 500
    assert bad.status_code == 400 and bad.data['code'] == 'invalid_changes'
    assert not Post.objects.filter(recipe_batch__recipe__title='Soupe').exists()


def test_own_written_or_named_recipe_takes_no_changes(cooked, user):
    written = cooked(user, label='Soupe', recipe_text='Des légumes, de l’eau.', changes='Plus de sel')
    named = cooked(user, label='Crêpes', changes='Plus de sucre', cooked_at='2026-11-09T20:00:00+01:00')

    assert _mprb(written).changes == '' and _mprb(named).changes == ''


def test_pasted_link_takes_changes(cooked, user):
    resp = cooked(user, import_url='https://www.marmiton.org/recettes/pates-123.aspx', changes='Sans crème')

    assert resp.status_code == 201, resp.data
    assert _mprb(resp).changes == 'Sans crème'


def test_planned_meal_changes_go_on_its_recipe_and_undo_restores_it(cooked, api, user, make_recipe, make_meal_plan):
    risotto = make_recipe(user, 'Risotto aux asperges')
    mp = make_meal_plan(user, day=date(2026, 11, 8), meal_time='dinner', recipes=[risotto])
    mprb = mp.meal_plan_recipe_batches.get()

    resp = cooked(user, meal_plan_id=mp.id, changes='Avec du citron')
    mprb.refresh_from_db()
    assert mprb.changes == 'Avec du citron'

    assert api.as_user(user).delete(f'/api/posts/{resp.data["id"]}/?undo_cooked=true').status_code == 204
    mprb.refresh_from_db()
    assert mprb.changes == ''  # parti avec le log, la recette prévue reste


def test_undo_of_a_new_meal_removes_its_changes(cooked, api, user, make_recipe):
    resp = cooked(user, recipe_id=make_recipe(user, 'Quiche').id, changes='Sans lardons')
    mprb_id = _mprb(resp).id

    assert api.as_user(user).delete(f'/api/posts/{resp.data["id"]}/?undo_cooked=true').status_code == 204
    assert not MealPlanRecipeBatch.objects.filter(id=mprb_id).exists()


def test_friends_see_changes_with_the_post_never_on_a_private_one(cooked, api, user, lea, follow, make_recipe):
    follow(lea, user)
    pasta = make_recipe(lea, 'Pâtes au canard')
    shared = cooked(user, recipe_id=pasta.id, changes=POTATOES)
    private = cooked(user, recipe_id=make_recipe(user, 'Quiche').id, changes='Sans lardons', publish=False)

    seen = _feed_post(api, lea, shared.data['id'])
    assert seen['dishes'][0]['changes'] == POTATOES
    assert seen['dishes'][0]['author']['first_name'] == 'Léa'
    assert _feed_post(api, lea, private.data['id']) is None
    assert api.as_user(lea).get(f'/api/posts/{private.data["id"]}/').status_code == 404
    # Le planning (invités, foyer) n'expose jamais le texte : seulement les posts.
    assert 'Sans lardons' not in api.as_user(user).get('/api/meal-plans/', {'date': '2026-11-08'}).content.decode()


def test_dish_page_shows_changes_to_viewers_of_the_post(cooked, api, user, lea, follow, make_recipe):
    follow(lea, user)
    pasta = make_recipe(lea, 'Pâtes au canard')
    cooked(user, recipe_id=pasta.id, changes=POTATOES)
    cooked(user, recipe_id=pasta.id, changes='Rien que pour moi', publish=False, cooked_at='2026-11-09T20:00:00+01:00')

    mine = api.as_user(user).get(f'/api/dishes/r-{pasta.id}/').json()['versions']['results']
    friends = api.as_user(lea).get(f'/api/dishes/r-{pasta.id}/').json()['friends']['posts']

    assert {v['post']['changes'] for v in mine} == {POTATOES, 'Rien que pour moi'}
    assert [p['changes'] for p in friends] == [POTATOES]


def test_changes_can_be_edited_after_posting(cooked, api, user, make_recipe):
    resp = cooked(user, recipe_id=make_recipe(user, 'Quiche').id, changes='Sans lardons')
    post = Post.objects.get(id=resp.data['id'])
    url = f'/api/posts/{post.id}/update-content/'
    client = api.as_user(user)

    ok = client.patch(
        url, {'changes': [{'recipe_batch_id': post.recipe_batch_id, 'changes': ' Avec du bacon '}]}, format='json'
    )
    other = client.patch(url, {'changes': [{'recipe_batch_id': 999999, 'changes': 'x'}]}, format='json')
    cleared = client.patch(url, {'changes': [{'recipe_batch_id': post.recipe_batch_id, 'changes': ''}]}, format='json')

    assert ok.status_code == 200 and ok.data['dishes'][0]['changes'] == 'Avec du bacon'
    assert other.status_code == 400 and other.data['code'] == 'invalid_changes'
    assert cleared.status_code == 200 and cleared.data['dishes'][0]['changes'] == ''
