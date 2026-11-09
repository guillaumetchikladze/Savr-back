"""
Fixtures partagées + garde-fous réseau pour toute la suite de tests.

Principes :
- aucun test ne touche le réseau (socket guard autouse, seule la base de test est joignable) ;
- les services externes (LLM, embeddings, S3, Expo, MS Graph, Apify) sont neutralisés
  par settings_test (clés vides) ; un test qui a besoin d'une réponse LLM/scraper la mocke
  explicitement au niveau du module appelant ;
- Celery n'exécute rien implicitement (broker mémoire) ; fixture `celery_delay` pour capturer ;
- fixtures minimales : `make_user`, `user`, `api`, `make_recipe`, `make_meal_plan`,
  `make_shopping_list`, `follow`.
"""

from __future__ import annotations

import itertools
import socket
from datetime import date
from unittest import mock

import pytest
from django.conf import settings
from django.utils import timezone

# ---------------------------------------------------------------------------
# Marqueurs de domaine automatiques (selon le fichier) → `make test-domain d=...`
# ---------------------------------------------------------------------------
_DOMAIN_BY_FILE = {
    'test_auth': 'auth',
    'test_mealplan': 'mealplan',
    'test_meal_plan': 'mealplan',
    'test_posts': 'posts',
    'test_shopping': 'shopping',
    'test_dietary': 'dietary',
    'test_chat': 'chat',
    'test_context_refs': 'chat',
    'test_session_context': 'chat',
    'test_gemini_tool_compat': 'chat',
    'test_recipe_import': 'imports',
    'test_imports': 'imports',
    'test_recipe': 'recipes',
    'test_collections': 'recipes',
}


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    for item in items:
        name = item.path.stem
        for prefix, marker in _DOMAIN_BY_FILE.items():
            if name.startswith(prefix):
                item.add_marker(getattr(pytest.mark, marker))
                break
        if item.path.parent.name == 'tests' and item.path.parent.parent.name == 'chat':
            item.add_marker(pytest.mark.chat)


# ---------------------------------------------------------------------------
# Garde-fou réseau : seules la base de test (et les sockets locales) sont joignables.
# Toute tentative sortante lève NetworkBlockedError (sous-classe de ConnectionRefusedError,
# donc traitée comme « service indisponible » par le code) ET fait échouer le test.
# ---------------------------------------------------------------------------
_ALLOWED_HOSTS = {'localhost', '127.0.0.1', '::1', '0.0.0.0'}
_db_host = settings.DATABASES['default'].get('HOST') or ''
if _db_host:
    _ALLOWED_HOSTS.add(_db_host)

_blocked_attempts: list[str] = []


class NetworkBlockedError(ConnectionRefusedError):
    pass


_real_connect = socket.socket.connect
_real_getaddrinfo = socket.getaddrinfo


def _is_allowed(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode()
    return host is None or (isinstance(host, str) and (host in _ALLOWED_HOSTS or host.startswith('/')))


def _guarded_connect(self, address, *args, **kwargs):
    if self.family == getattr(socket, 'AF_UNIX', object()) or _is_allowed(
        address[0] if isinstance(address, tuple) else address
    ):
        return _real_connect(self, address, *args, **kwargs)
    _blocked_attempts.append(repr(address))
    raise NetworkBlockedError(f'Accès réseau interdit pendant les tests ({address!r})')


def _guarded_getaddrinfo(host, *args, **kwargs):
    if _is_allowed(host):
        return _real_getaddrinfo(host, *args, **kwargs)
    _blocked_attempts.append(repr(host))
    raise NetworkBlockedError(f'Résolution DNS interdite pendant les tests ({host!r})')


@pytest.fixture(autouse=True, scope='session')
def _block_network():
    socket.socket.connect = _guarded_connect
    socket.getaddrinfo = _guarded_getaddrinfo
    yield
    socket.socket.connect = _real_connect
    socket.getaddrinfo = _real_getaddrinfo


@pytest.fixture(autouse=True)
def _fail_on_network_attempt():
    _blocked_attempts.clear()
    yield
    if _blocked_attempts:
        attempts = ', '.join(sorted(set(_blocked_attempts)))
        _blocked_attempts.clear()
        pytest.fail(f'Le test a tenté un accès réseau (à mocker) : {attempts}')


@pytest.fixture
def celery_delay():
    """Capture les `.delay()` / `.apply_async()` Celery sans broker : `celery_delay.calls`."""
    from celery.app.task import Task

    calls = []

    def _fake_apply_async(self, args=None, kwargs=None, **options):
        calls.append((self.name, tuple(args or ()), dict(kwargs or {})))
        result = mock.MagicMock()
        result.id = f'fake-task-{len(calls)}'
        return result

    with mock.patch.object(Task, 'apply_async', _fake_apply_async):
        holder = mock.MagicMock()
        holder.calls = calls
        yield holder


# ---------------------------------------------------------------------------
# Factories minimales
# ---------------------------------------------------------------------------
_seq = itertools.count(1)


@pytest.fixture
def make_user(db):
    from django.contrib.auth import get_user_model

    User = get_user_model()

    def _make(username: str | None = None, *, validated: bool = True, **extra):
        n = next(_seq)
        username = username or f'user{n}'
        extra.setdefault('email', f'{username}@example.test')
        if validated:
            extra.setdefault('validated_at', timezone.now())
        password = extra.pop('password', 'pass-1234-test')
        return User.objects.create_user(username=username, password=password, **extra)

    return _make


@pytest.fixture
def user(make_user):
    return make_user('alice')


@pytest.fixture
def api():
    """APIClient DRF ; `api.as_user(u)` force l'authentification."""
    from rest_framework.test import APIClient

    client = APIClient()

    def as_user(u):
        client.force_authenticate(user=u)
        return client

    client.as_user = as_user
    return client


@pytest.fixture
def make_recipe(db):
    from recipes.models import Ingredient, Recipe, RecipeIngredient

    def _make(created_by, title: str | None = None, *, ingredients=(), **extra):
        n = next(_seq)
        extra.setdefault('prep_time', 10)
        extra.setdefault('cook_time', 10)
        extra.setdefault('servings', 2)
        extra.setdefault('is_public', True)
        recipe = Recipe.objects.create(title=title or f'Recette {n}', created_by=created_by, **extra)
        for name in ingredients:
            ing, _ = Ingredient.objects.get_or_create(name=name)
            RecipeIngredient.objects.create(recipe=recipe, ingredient=ing, quantity=1, unit='')
        return recipe

    return _make


@pytest.fixture
def make_meal_plan(db):
    from recipes.models import MealPlan, MealPlanRecipeBatch, RecipeBatch

    def _make(owner, *, day: date | None = None, meal_time: str = 'lunch', recipes=(), **extra):
        extra.setdefault('meal_type', 'recipe' if recipes else 'unknown')
        mp = MealPlan.objects.create(user=owner, date=day or date.today(), meal_time=meal_time, **extra)
        for order, recipe in enumerate(recipes):
            batch = RecipeBatch.objects.create(recipe=recipe, created_by=owner)
            MealPlanRecipeBatch.objects.create(meal_plan=mp, recipe_batch=batch, order=order)
        return mp

    return _make


@pytest.fixture
def make_shopping_list(db):
    from recipes.models import ShoppingList, ShoppingListMember

    def _make(owner, *, name: str = 'Courses', collaborators=()):
        sl = ShoppingList.objects.create(name=name)
        ShoppingListMember.objects.create(shopping_list=sl, user=owner, role='owner')
        for u in collaborators:
            ShoppingListMember.objects.create(shopping_list=sl, user=u, role='collaborator')
        return sl

    return _make


@pytest.fixture
def follow(db):
    """`follow(a, b)` : a suit b (Follow approuvé)."""
    from accounts.models import Follow

    def _follow(follower, following):
        return Follow.objects.get_or_create(follower=follower, following=following)[0]

    return _follow
