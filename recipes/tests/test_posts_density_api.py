"""Densité sociale : Suggestions du feed (posts partagés seulement), arrêt à 3 amis actifs, onboarding social."""

import json
from datetime import date, timedelta
from io import StringIO

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.utils import timezone

from accounts.models import InviteRedemption, UserBlock
from recipes.models import Post, PostCookie

pytestmark = pytest.mark.django_db

SUGGESTIONS_URL = '/api/feed/suggestions/'
ONBOARDING_URL = '/api/onboarding/social/'


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()


def _post(author, label, *, public=False, published=True):
    return Post.objects.create(user=author, label=label, is_published=published, is_public=public)


def _cooked_today(make_meal_plan, *users):
    for u in users:
        make_meal_plan(u, day=date.today(), confirmed=True)


def test_suggestions_only_shared_posts_excluding_friends_and_blocked(api, user, make_user, follow):
    sam, eve, friend = make_user('sam'), make_user('eve'), make_user('friend')
    follow(user, friend)
    UserBlock.objects.create(blocker=eve, blocked=user)
    _post(sam, 'Privé')  # amis seulement
    _post(sam, 'Brouillon', public=True, published=False)
    _post(eve, 'Bloqué', public=True)
    _post(friend, 'Ami', public=True)  # déjà dans le feed principal
    old = _post(sam, 'Vieux', public=True)
    Post.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=40))

    # Partage explicite au moment du « J'ai cuisiné ».
    cooked = api.as_user(sam).post('/api/posts/cooked/', {'label': 'Dahl', 'is_public': True}, format='json')
    assert cooked.status_code == 201, cooked.data
    assert cooked.data['is_public'] is True

    resp = api.as_user(user).get(SUGGESTIONS_URL)
    assert resp.status_code == 200
    assert resp.data['active'] is True and resp.data['target'] == 3
    assert [p['display_title'] for p in resp.data['results']] == ['Dahl']
    assert resp.data['results'][0]['suggested'] is True

    # La carte s'ouvre et accepte un miam / « À mon tour » ; un post réservé aux amis reste fermé.
    public_id = resp.data['results'][0]['id']
    assert api.get(f'/api/posts/{public_id}/').status_code == 200
    assert api.post(f'/api/posts/{public_id}/my-turn/', format='json').status_code == 201
    private_id = Post.objects.get(label='Privé').id
    assert api.get(f'/api/posts/{private_id}/').status_code == 404
    blocked_id = Post.objects.get(label='Bloqué').id
    assert api.get(f'/api/posts/{blocked_id}/').status_code == 404

    # Le feed principal (app 1.0.9) est inchangé : pas de post de la communauté.
    feed = api.get('/api/posts/?is_published=true')
    assert {p['display_title'] for p in feed.data['results']} == {'Ami'}


def test_suggestions_stop_at_three_active_friends(api, user, make_user, follow, make_meal_plan):
    _post(make_user('sam'), 'Dahl', public=True)
    friends = [make_user(f'f{i}') for i in range(3)]
    for f in friends:
        follow(user, f)
    _cooked_today(make_meal_plan, *friends[:2])

    resp = api.as_user(user).get(SUGGESTIONS_URL)
    assert resp.data['active'] is True and resp.data['active_friends_count'] == 2 and resp.data['count'] == 1

    _cooked_today(make_meal_plan, friends[2])
    resp = api.get(SUGGESTIONS_URL)
    assert resp.data['active'] is False and resp.data['active_friends_count'] == 3 and resp.data['results'] == []


def test_onboarding_social_payload(api, user, make_user, follow, make_meal_plan):
    lea, tom, zoe, max_, blocked, me = (make_user(n) for n in ('lea', 'tom', 'zoe', 'max', 'blk', 'newbie'))
    # newbie est arrivé par le lien de lea (amis mutuels), et suit tom.
    InviteRedemption.objects.create(inviter=lea, invitee=me, at_signup=True, is_acquisition=True)
    for a, b in ((me, lea), (lea, me), (me, tom)):
        follow(a, b)
    # Amis d'amis : zoe (2 amis en commun) > max (1) ; blocked exclu ; alice invitée par newbie.
    for a, b in ((lea, zoe), (tom, zoe), (lea, max_), (lea, blocked)):
        follow(a, b)
    UserBlock.objects.create(blocker=blocked, blocked=me)
    InviteRedemption.objects.create(inviter=me, invitee=user)
    _cooked_today(make_meal_plan, lea)
    lea_post = _post(lea, 'Gratin')

    data = api.as_user(me).get(ONBOARDING_URL).data
    assert (data['friends_count'], data['active_friends_count'], data['target']) == (2, 1, 3)
    assert data['has_given_miam'] is False and data['feed_suggestions_active'] is True
    assert data['invited_by']['username'] == 'lea' and data['invite']['url'].endswith(data['invite']['code'])
    assert [p['id'] for p in data['first_miam_posts']] == [lea_post.id]
    people = [(p['username'], p['reason']) for p in data['suggested_people']]
    assert people[:3] == [('alice', 'you_invited'), ('zoe', 'mutual_friends'), ('max', 'mutual_friends')]
    usernames = {name for name, _ in people}
    assert not usernames & {'lea', 'tom', 'blk', 'newbie'}
    assert data['suggested_people'][1]['mutual_complices_count'] == 2

    PostCookie.objects.create(user=me, post=lea_post)
    data = api.get(ONBOARDING_URL).data
    assert data['has_given_miam'] is True and data['miams_given_count'] == 1 and data['first_miam_posts'] == []


def test_density_report(make_user, follow):
    from accounts.models import Follow

    now = timezone.now()
    dense, alone, a, b, c = (make_user(n) for n in ('dense', 'alone', 'a', 'b', 'c'))
    for u in (dense, alone):
        type(u).objects.filter(pk=u.pk).update(created_at=now - timedelta(days=10))
    for friend in (a, b, c):
        follow(dense, friend)
    follow(alone, a)
    PostCookie.objects.create(user=dense, post=_post(a, 'Soupe'))
    # Semaine 1 de dense / alone : J+1 ; le suivi de alone arrive après J7.
    Follow.objects.filter(follower=dense).update(created_at=now - timedelta(days=9))
    PostCookie.objects.filter(user=dense).update(created_at=now - timedelta(days=9))

    out = StringIO()
    call_command('density_report', '--days', '30', '--json', stdout=out)
    report = json.loads(out.getvalue())
    assert report['all'] == {'users': 2, 'pct_3_friends_d7': 50.0, 'pct_gave_miam_w1': 50.0, 'avg_miams_given_w1': 0.5}
    assert report['too_recent_for_d7'] == 3 and report['invited']['users'] == 0
