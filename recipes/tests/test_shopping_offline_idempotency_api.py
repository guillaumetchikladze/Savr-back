"""
Liste de courses hors-ligne : `X-Idempotency-Key` (rejeu sans ré-application, 409 si la clé change de requête),
opérations idempotentes par nature (cocher / décocher, `client_id`, suppression, archive / unarchive).
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from core.idempotency import purge_expired
from core.models import IdempotencyRecord
from recipes.models import ShoppingList, ShoppingListItem

pytestmark = pytest.mark.django_db

ITEMS_URL = '/api/shopping-list-items/'


def _add(client, shopping_list, name='Tomates', quantity=3, key=None, **extra):
    body = {'shopping_list_id': shopping_list.id, 'ingredient_name': name, 'quantity': quantity, 'unit': 'piece'}
    headers = {'HTTP_X_IDEMPOTENCY_KEY': key} if key else {}
    return client.post(ITEMS_URL, {**body, **extra}, format='json', **headers)


def _total(item_id):
    return sum(Decimal(str(q.quantity)) for q in ShoppingListItem.objects.get(id=item_id).quantities.all())


def test_replayed_request_returns_stored_response_and_applies_once(api, user, make_shopping_list):
    sl = make_shopping_list(user)
    client = api.as_user(user)

    first = _add(client, sl, quantity=3, key='add-1')
    retry = _add(client, sl, quantity=3, key='add-1')
    assert first.status_code == retry.status_code == 201
    assert retry.json() == first.json()
    assert retry['Idempotent-Replayed'] == 'true'
    item_id = first.data['id']
    assert _total(item_id) == Decimal('3')  # pas ré-additionné

    # Check rejoué après un décochage : la réponse mémorisée revient, l'état n'est pas re-coché.
    checked = client.post(f'{ITEMS_URL}{item_id}/check/', HTTP_X_IDEMPOTENCY_KEY='check-1')
    assert checked.status_code == 200 and checked.data['checked_at']
    client.post(f'{ITEMS_URL}{item_id}/uncheck/')
    replay = client.post(f'{ITEMS_URL}{item_id}/check/', HTTP_IDEMPOTENCY_KEY='check-1')
    assert replay.status_code == 200 and replay.json() == checked.json()
    assert ShoppingListItem.objects.get(id=item_id).checked_at is None

    # Les clés sont propres à chaque utilisateur.
    assert IdempotencyRecord.objects.filter(user=user).count() == 2


def test_key_reused_for_another_request_is_409_and_errors_are_not_stored(api, user, make_shopping_list):
    sl = make_shopping_list(user)
    client = api.as_user(user)
    item_id = _add(client, sl, quantity=2, key='k').data['id']

    assert _add(client, sl, quantity=5, key='k').status_code == 409  # autre corps
    assert client.post(f'{ITEMS_URL}{item_id}/check/', HTTP_X_IDEMPOTENCY_KEY='k').status_code == 409  # autre chemin
    assert _total(item_id) == Decimal('2')

    # Une erreur n'est pas mémorisée : la même clé reste utilisable une fois la requête valide.
    assert client.post(f'{ITEMS_URL}999999/check/', HTTP_X_IDEMPOTENCY_KEY='k2').status_code == 404
    assert client.post(f'{ITEMS_URL}{item_id}/check/', HTTP_X_IDEMPOTENCY_KEY='k2').status_code == 200


def test_expired_keys_are_purged_and_reusable(api, user, make_shopping_list):
    sl = make_shopping_list(user)
    client = api.as_user(user)
    item_id = _add(client, sl, quantity=2, key='old').data['id']
    IdempotencyRecord.objects.filter(key='old').update(created_at=timezone.now() - timedelta(hours=49))

    # Expirée mais pas encore purgée : traitée comme une clé neuve.
    assert _add(client, sl, quantity=2, key='old').status_code == 200
    assert _total(item_id) == Decimal('4')

    IdempotencyRecord.objects.filter(key='old').update(created_at=timezone.now() - timedelta(hours=49))
    assert purge_expired() == 1
    assert not IdempotencyRecord.objects.exists()


def test_check_and_uncheck_set_state_instead_of_toggling(api, user, make_user, make_shopping_list):
    collab = make_user('collab')
    sl = make_shopping_list(user, collaborators=[collab])
    item_id = _add(api.as_user(user), sl, quantity=4).data['id']

    assert api.as_user(user).post(f'{ITEMS_URL}{item_id}/check/').status_code == 200
    first_checked_at = ShoppingListItem.objects.get(id=item_id).checked_at
    again = api.as_user(collab).post(f'{ITEMS_URL}{item_id}/check/')
    assert again.status_code == 200 and again.data['status'] == 'purchased'
    item = ShoppingListItem.objects.get(id=item_id)
    assert item.checked_by == user and item.checked_at == first_checked_at  # premier qui a coché conservé

    client = api.as_user(user)
    for _ in range(2):
        resp = client.post(f'{ITEMS_URL}{item_id}/uncheck/')
        assert resp.status_code == 200 and resp.data['checked_at'] is None
        assert resp.data['remaining_quantity'] == 4.0


def test_add_with_client_id_twice_creates_one_item(api, user, make_shopping_list):
    sl = make_shopping_list(user)
    client = api.as_user(user)

    first = _add(client, sl, quantity=3, client_id='c-1')
    retry = _add(client, sl, quantity=3, client_id='c-1')
    assert (first.status_code, retry.status_code) == (201, 200)
    assert retry.data['id'] == first.data['id']
    assert _total(first.data['id']) == Decimal('3')

    # Un autre ajout du même article (autre client_id) fusionne toujours dans la même ligne.
    assert _add(client, sl, quantity=1, client_id='c-2').data['id'] == first.data['id']
    assert _total(first.data['id']) == Decimal('4')
    assert ShoppingListItem.objects.filter(shopping_list=sl).count() == 1


def test_delete_is_idempotent_but_stays_private(api, user, make_user, make_shopping_list):
    sl = make_shopping_list(user)
    item_id = _add(api.as_user(user), sl).data['id']

    outsider = api.as_user(make_user('outsider'))
    assert outsider.delete(f'{ITEMS_URL}{item_id}/').status_code == 404
    assert ShoppingListItem.objects.filter(id=item_id).exists()

    client = api.as_user(user)
    assert client.delete(f'{ITEMS_URL}{item_id}/').status_code == 204
    assert client.delete(f'{ITEMS_URL}{item_id}/').status_code == 204
    assert not ShoppingListItem.objects.filter(id=item_id).exists()


def test_archive_and_unarchive_are_idempotent_and_legacy_toggle_still_works(api, user, make_shopping_list):
    sl = make_shopping_list(user)
    client = api.as_user(user)
    url = f'/api/shopping-lists/{sl.id}/'

    for _ in range(2):
        resp = client.post(f'{url}archive/', {'is_archived': True}, format='json')
        assert resp.status_code == 200 and resp.data['is_archived'] is True
    assert client.get('/api/shopping-lists/').data['count'] == 0
    assert client.get(url).status_code == 200  # une liste archivée s'ouvre encore

    for attempt in range(2):
        resp = client.post(f'{url}unarchive/', HTTP_X_IDEMPOTENCY_KEY=f'unarchive-{attempt}')
        assert resp.status_code == 200 and resp.data['is_archived'] is False

    # App 1.0.9 : `archive/` sans corps bascule (y compris pour désarchiver une liste archivée).
    assert client.post(f'{url}archive/').data['is_archived'] is True
    assert client.post(f'{url}archive/').data['is_archived'] is False
    assert ShoppingList.objects.get(id=sl.id).is_archived is False
