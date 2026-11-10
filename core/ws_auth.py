"""Shared WebSocket JWT authentication."""

from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import close_old_connections
from rest_framework_simplejwt.tokens import AccessToken

User = get_user_model()


def get_active_user(pk):
    """Utilisateur actif pour cet id, sinon None (compte désactivé = jeton inutilisable)."""
    try:
        return User.objects.get(pk=pk, is_active=True)
    except User.DoesNotExist:
        return None


async def get_user_from_token(token: str):
    """
    Decode a JWT access token and return the associated user instance.
    Returns None if the token is invalid or the user does not exist / is inactive.
    """
    try:
        access = AccessToken(token)
        user_id = access.get('user_id')
        if not user_id:
            return None

        user = await sync_to_async(get_active_user)(user_id)
        close_old_connections()
        return user
    except Exception:
        return None
