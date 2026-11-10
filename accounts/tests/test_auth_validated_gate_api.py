"""Accès réservé aux comptes validés : un compte en liste d'attente n'utilise aucune fonctionnalité de l'app.

Les vues importent souvent `IsValidated as IsAuthenticated` : on vérifie la classe réelle, route par route,
pour qu'une nouvelle vue (ou une action avec ses propres `permission_classes`) ne retombe pas sur
`IsAuthenticated` / `AllowAny` par erreur.
"""

import pytest
from django.urls import URLResolver, get_resolver
from rest_framework.permissions import IsAdminUser

from accounts.permissions import HasAIFeature, IsValidated

pytestmark = pytest.mark.django_db

# Routes volontairement ouvertes aux comptes non validés (compte, inscription, liste d'attente, pages publiques).
NOT_VALIDATED_ALLOWED = {
    'api/app/version-check/',
    'api/auth/register/',
    'api/auth/login/',
    'api/auth/password-reset/request/',
    'api/auth/change-password/',
    'api/auth/account/delete/',
    'api/auth/profile/',
    'api/auth/billing/plans/',
    'api/auth/billing/handoff/',
    'api/auth/billing/handoff/resolve/',
    'api/auth/billing/status/',
    'api/auth/token/refresh/',
    'api/invites/redeem/',
    'api/invites/<str:code>/',
    'api/public/recipes/<int:recipe_id>/',
    'api/public/posts/<int:post_id>/',
    'api/public/users/<str:username>/',
}
GATED = (IsValidated, HasAIFeature, IsAdminUser)


def _routes(patterns, prefix=''):
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            yield from _routes(pattern.url_patterns, prefix + str(pattern.pattern))
        else:
            yield prefix + str(pattern.pattern), pattern.callback


def _gated(classes) -> bool:
    return any(isinstance(c, type) and issubclass(c, GATED) for c in classes)


def test_every_api_route_requires_a_validated_account_unless_allowlisted():
    offenders = set()
    for route, callback in _routes(get_resolver().url_patterns):
        view = getattr(callback, 'cls', None) or getattr(callback, 'view_class', None)
        if view is None or not route.startswith('api/') or route in NOT_VALIDATED_ALLOWED:
            continue
        if not _gated(getattr(view, 'permission_classes', [])):
            offenders.add(f'{route} ({view.__module__}.{view.__name__})')
        for action in (getattr(callback, 'actions', None) or {}).values():
            kwargs = getattr(getattr(view, action, None), 'kwargs', None) or {}
            if 'permission_classes' in kwargs and not _gated(kwargs['permission_classes']):
                offenders.add(f'{route} → {action}')
    assert not offenders, sorted(offenders)


def test_waitlisted_user_cannot_use_the_basket(api, make_user):
    client = api.as_user(make_user('attente', validated=False))
    for method, url, body in (
        ('get', '/api/basket/', None),
        ('post', '/api/basket/', {'label': 'Pâtes'}),
        ('post', '/api/basket/carry-over/', {}),
        ('post', '/api/basket/shopping-list/', {}),
    ):
        resp = getattr(client, method)(url, body, format='json')
        assert resp.status_code == 403, (method, url)
        assert resp.data['code'] == 'not_validated'
