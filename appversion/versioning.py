"""
Comparaison de versions « marketing » de l'app (1.0.9, 1.0.10, …).

Tolérant par construction : tout ce qui ne se parse pas renvoie None et l'appelant
doit traiter ce cas en fail-open (jamais bloquer un client sur une entrée bizarre).
"""

from __future__ import annotations

import re

# Préfixe numérique « 1 », « 1.0 », « 1.0.9 », « v1.2.3.4 » ; le reste (-beta, +42, (12)…) est ignoré.
_VERSION_RE = re.compile(r'^\s*[vV]?(\d{1,6}(?:\.\d{1,6}){0,4})(?![\d.]*\d)')
_MAX_LEN = 64


def parse_version(value) -> tuple[int, ...] | None:
    """« 1.0.9-beta » → (1, 0, 9) ; « 1.0 » et « 1.0.0 » → (1,) ; entrée invalide → None.

    Les zéros finaux sont retirés pour que 1.0 == 1.0.0 lors des comparaisons de tuples.
    """
    if not isinstance(value, str) or not value or len(value) > _MAX_LEN:
        return None
    match = _VERSION_RE.match(value)
    if not match:
        return None
    parts = [int(p) for p in match.group(1).split('.')]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_valid_version(value) -> bool:
    return parse_version(value) is not None
