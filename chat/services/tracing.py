"""Tracing OpenAI Agents SDK : désactivé par défaut (les traces contiennent des données utilisateur)."""

from agents import set_tracing_disabled
from django.conf import settings


def apply_tracing_setting() -> None:
    """Idempotent : le tracing n'est actif que si AGENT_TRACING_ENABLED est vrai."""
    set_tracing_disabled(not getattr(settings, 'AGENT_TRACING_ENABLED', False))
