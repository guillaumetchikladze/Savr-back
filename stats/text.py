"""Normalisation de texte partagée (noms d'ingrédients, tags de cuisine)."""

from __future__ import annotations

import re
import unicodedata

_NON_WORD = re.compile(r'[^a-z0-9]+')


def fold(text: str | None) -> str:
    """minuscules, sans accents, ponctuation → espace simple (« Pois-chiches » → « pois chiches »)."""
    if not text:
        return ''
    text = unicodedata.normalize('NFKD', str(text).replace('œ', 'oe').replace('Œ', 'oe').replace('æ', 'ae'))
    text = ''.join(c for c in text if not unicodedata.combining(c)).lower()
    return _NON_WORD.sub(' ', text).strip()
