"""Règle de copy permanente : jamais le mot « bande » dans un texte visible (notifications, partages)."""

import re
from pathlib import Path

import pytest

from accounts.services import invite_service
from notifications import registry
from notifications.registry import CATEGORIES, REGISTRY
from notifications.tests.test_notifications_registry import SAMPLE_CONTEXT

BANNED = re.compile(r'\bbande\b', re.IGNORECASE)

SAMPLE_CONTEXTS = (
    {**SAMPLE_CONTEXT, 'count': 1, 'new_member': True},
    {**SAMPLE_CONTEXT, 'actors': ['Léa'], 'count': 3, 'new_member': False},
)


def test_banned_word_regex_matches_only_the_word():
    assert BANNED.search('Rejoins ma Bande') and BANNED.search('« bande »')
    assert not BANNED.search('bandeau « Mettre à jour »')


@pytest.mark.parametrize('key', sorted(REGISTRY))
def test_registry_templates_never_say_bande(key):
    spec = REGISTRY[key]
    for text in (spec.title, spec.body):
        assert not BANNED.search(text), (key, text)
    for ctx in SAMPLE_CONTEXTS:
        title, body = spec.render(ctx)
        assert not BANNED.search(f'{title} {body}'), (key, title, body)


def test_registry_source_and_categories_never_say_bande():
    for category in CATEGORIES.values():
        assert not BANNED.search(f'{category.label} {category.description}')
    for module in (registry, invite_service):
        source = Path(module.__file__).read_text(encoding='utf-8')
        assert not BANNED.search(source), module.__name__


def test_invite_share_text_never_says_bande():
    text = invite_service.share_text('abcdefghjk')
    assert not BANNED.search(text)
    assert text.endswith('/i/abcdefghjk') and 'Rejoins-moi' in text
