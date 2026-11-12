"""« Refaire sa version » (ckd2 lot 3b, fx07) : depuis un post « D'après Léa · Sa version : … », le viewer
obtient sa propre recette (copie publique `adapted_from`, `adaptation_kind: post_version`), changements
appliqués par l'IA à la demande (mockée : pas de clé IA en local), plafond mensuel, repli copie simple,
idempotence par empreinte du texte, visibilité du post."""

from decimal import Decimal
from unittest import mock

import pytest

from recipes.models import Post, Recipe, RecipeImportRequest, Step
from recipes.services import equipment_service
from recipes.services.post_version import changes_signature
from recipes.services.pydantic_models import RecipeFormalized, RecipeIngredientFormalized, StepFormalized

pytestmark = pytest.mark.django_db

COOKED_URL = '/api/posts/cooked/'
EVENING = '2026-11-08T20:15:00+01:00'
POTATOES = 'Des pommes de terre au lieu des pâtes.'
REVISE = 'recipes.services.post_version._revise_with_ai'


def _url(post_id):
    return f'/api/posts/{post_id}/redo-version/'


def _revised(title='Gratin de canard aux pommes de terre'):
    return RecipeFormalized(
        title=title,
        description='Plus fondant.',
        steps_summary='Cuire, poêler, gratiner.',
        prep_time=15,
        cook_time=40,
        servings=2,
        recipe_ingredients=[
            RecipeIngredientFormalized(ingredient_name='pommes de terre', quantity=Decimal('600'), unit='g'),
            RecipeIngredientFormalized(ingredient_name='magret de canard', quantity=Decimal('300'), unit='g'),
        ],
        steps=[
            StepFormalized(order=1, instruction='Cuire les pommes de terre.'),
            StepFormalized(order=2, instruction='Poêler le canard puis gratiner.'),
        ],
    )


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


@pytest.fixture
def tom(make_user):
    return make_user('tom', first_name='Tom')


@pytest.fixture
def pasta(make_recipe, lea):
    recipe = make_recipe(lea, 'Pâtes au canard', ingredients=['pâtes', 'magret de canard'], description='Du dimanche')
    Step.objects.create(recipe=recipe, order=1, instruction='Cuire les pâtes.')
    Step.objects.create(recipe=recipe, order=2, instruction='Poêler le canard.')
    return recipe


@pytest.fixture
def tom_post(cooked, tom, pasta):
    resp = cooked(tom, recipe_id=pasta.id, label='Gratin', changes=POTATOES)
    assert resp.status_code == 201, resp.data
    return resp.data


def test_redo_copies_the_original_applies_the_changes_with_ai_and_opens_mine(
    api, user, tom, follow, pasta, tom_post, django_capture_on_commit_callbacks
):
    follow(user, tom)
    with mock.patch(REVISE, return_value=_revised()) as revise:
        with django_capture_on_commit_callbacks(execute=False):
            resp = api.as_user(user).post(_url(tom_post['id']), {}, format='json')

    assert resp.status_code == 201, resp.data
    assert resp.data['created'] is True and resp.data['ai_applied'] is True
    assert resp.data['original_recipe_id'] == pasta.id
    revise.assert_called_once()
    copy = Recipe.objects.get(id=resp.data['recipe']['id'])
    assert copy.created_by == user and copy.is_public is True  # prv1
    assert copy.adapted_from_id == pasta.id and copy.adaptation_kind == Recipe.ADAPTATION_POST_VERSION
    assert copy.adaptation_signature == changes_signature(POTATOES)
    assert copy.title == 'Gratin de canard aux pommes de terre'
    assert copy.description.startswith('D’après la version de Tom : Des pommes de terre au lieu des pâtes.')
    assert sorted(copy.recipe_ingredients.values_list('ingredient__name', flat=True)) == [
        'magret de canard',
        'pommes de terre',
    ]
    assert list(copy.steps.order_by('order').values_list('instruction', flat=True))[0] == 'Cuire les pommes de terre.'
    assert resp.data['recipe']['adapted_from'] == {'id': pasta.id, 'title': 'Pâtes au canard'}
    assert resp.data['recipe']['adaptation_kind'] == 'post_version'
    assert resp.data['recipe']['adapted_for_equipment'] is None
    # L'original n'a pas bougé ; l'appel compte dans le plafond mensuel.
    pasta.refresh_from_db()
    assert pasta.title == 'Pâtes au canard' and pasta.steps.count() == 2
    job = RecipeImportRequest.objects.get(user=user)
    assert job.payload['quota'] == 'ai_recipe' and job.payload['job_type'] == 'post_version'
    assert job.status == RecipeImportRequest.STATUS_SUCCESS and job.recipe_id == copy.id


def test_tapping_twice_returns_the_same_copy_without_ai(api, user, tom, follow, tom_post):
    follow(user, tom)
    client = api.as_user(user)
    with mock.patch(REVISE, return_value=_revised()):
        first = client.post(_url(tom_post['id']), {}, format='json')
    with mock.patch(REVISE) as revise:
        second = client.post(_url(tom_post['id']), {}, format='json')

    assert first.status_code == 201 and second.status_code == 200
    assert second.data['created'] is False and second.data['ai_applied'] is None
    assert second.data['recipe']['id'] == first.data['recipe']['id']
    revise.assert_not_called()
    assert Recipe.objects.filter(adaptation_kind=Recipe.ADAPTATION_POST_VERSION).count() == 1
    assert RecipeImportRequest.objects.filter(user=user).count() == 1  # la 2e fois ne coûte rien


def test_ai_failure_gives_a_plain_copy_with_the_changes_as_a_note(api, user, tom, follow, pasta, tom_post):
    follow(user, tom)
    with mock.patch(REVISE, side_effect=TimeoutError('slow')):
        resp = api.as_user(user).post(_url(tom_post['id']), {}, format='json')

    assert resp.status_code == 201, resp.data
    assert resp.data['ai_applied'] is False
    copy = Recipe.objects.get(id=resp.data['recipe']['id'])
    assert copy.title == 'Pâtes au canard' and copy.is_public is True
    assert copy.description == 'D’après la version de Tom : Des pommes de terre au lieu des pâtes.\n\nDu dimanche'
    assert list(copy.steps.order_by('order').values_list('instruction', flat=True)) == [
        'Cuire les pâtes.',
        'Poêler le canard.',
    ]
    assert sorted(copy.recipe_ingredients.values_list('ingredient__name', flat=True)) == ['magret de canard', 'pâtes']
    job = RecipeImportRequest.objects.get(user=user)
    assert job.status == RecipeImportRequest.STATUS_ERROR and job.payload['quota'] == 'ai_recipe'


def test_unusable_revision_keeps_the_plain_copy(api, user, tom, follow, tom_post):
    follow(user, tom)
    empty = _revised()
    empty.steps = []
    with mock.patch(REVISE, return_value=empty):
        resp = api.as_user(user).post(_url(tom_post['id']), {}, format='json')

    assert resp.status_code == 201 and resp.data['ai_applied'] is False
    copy = Recipe.objects.get(id=resp.data['recipe']['id'])
    assert copy.title == 'Pâtes au canard' and copy.steps.count() == 2


def test_over_the_monthly_cap_no_ai_call_plain_copy_premium_exempt(
    api, user, make_user, tom, follow, tom_post, settings
):
    settings.AI_RECIPE_MONTHLY_CAP = 1
    RecipeImportRequest.objects.create(user=user, payload={'quota': 'ai_recipe'})
    follow(user, tom)
    with mock.patch(REVISE, return_value=_revised()) as revise:
        capped = api.as_user(user).post(_url(tom_post['id']), {}, format='json')
    assert capped.status_code == 201 and capped.data['ai_applied'] is False
    revise.assert_not_called()
    assert RecipeImportRequest.objects.filter(user=user).count() == 1  # rien de plus compté

    premium = make_user('vip', plan='premium')
    RecipeImportRequest.objects.create(user=premium, payload={'quota': 'ai_recipe'})
    follow(premium, tom)
    with mock.patch(REVISE, return_value=_revised()) as revise:
        exempt = api.as_user(premium).post(_url(tom_post['id']), {}, format='json')
    assert exempt.status_code == 201 and exempt.data['ai_applied'] is True
    revise.assert_called_once()


def test_only_viewers_who_see_the_post(api, user, make_user, tom, follow, cooked, pasta, tom_post):
    stranger = make_user('stranger')
    with mock.patch(REVISE) as revise:
        hidden = api.as_user(stranger).post(_url(tom_post['id']), {}, format='json')
    assert hidden.status_code == 404 and hidden.data['code'] == 'not_found'
    revise.assert_not_called()

    # Post « rien que pour toi » de Tom : ses amis ne le voient pas, lui oui.
    private = cooked(
        tom,
        recipe_id=pasta.id,
        label='Gratin',
        changes='Avec du gruyère.',
        publish=False,
        cooked_at='2026-11-09T20:00:00+01:00',
    )
    assert private.status_code == 201, private.data
    assert Post.objects.get(id=private.data['id']).is_published is False
    follow(user, tom)
    with mock.patch(REVISE, side_effect=RuntimeError('no key')):
        friend = api.as_user(user).post(_url(private.data['id']), {}, format='json')
        own = api.as_user(tom).post(_url(private.data['id']), {}, format='json')
    assert friend.status_code == 404
    assert own.status_code == 201 and own.data['recipe']['description'].startswith('Ta version : Avec du gruyère.')
    assert not Recipe.objects.filter(created_by=stranger).exists()


def test_a_post_without_changes_has_nothing_to_redo(api, user, tom, follow, cooked, pasta):
    follow(user, tom)
    plain = cooked(tom, recipe_id=pasta.id)
    resp = api.as_user(user).post(_url(plain.data['id']), {}, format='json')
    bad = api.as_user(user).post(_url(plain.data['id']), {'recipe_batch_id': 'x'}, format='json')

    assert resp.status_code == 400 and resp.data['code'] == 'no_changes'
    assert bad.status_code == 400 and bad.data['code'] == 'invalid_dish'


def test_menu_picks_the_dish_and_other_changes_make_another_copy(
    api, user, tom, follow, cooked, pasta, make_recipe, lea
):
    pie = make_recipe(lea, 'Tarte aux pommes', ingredients=['pommes'])
    menu = cooked(
        tom,
        label='Dîner',
        dishes=[{'recipe_id': pasta.id, 'changes': POTATOES}, {'recipe_id': pie.id, 'changes': 'Avec des poires.'}],
    )
    assert menu.status_code == 201, menu.data
    pie_batch = next(d['recipe_batch_id'] for d in menu.data['dishes'] if d['recipe']['id'] == pie.id)
    follow(user, tom)
    with mock.patch(REVISE, side_effect=RuntimeError('no key')):
        client = api.as_user(user)
        first = client.post(_url(menu.data['id']), {}, format='json')
        tart = client.post(_url(menu.data['id']), {'recipe_batch_id': pie_batch}, format='json')

    assert first.data['original_recipe_id'] == pasta.id  # défaut : le premier plat changé
    assert tart.status_code == 201 and tart.data['original_recipe_id'] == pie.id
    assert tart.data['recipe']['description'].startswith('D’après la version de Tom : Avec des poires.')


def test_post_versions_never_pass_for_an_equipment_adaptation(api, user, tom, follow, pasta, tom_post):
    follow(user, tom)
    with mock.patch(REVISE, side_effect=RuntimeError('no key')):
        api.as_user(user).post(_url(tom_post['id']), {}, format='json')
    equipment_service.set_equipment(user, {'equipment': [{'key': 'airfryer'}]})
    user.refresh_from_db()

    from recipes.services.equipment_adaptation import existing_adaptation

    assert existing_adaptation(pasta.id, user, equipment_service.effective_kitchen(user)) is None
