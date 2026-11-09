"""
Nombre de requêtes SQL constant (pas de N+1) pour les listes de posts : feed, profil, Suggestions, détail.

Jeu de données « riche » (`FeedWorld`) : plusieurs auteurs (dont un suivi mutuel), co-cuisiniers (reposts) avec
demandes de suivi en attente dans les deux sens, invitations au repas (le viewer peut reposter), photos,
recettes, miams, commentaires, « À mon tour », `inspired_by`, ardoises calculées (mesure à chaud).

`measure()` attribue chaque requête à la ligne du code projet qui l'a déclenchée : en cas d'échec, le message
liste les origines dont le nombre a grandi avec la taille de la page.
"""

from __future__ import annotations

import os
import traceback
import uuid
from collections import Counter
from datetime import date, timedelta

import pytest
from django.core.cache import cache
from django.db import connection

from accounts.models import Follow, FollowRequest
from recipes.models import BasketItem, MealInvitation, Post, PostComment, PostCookie, PostPhoto, PostRepost

pytestmark = pytest.mark.django_db

FEED_URL = '/api/posts/?is_published=true&page_size=50'
SUGGESTIONS_URL = '/api/feed/suggestions/?page_size=20'
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


# ---------------------------------------------------------------------------
# Mesure : requêtes + origine (1re ligne du code projet hors tests dans la pile)
# ---------------------------------------------------------------------------
def _origin() -> str:
    for frame in reversed(traceback.extract_stack()[:-2]):
        path = frame.filename
        if not path.startswith(_PROJECT_ROOT) or '/tests/' in path or '/.venv/' in path or '/venv/' in path:
            continue
        if path.endswith('conftest.py'):
            continue
        return f'{os.path.relpath(path, _PROJECT_ROOT)}:{frame.lineno} {frame.name}'
    return '<django>'


def measure(client, url):
    """GET à chaud (ardoises déjà calculées) : (réponse, Counter origine → nombre de requêtes)."""
    assert client.get(url).status_code == 200  # échauffement : instantanés « L'ardoise » enregistrés
    origins: Counter = Counter()

    def wrapper(execute, sql, params, many, context):
        origins[_origin()] += 1
        return execute(sql, params, many, context)

    with connection.execute_wrapper(wrapper):
        resp = client.get(url)
    assert resp.status_code == 200, resp.content[:500]
    if os.environ.get('FEED_QUERIES_DEBUG'):
        print(f'\n{url}: {sum(origins.values())} requêtes')
        for origin, n in origins.most_common():
            print(f'  {n:4d}  {origin}')
    return resp, origins


def assert_constant(small: Counter, large: Counter, label: str):
    grown = {o: (small.get(o, 0), n) for o, n in large.items() if n > small.get(o, 0)}
    assert sum(large.values()) <= sum(small.values()), (
        f'{label} : {sum(small.values())} → {sum(large.values())} requêtes ; origines qui grandissent : {grown}'
    )


# ---------------------------------------------------------------------------
# Jeu de données
# ---------------------------------------------------------------------------
class FeedWorld:
    """Posts « riches » ajoutés par lots (`add(n)`) pour un viewer donné."""

    def __init__(self, viewer, authors, *, make_user, make_recipe, make_meal_plan, public=False):
        self.viewer = viewer
        self.authors = authors
        self.public = public
        self.make_recipe = make_recipe
        self.make_meal_plan = make_meal_plan
        self.posts: list[Post] = []
        self.fan = make_user()
        # Co-cuisiniers : le viewer a demandé à suivre g1 ; g2 a demandé à suivre le viewer ; g3 est suivi.
        self.g1, self.g2, self.g3 = make_user(), make_user(), make_user()
        FollowRequest.objects.create(requester=viewer, target=self.g1, status='pending')
        FollowRequest.objects.create(requester=self.g2, target=viewer, status='pending')
        Follow.objects.create(follower=viewer, following=self.g3)
        Follow.objects.create(follower=self.g3, following=viewer)

    def add(self, n: int) -> list[Post]:
        new = []
        for _ in range(n):
            i = len(self.posts)
            author = self.authors[i % len(self.authors)]
            recipe = self.make_recipe(author, f'Plat {i}', ingredients=['tomate', f'ingr{i % 3}'])
            mp = self.make_meal_plan(
                author,
                day=date.today() - timedelta(days=i % 5),
                meal_time='other',
                slot_key=uuid.uuid4().hex,
                custom_label=f'Repas {i}' if i % 2 else '',
                recipes=[recipe],
                confirmed=True,
            )
            batch = mp.meal_plan_recipe_batches.get().recipe_batch
            post = Post.objects.create(
                user=author,
                meal_plan=mp,
                recipe_batch=batch if i % 3 else None,
                label='' if i % 2 else f'Label {i}',
                comment='Bon',
                is_published=True,
                is_public=self.public,
                inspired_by_post=self.posts[0] if self.posts and i % 2 else None,
            )
            PostPhoto.objects.create(post=post, photo_type='at_meal_time', image_path=f'posts/p{i}.jpg', order=0)
            PostPhoto.objects.create(
                post=post, photo_type='after_cooking', image_path=f'posts/q{i}.jpg', order=1, recipe_batch=batch
            )
            PostCookie.objects.create(post=post, user=self.fan)
            if i % 2 == 0:
                PostCookie.objects.create(post=post, user=self.viewer)
            PostComment.objects.create(post=post, user=self.fan, text='Miam')
            # Invités / co-cuisiniers
            for guest, cond in ((self.g1, i % 2 == 0), (self.g2, i % 4 == 1), (self.g3, i % 3 == 0)):
                if cond:
                    MealInvitation.objects.create(inviter=author, invitee=guest, meal_plan=mp, status='accepted')
                    PostRepost.objects.create(user=guest, original_post=post, host_user=author)
            if author.id != self.viewer.id:
                if i % 5 == 0:
                    MealInvitation.objects.create(inviter=author, invitee=self.viewer, meal_plan=mp, status='accepted')
                    PostRepost.objects.create(user=self.viewer, original_post=post, host_user=author)
                elif i % 3 == 0:
                    MealInvitation.objects.create(inviter=author, invitee=self.viewer, meal_plan=mp, status='pending')
            # « À mon tour »
            week = date.today() - timedelta(days=date.today().weekday())
            BasketItem.objects.create(user=self.fan, week_start=week, label='x', inspired_by_post=post)
            if i % 2:
                BasketItem.objects.create(user=self.viewer, week_start=week, label='x', inspired_by_post=post)
            self.posts.append(post)
            new.append(post)
        return new


@pytest.fixture
def world_factory(make_user, make_recipe, make_meal_plan):
    def _make(viewer, authors, **kw):
        return FeedWorld(
            viewer, authors, make_user=make_user, make_recipe=make_recipe, make_meal_plan=make_meal_plan, **kw
        )

    return _make


# ---------------------------------------------------------------------------
# Tests de non-régression : 2 posts vs 20 posts → même nombre de requêtes
# ---------------------------------------------------------------------------
def test_feed_query_count_is_constant(api, user, make_user, follow, world_factory):
    authors = [make_user() for _ in range(4)]
    for a in authors:
        follow(user, a)
    follow(authors[0], user)  # suivi mutuel
    world = world_factory(user, [*authors, user])
    client = api.as_user(user)

    world.add(2)
    resp, small = measure(client, FEED_URL)
    assert len(resp.data['results']) == 2
    world.add(18)
    resp, large = measure(client, FEED_URL)
    assert len(resp.data['results']) == 20
    assert_constant(small, large, 'feed')
    assert sum(large.values()) <= 16, large


def test_profile_posts_query_count_is_constant(api, user, make_user, follow, world_factory):
    author, other = make_user(), make_user()
    follow(user, author)
    follow(user, other)
    world = world_factory(user, [author])
    other_world = world_factory(user, [other])
    client = api.as_user(user)
    url = f'/api/posts/?is_published=true&user={author.id}&page_size=50'

    world.add(2)
    resp, small = measure(client, url)
    assert len(resp.data['results']) == 2
    world.add(15)
    # Co-cuisinés : posts d'un autre auteur repostés par le profil.
    for post in other_world.add(3):
        MealInvitation.objects.create(inviter=other, invitee=author, meal_plan=post.meal_plan, status='accepted')
        PostRepost.objects.create(user=author, original_post=post, host_user=other)
    resp, large = measure(client, url)
    assert len(resp.data['results']) == 20
    assert_constant(small, large, 'profil')
    assert sum(large.values()) <= 19, large


def test_suggestions_query_count_is_constant(api, user, make_user, world_factory):
    strangers = [make_user() for _ in range(3)]
    world = world_factory(user, strangers, public=True)
    client = api.as_user(user)

    world.add(2)
    resp, small = measure(client, SUGGESTIONS_URL)
    assert resp.data['active'] is True and len(resp.data['results']) == 2
    world.add(18)
    resp, large = measure(client, SUGGESTIONS_URL)
    assert len(resp.data['results']) == 20
    assert_constant(small, large, 'suggestions')
    assert sum(large.values()) <= 20, large


def test_onboarding_first_miam_posts_query_count_is_constant(api, user, make_user, follow, world_factory):
    authors = [make_user() for _ in range(2)]
    for a in authors:
        follow(user, a)
    world = world_factory(user, authors)
    client = api.as_user(user)

    world.add(2)  # le 1er porte déjà le miam du viewer : 1 post « premier miam »
    resp, small = measure(client, '/api/onboarding/social/')
    assert len(resp.data['first_miam_posts']) == 1
    world.add(6)
    resp, large = measure(client, '/api/onboarding/social/')
    assert len(resp.data['first_miam_posts']) == 3
    assert_constant(small, large, 'onboarding')


def test_post_detail_query_count_does_not_grow_with_co_cookers(api, user, make_user, follow, world_factory):
    author = make_user()
    follow(user, author)
    world = world_factory(user, [author])
    (post,) = world.add(1)
    client = api.as_user(user)
    url = f'/api/posts/{post.id}/'

    _, small = measure(client, url)
    for _ in range(5):
        guest = make_user()
        MealInvitation.objects.create(inviter=author, invitee=guest, meal_plan=post.meal_plan, status='accepted')
        PostRepost.objects.create(user=guest, original_post=post, host_user=author)
        FollowRequest.objects.create(requester=user, target=guest, status='pending')
    resp, large = measure(client, url)
    assert len(resp.data['co_cooked_users']) >= 6
    assert_constant(small, large, 'détail')
