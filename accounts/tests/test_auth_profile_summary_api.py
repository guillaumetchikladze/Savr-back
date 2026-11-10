"""
Profil unifié (prf3) : `GET /api/auth/users/<id>/` porte `cooked_count` (tout ce que la personne a cuisiné, un
nombre seulement) et `shared_tables` (tablées en commun, seulement celles dont le lecteur est membre).
"""

from datetime import date

import pytest

from accounts.models import UserBlock
from recipes.models import Post
from tables.models import Table, TableMember

pytestmark = pytest.mark.django_db


def _url(u):
    return f'/api/auth/users/{u.id}/'


@pytest.fixture
def lea(make_user):
    return make_user('lea')


@pytest.fixture
def lea_cooked(lea, make_meal_plan, make_recipe):
    """Léa : 1 post publié, 1 post « rien que pour moi », 1 repas cuisiné sans post, 1 repas prévu (pas cuisiné)."""
    quiche = make_recipe(lea, 'Quiche')
    mp = make_meal_plan(lea, day=date(2026, 11, 9), recipes=[quiche], confirmed=True)
    Post.objects.create(user=lea, meal_plan=mp, is_published=True, comment='Trop bon')
    mp = make_meal_plan(lea, day=date(2026, 11, 7), custom_label='Soupe secrète', confirmed=True)
    Post.objects.create(user=lea, meal_plan=mp, label='Soupe secrète', is_published=False, comment='Note intime')
    make_meal_plan(lea, day=date(2026, 11, 5), custom_label='Riz aux aubergines', confirmed=True)
    make_meal_plan(lea, day=date(2026, 11, 20), custom_label='Prévu')


def _table(name, *members, status=TableMember.STATUS_ACCEPTED, emoji='🍝'):
    table = Table.objects.create(name=name, emoji=emoji, color='#FF6B6B')
    for i, (user, st) in enumerate(members):
        TableMember.objects.create(table=table, user=user, status=st or status, role='owner' if i == 0 else 'member')
    return table


def test_cooked_count_is_everything_cooked_for_anyone_but_only_a_number(api, make_user, follow, lea, lea_cooked):
    friend, stranger = make_user('friend'), make_user('stranger')
    follow(friend, lea)

    own = api.as_user(lea).get('/api/carnet/me/').json()['count']
    assert own == 3  # même nombre que son propre carnet (privé et repas sans post compris)

    for viewer in (friend, stranger):
        data = api.as_user(viewer).get(_url(lea)).json()
        assert data['cooked_count'] == own
        flat = str(data)
        assert 'Soupe secrète' not in flat and 'Note intime' not in flat and 'Riz aux aubergines' not in flat

    # Le contenu, lui, suit toujours la visibilité : l'ami ne voit que le post publié, l'inconnu rien.
    assert api.as_user(friend).get(f'/api/carnet/users/{lea.id}/').json()['count'] == 1
    assert api.as_user(stranger).get(f'/api/carnet/users/{lea.id}/').status_code == 403


def test_shared_tables_only_those_the_reader_belongs_to(api, make_user, follow, lea):
    me, tom = make_user('me'), make_user('tom')
    acc, inv = TableMember.STATUS_ACCEPTED, TableMember.STATUS_INVITED
    colocs = _table('Les colocs', (me, acc), (lea, acc), (tom, acc))
    _table('Famille de Léa', (lea, acc), (tom, acc))  # je n'en suis pas
    _table('Invitée seulement', (me, acc), (lea, inv))  # Léa n'a pas accepté
    _table('Moi invité', (lea, acc), (me, inv))  # je n'ai pas accepté
    follow(me, lea)

    data = api.as_user(me).get(_url(lea)).json()
    assert data['shared_tables'] == [{'id': colocs.id, 'name': 'Les colocs', 'emoji': '🍝', 'color': '#FF6B6B'}]
    assert 'members' not in str(data['shared_tables'])

    # Visible même sans suivi (on partage la tablée) ; jamais pour soi-même.
    assert [t['id'] for t in api.as_user(tom).get(_url(lea)).json()['shared_tables']] == [colocs.id] + [
        t.id for t in Table.objects.filter(name='Famille de Léa')
    ]
    assert api.as_user(lea).get(_url(lea)).json()['shared_tables'] == []


def test_blocked_profile_exposes_nothing(api, make_user, lea, lea_cooked):
    me = make_user('me')
    _table('Les colocs', (me, TableMember.STATUS_ACCEPTED), (lea, TableMember.STATUS_ACCEPTED))
    UserBlock.objects.create(blocker=lea, blocked=me)
    res = api.as_user(me).get(_url(lea))
    assert res.status_code == 403
    assert 'cooked_count' not in res.json() and 'shared_tables' not in res.json()
