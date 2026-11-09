"""
Liens publics qui ouvrent l'app (.claude-work/specs/deep-links.md).

URLs canoniques sur le site (`PUBLIC_WEB_BASE_URL`, défaut https://tchikook.fr) : `/r/<id>` (recette),
`/p/<id>` (post), `/u/<username>` (profil) ; les invitations restent `/i/<code>`
(`accounts.services.invite_service.invite_url`). Le site rend un aperçu (Open Graph) à partir de
`/api/public/…` et propose d'ouvrir l'app (`tchikook://recipe/<id>`, `post/<id>`, `user/<id>`).
"""

from __future__ import annotations

from urllib.parse import quote

from django.conf import settings

DEFAULT_WEB_BASE_URL = 'https://tchikook.fr'


def web_base_url() -> str:
    base = getattr(settings, 'PUBLIC_WEB_BASE_URL', '') or getattr(settings, 'INVITE_BASE_URL', '')
    return (base or DEFAULT_WEB_BASE_URL).strip().rstrip('/')


def recipe_share_url(recipe_id) -> str:
    return f'{web_base_url()}/r/{int(recipe_id)}'


def post_share_url(post_id) -> str:
    return f'{web_base_url()}/p/{int(post_id)}'


def profile_share_url(username: str) -> str:
    return f'{web_base_url()}/u/{quote(str(username or ""), safe="")}'


def app_url(path: str) -> str:
    """Lien profond de l'app (`tchikook://recipe/42`), même schéma que les notifications."""
    scheme = getattr(settings, 'NOTIFICATIONS_DEEP_LINK_SCHEME', 'tchikook')
    return f'{scheme}://{path.lstrip("/")}'


def public_display_name(user) -> str:
    """Nom affichable publiquement : prénom s'il est renseigné, sinon pseudo. Jamais l'email."""
    first = (getattr(user, 'first_name', '') or '').strip()
    return first or (getattr(user, 'username', '') or '').strip()
