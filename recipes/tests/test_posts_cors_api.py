"""CORS : le préflight de `POST /api/posts/cooked/` (Expo web) autorise les en-têtes envoyés par l'app."""

from django.test import Client

APP_HEADERS = ('idempotency-key', 'x-idempotency-key', 'x-app-version', 'x-app-platform', 'x-app-build')


def test_cooked_preflight_allows_idempotency_and_app_headers():
    resp = Client().options(
        '/api/posts/cooked/',
        HTTP_ORIGIN='http://localhost:8081',
        HTTP_ACCESS_CONTROL_REQUEST_METHOD='POST',
        HTTP_ACCESS_CONTROL_REQUEST_HEADERS='authorization,content-type,idempotency-key',
    )
    assert resp.status_code == 200
    allowed = {h.strip() for h in resp['Access-Control-Allow-Headers'].lower().split(',')}
    assert {'authorization', 'content-type', *APP_HEADERS} <= allowed
