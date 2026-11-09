"""
Catalogue des équipements de cuisine (« Ta cuisine ») : clés stables, libellés français, icônes.

Pur (aucune base) : utilisé par l'API `/api/equipment/`, l'inférence des recettes, la recherche, le plan de bataille
et l'agent. Contrat front : `.claude-work/specs/equipment.md`.

- `key` : identifiant stable (jamais renommé : stocké sur `User.kitchen_equipment` et `Recipe.required_equipment`).
- `icon` : nom Ionicons (l'app utilise Ionicons partout) ; `emoji` : repli.
- `hard` : sans cet équipement (ni une alternative), le plat n'est pas faisable → exclu des suggestions ; sinon
  (`hard=False`) le plat est seulement moins bien classé (« tu peux t'en passer »).
- `alternatives` : équipements qui peuvent le remplacer (four → airfryer pour de petites quantités…).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

MIN_BURNERS = 1
MAX_BURNERS = 6
DEFAULT_BURNERS = 4
MAX_OVENS = 2


@dataclass(frozen=True)
class Equipment:
    key: str
    label: str
    short_label: str  # « Il te manque : un four »
    icon: str
    emoji: str
    hard: bool = False
    alternatives: tuple[str, ...] = ()
    options: tuple[str, ...] = field(default=())


CATALOG: tuple[Equipment, ...] = (
    Equipment('four', 'Four', 'un four', 'thermometer-outline', '🥧', True, ('airfryer',), ('ovens',)),
    Equipment(
        'plaques',
        'Plaques de cuisson',
        'des plaques de cuisson',
        'flame-outline',
        '🔥',
        True,
        ('robot_cuiseur',),
        ('burners', 'induction'),
    ),
    Equipment('micro_ondes', 'Micro-ondes', 'un micro-ondes', 'radio-outline', '⏲️', False, ('plaques', 'four')),
    Equipment(
        'robot_cuiseur',
        'Robot cuiseur (Thermomix, Cookeo…)',
        'un robot cuiseur',
        'hardware-chip-outline',
        '🤖',
        False,
        ('plaques',),
    ),
    Equipment(
        'cocotte_minute',
        'Cocotte-minute',
        'une cocotte-minute',
        'speedometer-outline',
        '♨️',
        False,
        ('robot_cuiseur', 'cocotte_fonte', 'plaques'),
    ),
    Equipment(
        'mixeur_plongeant',
        'Mixeur plongeant',
        'un mixeur plongeant',
        'color-wand-outline',
        '🪄',
        False,
        ('blender', 'robot_cuiseur'),
    ),
    Equipment('blender', 'Blender', 'un blender', 'beaker-outline', '🥤', False, ('mixeur_plongeant', 'robot_cuiseur')),
    Equipment(
        'robot_patissier', 'Robot pâtissier', 'un robot pâtissier', 'sync-outline', '🎂', False, ('robot_cuiseur',)
    ),
    Equipment('airfryer', 'Airfryer', 'un airfryer', 'cube-outline', '🍟', False, ('four',)),
    Equipment(
        'barbecue_plancha',
        'Barbecue / plancha',
        'un barbecue ou une plancha',
        'bonfire-outline',
        '🍖',
        True,
        ('plaques', 'four'),
    ),
    Equipment(
        'cocotte_fonte',
        'Cocotte en fonte',
        'une cocotte en fonte',
        'ellipse-outline',
        '🍲',
        False,
        ('cocotte_minute', 'robot_cuiseur', 'four'),
    ),
    Equipment('machine_a_pain', 'Machine à pain', 'une machine à pain', 'square-outline', '🍞', False, ('four',)),
    Equipment('sorbetiere', 'Sorbetière', 'une sorbetière', 'ice-cream-outline', '🍨', False, ('blender',)),
    Equipment('gaufrier', 'Gaufrier', 'un gaufrier', 'grid-outline', '🧇', True),
    Equipment(
        'appareil_raclette_fondue',
        'Appareil à raclette / fondue',
        'un appareil à raclette ou à fondue',
        'people-outline',
        '🧀',
        False,
        ('four', 'plaques'),
    ),
    Equipment('wok', 'Wok', 'un wok', 'restaurant-outline', '🥡', False, ('plaques',)),
    Equipment(
        'autocuiseur_vapeur',
        'Cuiseur vapeur',
        'un cuiseur vapeur',
        'water-outline',
        '💧',
        False,
        ('plaques', 'cocotte_minute', 'robot_cuiseur', 'micro_ondes'),
    ),
)

BY_KEY: dict[str, Equipment] = {e.key: e for e in CATALOG}
KEYS: tuple[str, ...] = tuple(e.key for e in CATALOG)
_ORDER = {k: i for i, k in enumerate(KEYS)}

# « Tout le classique » : pré-sélection de l'onboarding.
CLASSIC_KEYS: tuple[str, ...] = ('four', 'plaques', 'micro_ondes', 'mixeur_plongeant')


class EquipmentError(ValueError):
    def __init__(self, message: str, code: str = 'invalid_equipment'):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True)
class Kitchen:
    """Équipements effectifs d'une cuisine (ceux de l'utilisateur, sinon de son foyer)."""

    keys: frozenset[str]
    burners: int | None = DEFAULT_BURNERS
    induction: bool = False
    ovens: int = 1
    source: str = 'mine'  # mine | household

    def has(self, key: str) -> bool:
        return key in self.keys

    @property
    def signature(self) -> str:
        return equipment_signature(self.keys)


def sort_keys(keys) -> list[str]:
    return sorted({k for k in keys if k in BY_KEY}, key=_ORDER.__getitem__)


def equipment_signature(keys) -> str:
    """Empreinte stable d'un jeu d'équipements (idempotence des adaptations)."""
    return hashlib.sha256(','.join(sort_keys(keys)).encode()).hexdigest()[:32]


def label(key: str) -> str:
    item = BY_KEY.get(key)
    return item.label if item else key


def item_payload(key: str) -> dict:
    item = BY_KEY[key]
    return {'key': item.key, 'label': item.label, 'icon': item.icon, 'emoji': item.emoji}


def catalog_payload() -> dict:
    return {
        'items': [
            {
                'key': e.key,
                'label': e.label,
                'short_label': e.short_label,
                'icon': e.icon,
                'emoji': e.emoji,
                'hard': e.hard,
                'alternatives': list(e.alternatives),
                'options': list(e.options),
            }
            for e in CATALOG
        ],
        'classic': default_payload_items(),
        'limits': {
            'burners': {'min': MIN_BURNERS, 'max': MAX_BURNERS, 'default': DEFAULT_BURNERS},
            'ovens_max': MAX_OVENS,
        },
    }


def default_storage() -> dict:
    return {'keys': list(CLASSIC_KEYS), 'burners': DEFAULT_BURNERS, 'induction': False, 'ovens': 1}


def default_payload_items() -> list[dict]:
    return items_payload(default_storage())


def items_payload(storage: dict) -> list[dict]:
    """Liste « équipement » de l'API à partir du stockage normalisé."""
    out = []
    for key in sort_keys(storage.get('keys') or []):
        entry = item_payload(key)
        if key == 'plaques':
            entry['burners'] = storage.get('burners') or DEFAULT_BURNERS
            entry['induction'] = bool(storage.get('induction'))
        if key == 'four':
            entry['count'] = storage.get('ovens') or 1
        out.append(entry)
    return out


def _int_in(value, low: int, high: int, name: str) -> int:
    if isinstance(value, bool):
        raise EquipmentError(f'{name} doit être un entier.')
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise EquipmentError(f'{name} doit être un entier.') from exc
    if not low <= number <= high:
        raise EquipmentError(f'{name} doit être entre {low} et {high}.')
    return number


def normalize_payload(data) -> dict:
    """
    PUT /api/equipment/ → stockage `{keys, burners, induction, ovens}`.

    Formes acceptées : `{"equipment": [{"key": "plaques", "burners": 2, "induction": true}, {"key": "four"}, …]}`
    ou `{"keys": [...], "burners": 2, "induction": true, "ovens": 1}` (raccourci). Clé inconnue → 400.
    `[]` est valide (« je n'ai rien de tout ça »).
    """
    if not isinstance(data, dict):
        raise EquipmentError('Corps JSON attendu.')
    burners = data.get('burners')
    induction = data.get('induction')
    ovens = data.get('ovens')
    if 'equipment' in data:
        raw = data.get('equipment')
        if not isinstance(raw, list):
            raise EquipmentError('equipment doit être une liste.')
        keys = []
        for entry in raw:
            if isinstance(entry, str):
                entry = {'key': entry}
            if not isinstance(entry, dict) or not isinstance(entry.get('key'), str):
                raise EquipmentError('Chaque équipement doit avoir une clé (key).')
            keys.append(entry['key'])
            if entry['key'] == 'plaques':
                burners = entry.get('burners', burners)
                induction = entry.get('induction', induction)
            if entry['key'] == 'four':
                ovens = entry.get('count', ovens)
    elif 'keys' in data:
        keys = data.get('keys')
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            raise EquipmentError('keys doit être une liste de clés.')
    else:
        raise EquipmentError('Indique equipment (ou keys).')
    unknown = [k for k in keys if k not in BY_KEY]
    if unknown:
        raise EquipmentError(f'Équipement inconnu : {", ".join(unknown[:3])}.', 'unknown_equipment')
    keys = sort_keys(keys)
    return {
        'keys': keys,
        'burners': _int_in(burners, MIN_BURNERS, MAX_BURNERS, 'burners')
        if 'plaques' in keys and burners is not None
        else (DEFAULT_BURNERS if 'plaques' in keys else None),
        'induction': bool(induction) if 'plaques' in keys else False,
        'ovens': _int_in(ovens, 1, MAX_OVENS, 'ovens')
        if 'four' in keys and ovens is not None
        else (1 if 'four' in keys else 0),
    }


def kitchen_from_storage(storage: dict | None, source: str = 'mine') -> Kitchen | None:
    if not isinstance(storage, dict):
        return None
    keys = frozenset(sort_keys(storage.get('keys') or []))
    return Kitchen(
        keys=keys,
        burners=(storage.get('burners') or DEFAULT_BURNERS) if 'plaques' in keys else 0,
        induction=bool(storage.get('induction')) and 'plaques' in keys,
        ovens=(storage.get('ovens') or 1) if 'four' in keys else 0,
        source=source,
    )


# ---------------------------------------------------------------------------
# Ce qui manque à une cuisine pour une recette
# ---------------------------------------------------------------------------
def missing_items(required, alternatives: dict | None, kitchen: Kitchen | None) -> list[dict]:
    """
    Équipements requis absents de `kitchen` : `[{key, label, short_label, icon, emoji, hard, blocking,
    alternatives: [{key, label}]}]` (alternatives que la cuisine possède). Cuisine inconnue → [] (on ne devine pas).
    `blocking` = requis « dur » sans aucune alternative disponible : le plat n'est pas faisable tel quel.
    """
    if kitchen is None or not required:
        return []
    alternatives = alternatives or {}
    out = []
    for key in sort_keys(required):
        if kitchen.has(key):
            continue
        item = BY_KEY[key]
        candidates = alternatives[key] if key in alternatives else list(item.alternatives)
        available = [k for k in sort_keys(candidates) if kitchen.has(k)]
        out.append(
            {
                'key': key,
                'label': item.label,
                'short_label': item.short_label,
                'icon': item.icon,
                'emoji': item.emoji,
                'hard': item.hard,
                'blocking': item.hard and not available,
                'alternatives': [{'key': k, 'label': BY_KEY[k].label} for k in available],
            }
        )
    return out


def missing_label(missing: list[dict]) -> str:
    """« Il te manque : un four » (badge) ; vide si rien ne manque."""
    if not missing:
        return ''
    names = [m['short_label'] for m in missing[:3]]
    text = names[0] if len(names) == 1 else ', '.join(names[:-1]) + ' et ' + names[-1]
    return f'Il te manque : {text}'
