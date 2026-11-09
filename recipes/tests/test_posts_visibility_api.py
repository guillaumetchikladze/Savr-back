"""
Visibilité d'un post appliquée à chaque action ciblant un post (`recipes/services/post_visibility.py`) :
invisible → 404 (jamais 403), commentaires réservés au cercle des amis, écritures réservées à l'auteur.
"""

from datetime import date

import pytest

from accounts.models import Notification, UserBlock
from recipes.models import MealInvitation, Post, PostComment, PostCookie, PostReport, PostRepost

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _no_celery(celery_delay):
    return celery_delay


@pytest.fixture
def author(make_user):
    return make_user('lea')


@pytest.fixture
def post(author, make_meal_plan):
    mp = make_meal_plan(author, day=date(2026, 11, 2), confirmed=True)
    return Post.objects.create(user=author, meal_plan=mp, label='Quiche', is_published=True)


def _url(post, suffix=''):
    return f'/api/posts/{post.id}/{suffix}'


def _actions(client, post, comment):
    """Statut de chaque action « lecture / interaction » sur le post."""
    return {
        'retrieve': client.get(_url(post)).status_code,
        'miam': client.post(_url(post, 'send_cookie/')).status_code,
        'miam_list': client.get(_url(post, 'cookie-users/')).status_code,
        'unmiam': client.delete(_url(post, 'remove_cookie/')).status_code,
        'my_turn': client.post(_url(post, 'my-turn/'), format='json').status_code,
        'report': client.post(_url(post, 'report/'), {'reason': 'x'}, format='json').status_code,
        'comments_list': client.get(_url(post, 'comments/')).status_code,
        'comment': client.post(_url(post, 'comments/'), {'text': 'Miam'}, format='json').status_code,
        'comment_like': client.post(_url(post, f'comments/{comment.id}/like/')).status_code,
        'repost': client.post(_url(post, 'repost/')).status_code,
    }


ALL_404 = dict.fromkeys(
    (
        'retrieve',
        'miam',
        'miam_list',
        'unmiam',
        'my_turn',
        'report',
        'comments_list',
        'comment',
        'comment_like',
        'repost',
    ),
    404,
)


def _assert_no_trace(user, post):
    assert not PostCookie.objects.filter(user=user, post=post).exists()
    assert not PostComment.objects.filter(user=user, post=post).exists()
    assert not PostReport.objects.filter(reporter=user, post=post).exists()
    assert not PostRepost.objects.filter(user=user, original_post=post).exists()


def test_stranger_and_blocked_get_404_on_every_action_of_friends_only_post(api, author, post, make_user, follow):
    comment = PostComment.objects.create(post=post, user=author, text='Le mien')
    stranger, blocked, blocker = make_user('stranger'), make_user('blocked'), make_user('blocker')
    follow(stranger, make_user('other'))  # suivre quelqu'un d'autre ne suffit pas
    follow(author, stranger)  # être suivi par l'auteur ne suffit pas non plus
    # Blocage dans les deux sens, même avec un Follow resté en base.
    follow(blocked, author)
    UserBlock.objects.create(blocker=author, blocked=blocked)
    follow(blocker, author)
    UserBlock.objects.create(blocker=blocker, blocked=author)

    for viewer in (stranger, blocked, blocker):
        assert _actions(api.as_user(viewer), post, comment) == ALL_404, viewer.username
        _assert_no_trace(viewer, post)
    assert not Notification.objects.filter(user=author).exists()


def test_friend_can_view_miam_comment_and_my_turn(api, author, post, make_user, follow):
    comment = PostComment.objects.create(post=post, user=author, text='Le mien')
    friend = make_user('friend')
    follow(friend, author)
    client = api.as_user(friend)

    assert client.post(_url(post, 'send_cookie/')).status_code == 201
    assert client.post(_url(post, 'comments/'), {'text': 'Trop bon'}, format='json').status_code == 201
    assert client.post(_url(post, f'comments/{comment.id}/like/')).status_code == 200
    assert [c['text'] for c in client.get(_url(post, 'comments/')).data] == ['Le mien', 'Trop bon']
    assert client.get(_url(post, 'cookie-users/')).data['count'] == 1
    assert client.post(_url(post, 'my-turn/'), format='json').status_code == 201
    assert client.post(_url(post, 'report/'), {'reason': 'x'}, format='json').status_code == 200
    # Pas invité au repas : visible mais repost refusé (403, l'existence n'est pas un secret ici).
    assert client.post(_url(post, 'repost/')).status_code == 403


def test_public_post_opens_to_strangers_for_miam_and_my_turn_but_not_comments(api, author, post, make_user):
    Post.objects.filter(pk=post.pk).update(is_public=True)
    comment = PostComment.objects.create(post=post, user=author, text='Le mien')
    stranger, blocked = make_user('stranger'), make_user('blocked')
    UserBlock.objects.create(blocker=blocked, blocked=author)

    assert _actions(api.as_user(stranger), post, comment) == {
        'retrieve': 200,
        'miam': 201,
        'miam_list': 200,
        'unmiam': 200,
        'my_turn': 201,
        'report': 200,
        'comments_list': 404,
        'comment': 404,
        'comment_like': 404,
        'repost': 403,
    }
    assert _actions(api.as_user(blocked), post, comment) == ALL_404


def test_inactive_author_hides_post_even_from_friends(api, author, post, make_user, follow):
    friend = make_user('friend')
    follow(friend, author)
    author.is_active = False
    author.save(update_fields=['is_active'])
    comment = PostComment.objects.create(post=post, user=friend, text='x')

    assert _actions(api.as_user(friend), post, comment) == ALL_404


def test_meal_guest_sees_reposts_and_comments_and_friends_of_reposter_only_view(api, author, post, make_user, follow):
    guest, guest_friend = make_user('guest'), make_user('guest_friend')
    MealInvitation.objects.create(inviter=author, invitee=guest, meal_plan=post.meal_plan, status='accepted')
    follow(guest_friend, guest)
    guest_client = api.as_user(guest)

    assert guest_client.get(_url(post)).status_code == 200
    assert guest_client.post(_url(post, 'comments/'), {'text': 'Merci'}, format='json').status_code == 201
    assert guest_client.post(_url(post, 'repost/')).status_code == 201

    # Le post co-cuisiné apparaît sur le profil du guest : ses amis l'ouvrent et le miament, sans le commenter.
    friend_client = api.as_user(guest_friend)
    profile = friend_client.get('/api/posts/', {'is_published': 'true', 'user': guest.id})
    assert [p['id'] for p in profile.data['results']] == [post.id]
    assert friend_client.get(_url(post)).status_code == 200
    assert friend_client.post(_url(post, 'send_cookie/')).status_code == 201
    assert friend_client.get(_url(post, 'comments/')).status_code == 404

    # Hôte bloqué par l'ami du guest : disparaît du profil et du détail.
    UserBlock.objects.create(blocker=guest_friend, blocked=author)
    profile = friend_client.get('/api/posts/', {'is_published': 'true', 'user': guest.id})
    assert profile.data['results'] == []
    assert friend_client.get(_url(post)).status_code == 404


def test_comments_hide_blocked_users_and_mentions_skip_outsiders(api, author, post, make_user, follow):
    friend, troll, outsider = make_user('friend'), make_user('troll'), make_user('outsider')
    follow(friend, author)
    follow(troll, author)
    PostComment.objects.create(post=post, user=troll, text='Bof')
    UserBlock.objects.create(blocker=friend, blocked=troll)
    client = api.as_user(friend)

    resp = client.post(_url(post, 'comments/'), {'text': 'Top @outsider @lea'}, format='json')

    assert resp.status_code == 201
    assert [c['text'] for c in client.get(_url(post, 'comments/')).data] == ['Top @outsider @lea']
    troll_comment = PostComment.objects.get(user=troll)
    assert client.post(_url(post, f'comments/{troll_comment.id}/like/')).status_code == 404
    # Mention d'un compte qui ne voit pas le post : aucune notification.
    assert not Notification.objects.filter(user=outsider).exists()
    assert Notification.objects.filter(user=author, notification_type='post_comment').exists()


def test_writes_stay_owner_only_whatever_the_query_params(api, author, post, make_user, follow):
    friend = make_user('friend')
    follow(friend, author)
    client = api.as_user(friend)
    params = f'?is_published=true&user={author.id}'

    assert client.delete(_url(post) + params).status_code == 404
    assert client.patch(_url(post) + params, {'comment': 'pwned'}, format='json').status_code == 404
    assert client.patch(_url(post, 'update-content/') + params, {'comment': 'x'}, format='json').status_code == 404
    assert client.post(_url(post, 'publish/') + params).status_code == 404
    assert client.post(_url(post, 'photos/'), {'image_paths': ['posts/x.jpg']}, format='json').status_code == 404
    post.refresh_from_db()
    assert post.comment != 'pwned'

    # Un `?user=` invalide ne retombe jamais sur tous les posts publiés.
    stranger_post = Post.objects.create(user=make_user('stranger'), label='Secret', is_published=True)
    ids = {p['id'] for p in client.get('/api/posts/', {'is_published': 'true', 'user': 'abc'}).data['results']}
    assert stranger_post.id not in ids and not ids
