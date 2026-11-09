"""
Matching texte « mot-clé de préférence → nom d'ingrédient », partagé par tous les chemins
du filtrage alimentaire (allergies, régimes, goûts ; filtre SQL et détail des raisons).

Règles (volontairement simples, pas de NLP) :

- **Mots entiers** : un mot-clé ne matche qu'aux frontières de mot (``lait`` ≠ ``laitue``,
  ``ris`` ≠ ``riz``, ``bar`` ≠ ``barbecue``).
- **Accents / casse** : insensibles (``creme`` = ``Crème``, ``oeuf`` = ``œuf``).
- **Pluriel français simple** : un ``s`` / ``x`` final est optionnel des deux côtés
  (``olive`` = ``olives``, ``chou`` = ``choux``). Les mots de moins de 4 lettres ne sont pas
  « dé-pluralisés » (``ris`` ne devient pas ``ri``), seul le suffixe optionnel s'applique.
- **Élision / ponctuation** : apostrophes (' ’), tirets, slash… sont des séparateurs ;
  ``blanc d'oeuf`` matche ``Blanc d’œufs``, ``noix de saint jacques`` matche ``noix de saint-jacques``.
- Mots-clés de moins de 3 caractères ignorés (trop ambigus).

La même expression régulière sert côté Postgres (``name__iregex``, préfiltre catalogue) et
côté Python (confirmation + détail des raisons) : il n'y a qu'une seule définition du match.

Les compléments de nom (« huile d'olive » pour « olives », « lait de coco » pour « lait ») ne
sont **pas** devinés : un mot-clé présent dans un nom composé matche, ce qui reste prudent pour
les allergies (« beurre de cacahuète » DOIT matcher une allergie à l'arachide). Les cas où le
mot-clé n'est qu'un complément sans le produit visé sont listés dans la table curée
``PreferenceIngredientException`` (voir ``dietary_filters``).
"""

import re
from functools import lru_cache

from unidecode import unidecode

# Caractères « de mot » (lettres latines accentuées incluses) ; tout le reste sépare les mots.
_WORD_CHARS = '0-9A-Za-zÀ-ÖØ-öø-ÿœŒ_'
_BOUNDARY = f'[^{_WORD_CHARS}]'
_SEPARATOR = f'[^{_WORD_CHARS}]+'

# Lettre ASCII normalisée → classe acceptant ses variantes accentuées (majuscules incluses :
# la casse des non-ASCII n'est pas garantie par iregex selon la locale Postgres).
_ACCENT_CLASSES = {
    'a': '[aàâäáãåAÀÂÄÁÃÅ]',
    'e': '[eéèêëEÉÈÊË]',
    'i': '[iîïíìIÎÏÍÌ]',
    'o': '[oôöóòõOÔÖÓÒÕ]',
    'u': '[uùûüúUÙÛÜÚ]',
    'c': '[cçCÇ]',
    'n': '[nñNÑ]',
    'y': '[yÿýYÝ]',
}
_OE = '(oe|OE|Oe|œ|Œ)'

_NON_ALNUM_RE = re.compile(r'[^a-z0-9]+')

MIN_KEYWORD_LENGTH = 3


def normalize_text(value):
    """Minuscules, sans accents (œ → oe), ponctuation/apostrophes → espaces."""
    if not isinstance(value, str):
        return ''
    try:
        s = unidecode(value)
    except Exception:
        s = value
    return _NON_ALNUM_RE.sub(' ', s.lower()).strip()


def _stem(token):
    """Pluriel simple : retire un s/x final sur les mots d'au moins 4 lettres."""
    if len(token) >= 4 and token[-1] in 'sx':
        return token[:-1]
    return token


def stem_tokens(value):
    return [_stem(t) for t in normalize_text(value).split() if t]


def match_key(value):
    """Clé de comparaison de libellés : « Olives » == « olive », « Crème » == « creme »."""
    return ' '.join(stem_tokens(value))


def _token_pattern(token):
    out = []
    i = 0
    while i < len(token):
        if token[i] == 'o' and i + 1 < len(token) and token[i + 1] == 'e':
            out.append(_OE)
            i += 2
            continue
        out.append(_ACCENT_CLASSES.get(token[i], token[i]))
        i += 1
    return ''.join(out) + '[sx]?'


@lru_cache(maxsize=4096)
def keyword_pattern(keyword):
    """
    Regex (compatible Postgres ARE et Python ``re``) pour un mot-clé, à utiliser en
    insensible à la casse. ``None`` si le mot-clé est trop court.
    """
    tokens = stem_tokens(keyword)
    if not tokens or len(''.join(tokens)) < MIN_KEYWORD_LENGTH:
        return None
    body = _SEPARATOR.join(_token_pattern(t) for t in tokens)
    return f'(^|{_BOUNDARY}){body}({_BOUNDARY}|$)'


@lru_cache(maxsize=4096)
def _compiled(keyword):
    pat = keyword_pattern(keyword)
    return re.compile(pat, re.IGNORECASE) if pat else None


def name_matches(name, keyword):
    """True si ``keyword`` apparaît en mots entiers dans ``name`` (règles ci-dessus)."""
    rx = _compiled(keyword or '')
    return bool(rx and name and rx.search(name))
