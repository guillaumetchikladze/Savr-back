"""
Cuisines du monde : liste fermée (~25) + normalisation de `search_context_tags.cuisine_style` (texte libre LLM).

`normalize_cuisine_styles(styles)` : table de synonymes (déterministe, sans réseau). Les tags non résolus
passent par le LLM via `python manage.py normalize_cuisines` (repli sur la liste fermée).
« fusion » et « autre » ne comptent pas dans la collection « tour du monde ».
"""

from __future__ import annotations

from dataclasses import dataclass

from .text import fold


@dataclass(frozen=True)
class Cuisine:
    code: str
    label: str
    flag: str
    country: str  # code ISO 3166-1 alpha-2 (ou région) pour la carte côté app


CUISINES: tuple[Cuisine, ...] = (
    Cuisine('francaise', 'Française', '🇫🇷', 'FR'),
    Cuisine('italienne', 'Italienne', '🇮🇹', 'IT'),
    Cuisine('espagnole', 'Espagnole', '🇪🇸', 'ES'),
    Cuisine('portugaise', 'Portugaise', '🇵🇹', 'PT'),
    Cuisine('grecque', 'Grecque', '🇬🇷', 'GR'),
    Cuisine('britannique', 'Britannique', '🇬🇧', 'GB'),
    Cuisine('allemande', 'Allemande', '🇩🇪', 'DE'),
    Cuisine('scandinave', 'Scandinave', '🇸🇪', 'SE'),
    Cuisine('turque', 'Turque', '🇹🇷', 'TR'),
    Cuisine('libanaise', 'Libanaise', '🇱🇧', 'LB'),
    Cuisine('marocaine', 'Marocaine', '🇲🇦', 'MA'),
    Cuisine('ethiopienne', 'Éthiopienne', '🇪🇹', 'ET'),
    Cuisine('africaine_ouest', "Africaine de l'Ouest", '🇸🇳', 'SN'),
    Cuisine('indienne', 'Indienne', '🇮🇳', 'IN'),
    Cuisine('thai', 'Thaïe', '🇹🇭', 'TH'),
    Cuisine('vietnamienne', 'Vietnamienne', '🇻🇳', 'VN'),
    Cuisine('chinoise', 'Chinoise', '🇨🇳', 'CN'),
    Cuisine('japonaise', 'Japonaise', '🇯🇵', 'JP'),
    Cuisine('coreenne', 'Coréenne', '🇰🇷', 'KR'),
    Cuisine('americaine', 'Américaine', '🇺🇸', 'US'),
    Cuisine('mexicaine', 'Mexicaine', '🇲🇽', 'MX'),
    Cuisine('peruvienne', 'Péruvienne', '🇵🇪', 'PE'),
    Cuisine('bresilienne', 'Brésilienne', '🇧🇷', 'BR'),
    Cuisine('fusion', 'Fusion', '🌍', ''),
    Cuisine('autre', 'Autre', '🍽️', ''),
)
CUISINES_BY_CODE: dict[str, Cuisine] = {c.code: c for c in CUISINES}
CUISINE_CODES: tuple[str, ...] = tuple(CUISINES_BY_CODE)
CUISINE_CHOICES = [(c.code, c.label) for c in CUISINES]
# Ne comptent pas comme « cuisine du monde explorée ».
NON_COLLECTIBLE = frozenset({'fusion', 'autre'})

# Synonymes (forme pliée : minuscules, sans accents). Les tags génériques (familiale, maison…) sont ignorés.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    'francaise': (
        'francaise',
        'francais',
        'france',
        'french',
        'bistrot',
        'bistro',
        'provencale',
        'provence',
        'bretonne',
        'lyonnaise',
        'alsacienne',
        'savoyarde',
        'basque',
        'normande',
        'bourguignonne',
        'sud ouest',
        'du sud ouest',
        'nicoise',
        'auvergnate',
        'lorraine',
        'corse',
        'cuisine francaise',
        'terroir francais',
    ),
    'italienne': ('italienne', 'italien', 'italie', 'italian', 'sicilienne', 'toscane', 'napolitaine', 'romaine'),
    'espagnole': ('espagnole', 'espagnol', 'espagne', 'spanish', 'tapas', 'catalane', 'andalouse'),
    'portugaise': ('portugaise', 'portugais', 'portugal'),
    'grecque': ('grecque', 'grec', 'grece', 'greek', 'cretoise'),
    'britannique': ('britannique', 'anglaise', 'anglais', 'angleterre', 'ecossaise', 'irlandaise', 'british'),
    'allemande': ('allemande', 'allemand', 'allemagne', 'german', 'autrichienne', 'bavaroise'),
    'scandinave': (
        'scandinave',
        'nordique',
        'suedoise',
        'norvegienne',
        'danoise',
        'finlandaise',
        'islandaise',
        'nordic',
    ),
    'turque': ('turque', 'turc', 'turquie', 'turkish', 'ottomane'),
    'libanaise': (
        'libanaise',
        'libanais',
        'liban',
        'lebanese',
        'levantine',
        'syrienne',
        'israelienne',
        'palestinienne',
    ),
    'marocaine': (
        'marocaine',
        'marocain',
        'maroc',
        'moroccan',
        'maghrebine',
        'algerienne',
        'tunisienne',
        'nord africaine',
    ),
    'ethiopienne': ('ethiopienne', 'ethiopie', 'erythreenne'),
    'africaine_ouest': (
        'africaine de l ouest',
        'ouest africaine',
        'senegalaise',
        'ivoirienne',
        'nigeriane',
        'malienne',
        'camerounaise',
        'ghaneenne',
        'beninoise',
        'togolaise',
    ),
    'indienne': ('indienne', 'indien', 'inde', 'indian', 'pakistanaise', 'srilankaise', 'sri lankaise', 'nepalaise'),
    'thai': ('thai', 'thaie', 'thailandaise', 'thailande'),
    'vietnamienne': ('vietnamienne', 'vietnamien', 'vietnam', 'viet'),
    'chinoise': ('chinoise', 'chinois', 'chine', 'chinese', 'cantonaise', 'sichuanaise', 'sichuan', 'taiwanaise'),
    'japonaise': ('japonaise', 'japonais', 'japon', 'japanese'),
    'coreenne': ('coreenne', 'coreen', 'coree', 'korean'),
    'americaine': ('americaine', 'americain', 'etats unis', 'usa', 'us', 'american', 'cajun', 'creole americaine'),
    'mexicaine': ('mexicaine', 'mexicain', 'mexique', 'mexican', 'tex mex', 'texmex'),
    'peruvienne': ('peruvienne', 'perou', 'peruvian'),
    'bresilienne': ('bresilienne', 'bresil', 'brazilian'),
    'fusion': ('fusion',),
}
_SYNONYM_INDEX: dict[str, str] = {fold(s): code for code, syns in _SYNONYMS.items() for s in syns}
# Tags trop génériques pour décider (ignorés ; pas de repli « autre » automatique).
GENERIC_TAGS = frozenset(
    fold(s)
    for s in (
        'maison',
        'familiale',
        'traditionnelle',
        'moderne',
        'internationale',
        'du monde',
        'healthy',
        'rapide',
        'vegetarienne',
        'vegane',
        'gourmande',
        'reconfortante',
        'classique',
        'simple',
        'europeenne',
        'asiatique',
        'mediterraneenne',
        'orientale',
        'exotique',
        'creative',
        'bistronomique',
        'cuisine du monde',
        'street food',
    )
)


def _match_one(tag: str) -> str | None:
    folded = fold(tag)
    if not folded or folded in GENERIC_TAGS:
        return None
    hit = _SYNONYM_INDEX.get(folded)
    if hit:
        return hit
    # Phrase plus longue (« cuisine italienne du sud ») : un synonyme comme mot entier.
    words = f' {folded} '
    for syn, code in sorted(_SYNONYM_INDEX.items(), key=lambda kv: -len(kv[0])):
        if len(syn) >= 4 and f' {syn} ' in words:
            return code
    return None


def normalize_cuisine_styles(styles) -> str | None:
    """Premier tag reconnu → code de la liste fermée ; None si rien n'est décidable sans LLM."""
    if isinstance(styles, str):
        styles = [styles]
    for tag in styles or []:
        code = _match_one(tag) if isinstance(tag, str) else None
        if code:
            return code
    return None


def recipe_cuisine_styles(recipe_tags) -> list[str]:
    tags = recipe_tags or {}
    styles = tags.get('cuisine_style') if isinstance(tags, dict) else None
    return [s for s in (styles or []) if isinstance(s, str) and s.strip()]


def has_only_generic_styles(styles: list[str]) -> bool:
    return all(fold(s) in GENERIC_TAGS for s in styles)


def cuisine_payload(code: str, count: int | None = None) -> dict:
    c = CUISINES_BY_CODE.get(code) or CUISINES_BY_CODE['autre']
    data = {'code': c.code, 'label': c.label, 'flag': c.flag, 'country': c.country or None}
    if count is not None:
        data['count'] = count
    return data
