"""Contexte passé à l'agent pour chaque tour."""

from dataclasses import dataclass

from django.contrib.auth.models import AbstractBaseUser


@dataclass
class AgentContext:
    user: AbstractBaseUser
    conversation_id: int
