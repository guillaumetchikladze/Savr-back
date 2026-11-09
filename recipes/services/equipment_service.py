"""
« Ta cuisine » : équipements de l'utilisateur et équipements requis par les recettes.

Contrat front : `.claude-work/specs/equipment.md`. Catalogue : `equipment_catalog` ; détection : `equipment_inference`.

Cuisine d'un utilisateur (choix de modèle)
- Stockée **par utilisateur** (`User.kitchen_equipment` + `kitchen_equipment_set_at`) : 0 requête pour le viewer
  (déjà chargé par l'authentification).
- Le foyer partage une cuisine : tant qu'un utilisateur n'a rien renseigné, sa cuisine effective est celle du compte
  de son foyer (lien accepté, `HouseholdMember`) qui l'a renseignée le plus récemment (`source: "household"`,
  1 requête, mémorisée sur l'objet user de la requête). Dès qu'il enregistre la sienne, elle prime.
- Jamais renseignée (ni lui, ni son foyer) → cuisine **inconnue** : aucun filtre, aucun « Il te manque »
  (on ne devine pas). La sélection « Tout le classique » n'est qu'une pré-sélection de l'onboarding.

Recettes
- `Recipe.required_equipment` = surcharge manuelle (`required_equipment_override`) si présente, sinon inférence
  (titre + étapes) ∪ indices du pipeline IA (`equipment_ai_hints`). Recalculé par signaux (création / édition d'une
  étape, sauvegarde de la recette) ; regroupé en un seul calcul par recette dans `deferred_equipment_refresh()`.
- Backfill : `python manage.py infer_recipe_equipment --only-missing`.
"""

from __future__ import annotations

import contextlib
import threading

from django.db.models import Q
from django.utils import timezone

from recipes.services import equipment_catalog as catalog
from recipes.services.equipment_catalog import Kitchen
from recipes.services.equipment_inference import infer_equipment

_REQUEST_ATTR = '_effective_kitchen'
_UNSET = object()

SOURCE_INFERRED = 'inferred'
SOURCE_MANUAL = 'manual'


# ---------------------------------------------------------------------------
# Cuisine de l'utilisateur
# ---------------------------------------------------------------------------
def has_set_equipment(user) -> bool:
    return bool(getattr(user, 'kitchen_equipment_set_at', None))


def _household_storage(user) -> dict | None:
    """Cuisine du compte du foyer (lien accepté) renseignée le plus récemment : 1 requête."""
    from accounts.models import HouseholdMember, User

    links = HouseholdMember.objects.filter(status=HouseholdMember.STATUS_ACCEPTED, user__isnull=False)
    partners = Q(id__in=links.filter(owner_id=user.id).values('user_id')) | Q(
        id__in=links.filter(user_id=user.id).values('owner_id')
    )
    row = (
        User.objects.filter(partners, kitchen_equipment_set_at__isnull=False, is_active=True)
        .order_by('-kitchen_equipment_set_at')
        .values_list('kitchen_equipment', flat=True)
        .first()
    )
    return row if isinstance(row, dict) else None


def effective_kitchen(user) -> Kitchen | None:
    """Cuisine effective (la sienne : 0 requête, sinon celle du foyer : 1 requête), ou None si inconnue."""
    if user is None or not getattr(user, 'is_authenticated', False):
        return None
    if has_set_equipment(user):
        return catalog.kitchen_from_storage(user.kitchen_equipment or {'keys': []}, 'mine')
    return catalog.kitchen_from_storage(_household_storage(user), 'household')


def request_kitchen(request) -> Kitchen | None:
    """Cuisine effective du user de la requête, évaluée une seule fois par requête (mémo sur l'objet requête)."""
    if request is None:
        return None
    cached = getattr(request, _REQUEST_ATTR, _UNSET)
    if cached is _UNSET:
        cached = effective_kitchen(getattr(request, 'user', None))
        setattr(request, _REQUEST_ATTR, cached)
    return cached


def equipment_payload(user) -> dict:
    """GET /api/equipment/."""
    kitchen = effective_kitchen(user)
    if kitchen is None:
        storage, source = catalog.default_storage(), 'default'
    elif kitchen.source == 'mine':
        storage, source = user.kitchen_equipment or {'keys': []}, 'mine'
    else:
        storage = {
            'keys': sorted(kitchen.keys),
            'burners': kitchen.burners,
            'induction': kitchen.induction,
            'ovens': kitchen.ovens,
        }
        source = 'household'
    items = catalog.items_payload(storage)
    return {
        'has_set_equipment': has_set_equipment(user),
        'source': source,
        'equipment': items,
        'keys': [i['key'] for i in items],
        'updated_at': user.kitchen_equipment_set_at.isoformat() if user.kitchen_equipment_set_at else None,
    }


def set_equipment(user, data) -> dict:
    """PUT /api/equipment/ (remplace tout). Lève `EquipmentError`."""
    storage = catalog.normalize_payload(data)
    user.kitchen_equipment = storage
    user.kitchen_equipment_set_at = timezone.now()
    user.save(update_fields=['kitchen_equipment', 'kitchen_equipment_set_at', 'updated_at'])
    return equipment_payload(user)


def viewer_kitchen(context: dict | None) -> Kitchen | None:
    """Cuisine du viewer d'un serializer (requête → user ; une seule évaluation par requête)."""
    return request_kitchen((context or {}).get('request'))


# ---------------------------------------------------------------------------
# Ce qui manque au viewer
# ---------------------------------------------------------------------------
def recipe_missing(recipe, kitchen: Kitchen | None) -> list[dict]:
    if recipe is None or kitchen is None:
        return []
    return catalog.missing_items(
        getattr(recipe, 'required_equipment', None) or [], getattr(recipe, 'equipment_alternatives', None), kitchen
    )


def adaptation_enabled() -> bool:
    from django.conf import settings

    return bool(getattr(settings, 'EQUIPMENT_ADAPT_ENABLED', True))


def required_payload(recipe) -> list[dict]:
    return [catalog.item_payload(k) for k in catalog.sort_keys(getattr(recipe, 'required_equipment', None) or [])]


def recipes_missing(recipes, kitchen: Kitchen | None) -> tuple[list[dict], int | None]:
    """Union de ce qui manque pour plusieurs recettes (post multi-recettes) + 1re recette à adapter."""
    merged: dict[str, dict] = {}
    adapt_id = None
    for recipe in recipes:
        missing = recipe_missing(recipe, kitchen)
        if missing and adapt_id is None:
            adapt_id = recipe.id
        for item in missing:
            entry = merged.setdefault(item['key'], {**item, 'recipe_ids': []})
            entry['recipe_ids'].append(recipe.id)
    order = {k: i for i, k in enumerate(catalog.KEYS)}
    return sorted(merged.values(), key=lambda m: order[m['key']]), adapt_id


# ---------------------------------------------------------------------------
# Recherche : exclure / déclasser
# ---------------------------------------------------------------------------
def search_mode() -> str:
    from django.conf import settings

    mode = str(getattr(settings, 'EQUIPMENT_SEARCH_MODE', 'exclude')).lower()
    return mode if mode in ('exclude', 'rank', 'off') else 'exclude'


def blocking_keys(kitchen: Kitchen | None) -> list[str]:
    """Équipements « durs » absents de la cuisine et sans alternative du catalogue disponible."""
    if kitchen is None:
        return []
    out = []
    for item in catalog.CATALOG:
        if not item.hard or kitchen.has(item.key):
            continue
        if any(kitchen.has(alt) for alt in item.alternatives):
            continue
        out.append(item.key)
    return out


def exclude_unfeasible(queryset, user, *, kitchen: Kitchen | None | object = _UNSET, mode: str | None = None):
    """
    Retire les recettes qui demandent un équipement « dur » absent (four, plaques, barbecue, gaufrier) sans
    alternative possible. Ses propres recettes restent visibles. Four absent mais airfryer présent : la recette
    reste si elle accepte l'airfryer (≤ 4 portions) ; sinon elle est retirée.
    """
    mode = mode or search_mode()
    if kitchen is _UNSET:
        kitchen = effective_kitchen(user)
    if mode != 'exclude' or kitchen is None:
        return queryset
    mine = Q(created_by_id=user.id)
    for key in blocking_keys(kitchen):
        queryset = queryset.exclude(Q(required_equipment__contains=[key]) & ~mine)
    if not kitchen.has('four') and kitchen.has('airfryer'):
        # Alternative propre à la recette : `equipment_alternatives.four == []` → four vraiment requis.
        queryset = queryset.exclude(
            Q(required_equipment__contains=['four']) & Q(equipment_alternatives__four=[]) & ~mine
        )
    return queryset


def rank_penalty(recipe, kitchen: Kitchen | None) -> int:
    """0 = faisable ; 1 = un équipement « mou » manque ou une alternative existe ; 2 = non faisable."""
    missing = recipe_missing(recipe, kitchen)
    if not missing:
        return 0
    return 2 if any(m['blocking'] for m in missing) else 1


def sort_by_equipment(items: list, kitchen: Kitchen | None) -> list:
    """Tri stable : les recettes faisables d'abord (sans jamais rien retirer)."""
    if kitchen is None or search_mode() == 'off':
        return items
    return [r for _, _, r in sorted((rank_penalty(r, kitchen), i, r) for i, r in enumerate(items))]


# ---------------------------------------------------------------------------
# Équipements requis par une recette
# ---------------------------------------------------------------------------
def compute_recipe_equipment(recipe, steps: list[tuple[str, str]] | None = None) -> tuple[list[str], dict, str]:
    """(required, alternatives, source) d'une recette ; `steps` = [(titre, instruction)] (sinon lus en base)."""
    if steps is None:
        steps = list(recipe.steps.order_by('order').values_list('title', 'instruction'))
    keys, alternatives = infer_equipment(recipe.title or '', steps, servings=recipe.servings)
    override = recipe.required_equipment_override
    if isinstance(override, list):
        return catalog.sort_keys(override), alternatives, SOURCE_MANUAL
    hints = recipe.equipment_ai_hints if isinstance(recipe.equipment_ai_hints, list) else []
    return catalog.sort_keys([*keys, *hints]), alternatives, SOURCE_INFERRED


def refresh_recipe_equipment(recipe_or_id) -> list[str] | None:
    """Recalcule et enregistre `required_equipment` (UPDATE seulement si ça change). 2-3 requêtes."""
    from recipes.models import Recipe

    recipe = recipe_or_id
    if not isinstance(recipe, Recipe):
        recipe = (
            Recipe.objects.filter(pk=recipe_or_id)
            .only(
                'id',
                'title',
                'servings',
                'required_equipment',
                'equipment_alternatives',
                'required_equipment_override',
                'equipment_ai_hints',
            )
            .first()
        )
        if recipe is None:
            return None
    required, alternatives, _ = compute_recipe_equipment(recipe)
    if recipe.required_equipment != required or (recipe.equipment_alternatives or {}) != alternatives:
        Recipe.objects.filter(pk=recipe.pk).update(required_equipment=required, equipment_alternatives=alternatives)
        recipe.required_equipment = required
        recipe.equipment_alternatives = alternatives
    return required


# Regroupement : pendant une création / édition (plusieurs étapes), un seul recalcul par recette à la fin.
_local = threading.local()


def _pending() -> set | None:
    return getattr(_local, 'pending', None)


def schedule_refresh(recipe_id: int | None) -> None:
    if not recipe_id:
        return
    pending = _pending()
    if pending is not None:
        pending.add(recipe_id)
    else:
        refresh_recipe_equipment(recipe_id)


@contextlib.contextmanager
def deferred_equipment_refresh():
    """Regroupe les recalculs déclenchés par les signaux (création d'une recette et de ses étapes)."""
    outer = _pending()
    if outer is not None:  # déjà dans un bloc : le bloc externe recalculera
        yield
        return
    _local.pending = set()
    try:
        yield
    finally:
        ids, _local.pending = _local.pending, None
    for recipe_id in sorted(ids):
        refresh_recipe_equipment(recipe_id)


def set_ai_hints(recipe, keys) -> None:
    """Indices du pipeline IA (sortie structurée) : filtrés sur le catalogue, unis à l'inférence."""
    from recipes.models import Recipe

    hints = catalog.sort_keys(keys or [])
    Recipe.objects.filter(pk=recipe.pk).update(equipment_ai_hints=hints)
    recipe.equipment_ai_hints = hints
    refresh_recipe_equipment(recipe)
