"""« Les tablées » : consentement, plafonds, départ, semaine collective (visibilité, blocage), rituel opt-in."""

from datetime import timedelta

import pytest

from accounts.models import Notification, UserBlock
from recipes.models import BasketItem, Post
from recipes.services.basket_service import current_week_start
from tables.models import Table, TableMember

TABLES = '/api/tables/'


def _detail(t) -> str:
    return f'{TABLES}{t}/'


@pytest.fixture
def crew(make_user, follow):
    """alice (créatrice) et ses amies : lea (alice la suit), tom (tom suit alice : ami·e dans un sens)."""
    alice, lea, tom = make_user('alice'), make_user('lea'), make_user('tom')
    follow(alice, lea)
    follow(tom, alice)
    return alice, lea, tom


def _table(api, owner, *members, name='Les colocs'):
    """Tablée créée par `owner`, chaque membre invité puis acceptant."""
    res = api.as_user(owner).post(TABLES, {'name': name, 'member_ids': [m.id for m in members]}, format='json')
    assert res.status_code == 201, res.content
    table_id = res.json()['id']
    for m in members:
        assert api.as_user(m).post(f'{_detail(table_id)}accept/').status_code == 200
    return table_id


def test_invitation_needs_friendship_and_consent(api, crew, make_user, django_capture_on_commit_callbacks):
    alice, lea, tom = crew
    stranger = make_user('stranger')
    client = api.as_user(alice)

    res = client.post(TABLES, {'name': ' Les  colocs ', 'emoji': '🍝', 'member_ids': [stranger.id]}, format='json')
    assert res.status_code == 403 and res.json()['code'] == 'not_friends'
    assert not Table.objects.exists()  # création annulée avec l'invitation refusée

    with django_capture_on_commit_callbacks(execute=True):
        res = client.post(
            TABLES, {'name': ' Les  colocs ', 'emoji': '🍝', 'member_ids': [lea.id, tom.id]}, format='json'
        )
    assert res.status_code == 201
    body = res.json()
    table_id = body['id']
    assert body['name'] == 'Les colocs' and body['me']['role'] == 'owner'
    assert body['invitations_sent'] == {str(lea.id): 'invited', str(tom.id): 'invited'}
    assert (body['member_count'], body['invited_count']) == (1, 2)

    # Invitation in-app (registre) et consentement : l'invitée voit l'en-tête, pas la semaine.
    notif = Notification.objects.get(user=lea, notification_type='table_invitation')
    assert notif.message == 'alice t’invite à la tablée « Les colocs ».'
    listing = api.as_user(lea).get(TABLES).json()
    assert listing['tables'] == [] and [i['id'] for i in listing['invitations']] == [table_id]
    preview = api.as_user(lea).get(_detail(table_id)).json()
    assert preview['me']['status'] == 'invited' and preview['week'] is None and preview['ritual'] is None
    assert api.as_user(lea).get(f'{_detail(table_id)}?week=bad').status_code == 400
    assert api.as_user(stranger).get(_detail(table_id)).status_code == 404

    # Seul un membre accepté invite ; l'invitée ne peut pas inviter avant d'accepter.
    assert api.as_user(lea).post(f'{_detail(table_id)}invite/', {'user_ids': [alice.id]}).status_code == 404

    with django_capture_on_commit_callbacks(execute=True):
        assert api.as_user(lea).post(f'{_detail(table_id)}accept/').json()['me']['status'] == 'accepted'
    joined = Notification.objects.get(user=alice, notification_type='table_joined')
    assert joined.message == 'lea a rejoint « Les colocs ».'

    # Refus : invitation supprimée, sans notification à la créatrice.
    with django_capture_on_commit_callbacks(execute=True):
        assert api.as_user(tom).post(f'{_detail(table_id)}decline/').status_code == 200
    assert not TableMember.objects.filter(table_id=table_id, user=tom).exists()
    assert Notification.objects.filter(user=alice, notification_type='table_joined').count() == 1


def test_member_and_table_caps(api, crew, make_user, follow, settings):
    settings.TABLE_MAX_MEMBERS = 3
    settings.TABLES_MAX_PER_USER = 1
    alice, lea, tom = crew
    zoe = make_user('zoe')
    follow(alice, zoe)

    table_id = _table(api, alice, lea)
    res = api.as_user(alice).post(f'{_detail(table_id)}invite/', {'user_ids': [tom.id, zoe.id]}, format='json')
    assert res.status_code == 400 and res.json()['code'] == 'table_full'  # invitations en attente comprises
    assert api.as_user(alice).post(f'{_detail(table_id)}invite/', {'user_ids': [tom.id]}).status_code == 200
    res = api.as_user(alice).post(f'{_detail(table_id)}invite/', {'user_ids': [zoe.id]}, format='json')
    assert res.json()['code'] == 'table_full'

    # Une tablée max par personne (réglage) : créer ou accepter une 2e est refusé.
    assert api.as_user(alice).post(TABLES, {'name': 'Famille'}).json()['code'] == 'too_many_tables'
    other = _table(api, zoe, name='Famille')
    follow(zoe, lea)
    api.as_user(zoe).post(f'{_detail(other)}invite/', {'user_ids': [lea.id]}, format='json')
    res = api.as_user(lea).post(f'{_detail(other)}accept/')
    assert res.status_code == 400 and res.json()['code'] == 'too_many_tables'


def test_owner_manages_and_anyone_leaves(api, crew):
    alice, lea, tom = crew
    table_id = _table(api, alice, lea, tom)

    assert api.as_user(lea).patch(_detail(table_id), {'name': 'Nope'}).status_code == 403
    assert api.as_user(lea).delete(f'{_detail(table_id)}members/{tom.id}/').status_code == 403
    assert api.as_user(alice).patch(_detail(table_id), {'name': 'Coloc 2.0'}).json()['name'] == 'Coloc 2.0'
    assert api.as_user(alice).delete(f'{_detail(table_id)}members/{tom.id}/').json()['member_count'] == 2

    # La créatrice part : le rôle passe au membre le plus ancien ; dernier départ = tablée supprimée.
    assert api.as_user(alice).post(f'{_detail(table_id)}leave/').status_code == 200
    assert TableMember.objects.get(table_id=table_id, user=lea).role == 'owner'
    assert api.as_user(alice).get(_detail(table_id)).status_code == 404
    api.as_user(lea).post(f'{_detail(table_id)}leave/')
    assert not Table.objects.filter(id=table_id).exists()


def test_week_summary_is_collective_and_respects_visibility(api, crew, make_user, follow, make_meal_plan):
    alice, lea, tom = crew
    zoe = make_user('zoe')
    follow(alice, zoe)
    table_id = _table(api, alice, lea, tom, zoe)
    monday = current_week_start(alice)

    lea_meal = make_meal_plan(lea, day=monday, custom_label='Dahl', meal_type='recipe', confirmed=True)
    Post.objects.create(user=lea, meal_plan=lea_meal, label='Dahl', is_published=True)
    # tom a cuisiné, mais alice ne le suit pas : son post ne lui est pas visible (compté quand même).
    tom_meal = make_meal_plan(
        tom, day=monday + timedelta(days=1), custom_label='Le dahl', meal_type='recipe', confirmed=True
    )
    Post.objects.create(user=tom, meal_plan=tom_meal, label='Le dahl', is_published=True)
    make_meal_plan(alice, day=monday - timedelta(days=1), custom_label='Pizza', meal_type='recipe', confirmed=True)
    make_meal_plan(zoe, day=monday, custom_label='Gratin', meal_type='recipe')  # planifié, pas cuisiné

    body = api.as_user(alice).get(_detail(table_id)).json()
    week = body['week']
    assert (week['cooked_members'], week['member_count']) == (2, 4)
    assert week['headline'] == '2 sur 4 ont cuisiné cette semaine'
    assert week['dishes_count'] == 1  # « Dahl » = « Le dahl » (même plat), la pizza est d'avant
    members = {m['user']['id']: m for m in body['members']}
    assert members[lea.id]['latest_post']['title'] == 'Dahl'
    assert members[tom.id]['latest_post'] is None
    assert 'cooked' not in members[lea.id] and 'rank' not in members[lea.id]  # jamais de compteur individuel

    # Blocage : les deux restent membres, mais ne se voient plus dans la tablée.
    UserBlock.objects.create(blocker=lea, blocked=alice)
    body = api.as_user(alice).get(_detail(table_id)).json()
    assert lea.id not in {m['user']['id'] for m in body['members']}
    assert body['week']['member_count'] == 4  # compteur collectif anonyme inchangé
    assert TableMember.objects.filter(table_id=table_id, user=lea, status='accepted').exists()


def test_sunday_ritual_shares_basket_only_on_opt_in(api, crew, make_meal_plan):
    alice, lea, _tom = crew
    table_id = _table(api, alice, lea)
    week = current_week_start(lea)
    BasketItem.objects.create(user=lea, week_start=week, label='Pâtes au pesto')
    planned = make_meal_plan(lea, day=week, custom_label='Quiche', meal_type='recipe')
    BasketItem.objects.create(user=lea, week_start=week, label='Quiche', meal_plan=planned)  # datée : exclue

    ritual = api.as_user(alice).get(_detail(table_id)).json()['ritual']
    assert ritual['title'] == 'Qui cuisine quoi cette semaine ?'
    assert ritual['entries'] == [] and ritual['sharing_count'] == 0

    res = api.as_user(lea).patch(f'{_detail(table_id)}me/', {'share_basket': True}, format='json')
    assert res.json()['me']['share_basket'] is True
    ritual = api.as_user(alice).get(_detail(table_id)).json()['ritual']
    (entry,) = ritual['entries']
    assert entry['user']['id'] == lea.id and [i['title'] for i in entry['items']] == ['Pâtes au pesto']


def test_feed_filtered_by_table_respects_membership_and_visibility(api, crew, make_user, follow, make_meal_plan):
    """hom1 : `GET /api/posts/?table=<id>` = posts publiés des membres que le viewer peut voir, membres seulement."""
    alice, lea, tom = crew
    zoe, outsider = make_user('zoe'), make_user('outsider')
    follow(alice, zoe)
    follow(alice, outsider)
    table_id = _table(api, alice, lea, tom)
    # zoe invitée, pas encore membre : ses posts ne font pas partie du fil de la tablée.
    api.as_user(alice).post(f'{_detail(table_id)}invite/', {'user_ids': [zoe.id]}, format='json')

    mine = Post.objects.create(user=alice, label='Gratin', is_published=True)
    lea_post = Post.objects.create(user=lea, label='Dahl', is_published=True)
    Post.objects.create(user=lea, label='Brouillon', is_published=False)
    Post.objects.create(user=tom, label='Le dahl', is_published=True)  # alice ne suit pas tom, pas public
    tom_public = Post.objects.create(user=tom, label='Tarte', is_published=True, is_public=True)
    Post.objects.create(user=zoe, label='Soupe', is_published=True)
    Post.objects.create(user=outsider, label='Pizza', is_published=True)

    client = api.as_user(alice)
    res = client.get('/api/posts/', {'table': table_id, 'is_published': 'true', 'page_size': 20})
    assert res.status_code == 200, res.content
    ids = {p['id'] for p in res.json()['results']}
    assert ids == {mine.id, lea_post.id, tom_public.id}

    # `?user=` ne contourne pas le filtre de la tablée.
    res = client.get('/api/posts/', {'table': table_id, 'user': outsider.id})
    assert {p['id'] for p in res.json()['results']} == {mine.id, lea_post.id, tom_public.id}

    # Blocage : le post de lea disparaît du fil de la tablée d'alice.
    UserBlock.objects.create(blocker=lea, blocked=alice)
    res = client.get('/api/posts/', {'table': table_id})
    assert {p['id'] for p in res.json()['results']} == {mine.id, tom_public.id}

    # Invitée (pas acceptée), inconnue, id invalide, autre tablée : 404 `table_not_found`.
    for viewer, value in ((zoe, table_id), (outsider, table_id), (alice, 999999), (alice, 'abc')):
        res = api.as_user(viewer).get('/api/posts/', {'table': value})
        assert res.status_code == 404 and res.json()['code'] == 'table_not_found'
