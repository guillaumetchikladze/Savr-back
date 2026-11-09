"""Listes de courses : ajout d'articles, cocher/décocher, permissions membres / non-membres."""

from decimal import Decimal

import pytest

from recipes.models import ShoppingListItem, ShoppingListMember

pytestmark = pytest.mark.django_db

ITEMS_URL = '/api/shopping-list-items/'


def _add(client, shopping_list, name='Tomates', quantity=3, unit='piece'):
    return client.post(
        ITEMS_URL,
        {'shopping_list_id': shopping_list.id, 'ingredient_name': name, 'quantity': quantity, 'unit': unit},
        format='json',
    )


def _total(item):
    return sum(Decimal(str(q.quantity)) for q in item.quantities.all())


def test_member_adds_item_and_same_item_is_merged(api, user, make_shopping_list):
    sl = make_shopping_list(user)
    client = api.as_user(user)

    first = _add(client, sl, quantity=3)
    assert first.status_code == 201, first.data
    again = _add(client, sl, name='tomates', quantity=2)
    assert again.status_code == 200

    item = ShoppingListItem.objects.get(shopping_list=sl)
    assert _total(item) == Decimal('5')


def test_collaborator_checks_and_unchecks_item(api, user, make_user, make_shopping_list):
    collaborator = make_user('collab')
    sl = make_shopping_list(user, collaborators=[collaborator])
    item_id = _add(api.as_user(user), sl, quantity=4).data['id']
    client = api.as_user(collaborator)

    checked = client.post(f'{ITEMS_URL}{item_id}/check/')
    assert checked.status_code == 200
    item = ShoppingListItem.objects.get(id=item_id)
    assert item.checked_by == collaborator and item.checked_at is not None
    assert sum(q.checked_quantity for q in item.quantities.all()) == Decimal('4')

    assert client.post(f'{ITEMS_URL}{item_id}/uncheck/').status_code == 200
    item.refresh_from_db()
    assert item.checked_at is None
    assert sum(q.checked_quantity for q in item.quantities.all()) == 0


def test_non_member_is_locked_out(api, user, make_user, make_shopping_list):
    sl = make_shopping_list(user)
    item_id = _add(api.as_user(user), sl).data['id']
    outsider = api.as_user(make_user('outsider'))

    assert outsider.get(f'/api/shopping-lists/{sl.id}/').status_code == 404
    assert outsider.get('/api/shopping-lists/').data['count'] == 0
    assert _add(outsider, sl).status_code == 404
    assert outsider.post(f'{ITEMS_URL}{item_id}/check/').status_code == 404
    assert outsider.get(ITEMS_URL, {'shopping_list_id': sl.id}).data['count'] == 0
    assert ShoppingListItem.objects.get(id=item_id).checked_at is None


def test_only_owner_can_remove_members(api, user, make_user, make_shopping_list):
    collab, other = make_user('collab'), make_user('other')
    sl = make_shopping_list(user, collaborators=[collab, other])

    denied = api.as_user(collab).post(
        f'/api/shopping-lists/{sl.id}/remove_member/', {'user_id': other.id}, format='json'
    )
    assert denied.status_code == 403

    owner_client = api.as_user(user)
    assert (
        owner_client.post(
            f'/api/shopping-lists/{sl.id}/remove_member/', {'user_id': user.id}, format='json'
        ).status_code
        == 400
    )
    assert (
        owner_client.post(
            f'/api/shopping-lists/{sl.id}/remove_member/', {'user_id': other.id}, format='json'
        ).status_code
        == 200
    )
    assert not ShoppingListMember.objects.filter(shopping_list=sl, user=other).exists()
