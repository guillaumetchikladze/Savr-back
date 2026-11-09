"""« Foyer + invités » : foyer consenti, inclus par défaut dans les repas / le panier, convives explicites."""

from decimal import Decimal

import pytest

from accounts.models import HouseholdMember, Notification
from recipes.models import MealInvitation, MealPlanRecipeBatch, ShoppingListItem
from recipes.services.shopping_list_service import link_batch_to_shopping_list

pytestmark = pytest.mark.django_db

HOUSEHOLD_URL = '/api/household/'
BASKET_URL = '/api/basket/'

# Clés du payload repas lues par l'app 1.0.9 : elles restent (les nouveaux champs sont additifs).
LEGACY_MEAL_PLAN_KEYS = {
    'id',
    'date',
    'meal_time',
    'slot_key',
    'custom_label',
    'recipes',
    'user',
    'participants',
    'confirmed',
    'guest_count',
    'dining_alone',
    'total_guest_count',
    'total_participants',
    'total_servings',
    'servings_breakdown',
}


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache

    cache.clear()


@pytest.fixture
def pauline(make_user):
    return make_user('pauline', first_name='Pauline')


@pytest.fixture
def household(api, user, pauline, follow, django_capture_on_commit_callbacks):
    """Alice ajoute Pauline (amie) + « Jules » (sans compte) ; Pauline accepte. Retourne les ids d'entrées."""
    follow(user, pauline)
    with django_capture_on_commit_callbacks(execute=True):
        api.as_user(user).post(f'{HOUSEHOLD_URL}members/', {'user_id': pauline.id}, format='json')
    link = HouseholdMember.objects.get(owner=user, user=pauline)
    with django_capture_on_commit_callbacks(execute=True):
        api.as_user(pauline).post(f'{HOUSEHOLD_URL}requests/{link.id}/accept/')
    jules = api.as_user(user).post(f'{HOUSEHOLD_URL}members/', {'name': 'Jules', 'is_child': True}, format='json')
    assert jules.status_code == 201, jules.data
    return {'pauline': link.id, 'jules': HouseholdMember.objects.get(owner=user, name='Jules').id}


def test_household_link_needs_friendship_and_consent(
    api, user, pauline, make_user, follow, django_capture_on_commit_callbacks
):
    alice = api.as_user(user)  # un seul client : re-`as_user` avant chaque appel d'un autre compte
    stranger = make_user('stranger')
    assert alice.post(f'{HOUSEHOLD_URL}members/', {'user_id': stranger.id}, format='json').status_code == 403

    follow(pauline, user)  # amis dans un sens suffit
    with django_capture_on_commit_callbacks(execute=True):
        resp = alice.post(f'{HOUSEHOLD_URL}members/', {'user_id': pauline.id}, format='json')
    assert resp.status_code == 201 and resp.data['action'] == 'request_sent'
    assert [(m['kind'], m['status']) for m in resp.data['members']] == [('user', 'pending')]
    request_notif = Notification.objects.get(user=pauline, notification_type='household_request')
    assert request_notif.message == 'alice veut t’ajouter à son foyer.'

    # En attente : pas encore dans le foyer (ni des repas).
    pauline_view = api.as_user(pauline).get(HOUSEHOLD_URL).data
    assert pauline_view['members'] == [] and len(pauline_view['incoming_requests']) == 1
    link_id = pauline_view['incoming_requests'][0]['id']

    with django_capture_on_commit_callbacks(execute=True):
        accepted = api.as_user(pauline).post(f'{HOUSEHOLD_URL}requests/{link_id}/accept/')
    assert accepted.status_code == 200
    assert Notification.objects.get(user=user, notification_type='household_accepted').message == (
        'pauline a rejoint ton foyer.'
    )
    # Lien réciproque : chacun voit l'autre dans son foyer.
    assert [m['user']['id'] for m in accepted.data['members']] == [user.id]
    assert [m['name'] for m in api.as_user(user).get(HOUSEHOLD_URL).data['members']] == ['Pauline']

    # PUT déclaratif : Pauline quitte le foyer en renvoyant une liste vide.
    assert api.as_user(pauline).put(HOUSEHOLD_URL, {'members': []}, format='json').status_code == 200
    assert not HouseholdMember.objects.exists()


def test_meal_includes_household_by_default_and_per_meal_exclusion(
    api, user, pauline, make_user, make_recipe, make_meal_plan, household
):
    friend = make_user('friend')
    mp = make_meal_plan(user, recipes=[make_recipe(user, 'Quiche', servings=2)], guest_count=2)
    MealInvitation.objects.create(inviter=user, invitee=friend, meal_plan=mp, status='accepted')
    # Pauline invitée « à l'ancienne » (app 1.0.9) : comptée une seule fois (foyer).
    MealInvitation.objects.create(inviter=user, invitee=pauline, meal_plan=mp, status='accepted')
    alice = api.as_user(user)

    data = alice.get(f'/api/meal-plans/{mp.id}/').data
    assert LEGACY_MEAL_PLAN_KEYS <= set(data)
    diners = data['diners']
    # 1 (toi) + Pauline + Jules + 2 invités sans compte + friend (Pauline dédupliquée)
    assert diners['total'] == data['servings_suggested'] == data['total_servings'] == 6
    assert [m['name'] for m in diners['household']] == ['Pauline', 'Jules']
    assert diners['guests'] == 2 and [u['id'] for u in diners['invited_accepted']] == [friend.id]
    assert data['recipes'][0]['portions'] == 6  # les portions « suivent » le total

    # Jules n'est pas là ce soir : on le décoche pour ce repas seulement.
    resp = alice.patch(f'/api/meal-plans/{mp.id}/', {'excluded_household_ids': [household['jules']]}, format='json')
    assert resp.status_code == 200, resp.data
    assert resp.data['diners']['total'] == resp.data['total_servings'] == 5
    assert [m['name'] for m in resp.data['diners']['household_excluded']] == ['Jules']
    other = make_meal_plan(user, meal_time='dinner')
    assert alice.get(f'/api/meal-plans/{other.id}/').data['diners']['total'] == 3  # toi, Pauline, Jules


def test_shopping_quantities_follow_household_total(user, make_recipe, make_meal_plan, make_shopping_list, household):
    quiche = make_recipe(user, 'Quiche', ingredients=['oeufs'], servings=2)
    mp = make_meal_plan(user, recipes=[quiche])  # toi + Pauline + Jules = 3
    sl = make_shopping_list(user)
    link_batch_to_shopping_list(sl, MealPlanRecipeBatch.objects.get(meal_plan=mp).recipe_batch)
    item = ShoppingListItem.objects.get(shopping_list=sl)
    assert sum(Decimal(str(q.quantity)) for q in item.quantities.all()) == Decimal('1.5')  # 3/2 recette


def test_basket_items_take_week_composition_and_shopping_follows(api, user, make_recipe, household):
    alice = api.as_user(user)
    quiche = make_recipe(user, 'Quiche', ingredients=['oeufs'], servings=2)
    first = alice.post(BASKET_URL, {'recipe_id': quiche.id}, format='json').data
    assert first['diners']['total'] == first['servings_suggested'] == 3 and first['servings'] is None

    # En-tête « Pour N » : +1 invité, Jules décoché → toutes les envies non datées de la semaine.
    week = alice.post(
        f'{BASKET_URL}diners/', {'guest_count': 1, 'excluded_household_ids': [household['jules']]}, format='json'
    ).data
    assert week['diners']['total'] == 3 and week['items'][0]['diners']['total'] == 3
    second = alice.post(BASKET_URL, {'label': 'Salade'}, format='json').data  # reprend la composition
    assert (second['guest_count'], second['excluded_household_ids']) == (1, [household['jules']])

    resp = alice.post(f'{BASKET_URL}shopping-list/', {'name': 'Courses'}, format='json')
    assert resp.status_code == 201, resp.data
    item = ShoppingListItem.objects.get(shopping_list_id=resp.data['shopping_list']['id'])
    assert sum(Decimal(str(q.quantity)) for q in item.quantities.all()) == Decimal('1.5')


def test_blocking_removes_household_link_and_shared_names(
    api, user, pauline, make_user, follow, make_meal_plan, household
):
    from accounts.services.household_service import household_for

    HouseholdMember.objects.create(owner=pauline, name='Léo', is_child=True)  # partagé via le lien
    tom = make_user('tom')
    follow(tom, pauline)
    HouseholdMember.objects.create(owner=tom, user=pauline, status='pending')  # demande en attente, autre sens
    assert {e.name for e in household_for(user.id)} == {'Pauline', 'Jules', 'Léo'}

    assert api.as_user(pauline).post(f'/api/auth/users/{user.id}/block/').status_code == 201
    assert api.as_user(pauline).post(f'/api/auth/users/{tom.id}/block/').status_code == 201

    assert not HouseholdMember.objects.filter(user__isnull=False).exists()
    assert [e.name for e in household_for(user.id)] == ['Jules']  # Léo (de Pauline) ne s'applique plus
    assert [e.name for e in household_for(pauline.id)] == ['Léo']
    mp = make_meal_plan(user)
    assert api.as_user(user).get(f'/api/meal-plans/{mp.id}/').data['diners']['total'] == 2  # toi + Jules
