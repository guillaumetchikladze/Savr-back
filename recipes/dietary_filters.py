"""
Filtrage des recettes / alertes selon allergies, régimes et goûts (« n'aime pas »).

Un seul moteur de matching pour tous les usages (exclusion stricte des suggestions,
conflits invités, raisons + détail affichés dans l'app) :

1. **Mots-clés par préférence** (dans cet ordre de priorité) : override utilisateur
   (``UserPreferenceMapping``) → mapping communautaire **validé** (``PreferenceMapping``,
   les ``pending`` ne déclenchent jamais d'alerte) → table historique
   ``DietaryIngredientMatch`` → le libellé brut. Régimes : ``REGIME_KEYWORDS``.

2. **Match texte en mots entiers** (``dietary_matching``) : frontières de mot, accents,
   pluriel simple, élision. Même regex en SQL (préfiltre catalogue) et en Python.

3. **Exceptions** (``PreferenceIngredientException``) soustraites de tous les chemins :
   règles globales curées (« olive » ↛ « huile d'olive », « lait » ↛ « lait de coco »…) et
   « pas un problème » signalés depuis l'app.

Pas de couche sémantique (embeddings) : elle reliait des ingrédients voisins mais distincts
(« olives » ↔ « huile d'olive », distance cosinus ≤ 0,42) sans moyen de l'expliquer à
l'utilisateur ; un match sémantique n'apportait de valeur que sans hit texte, précisément le
cas où il produisait des faux positifs. Les allergies/régimes ne l'utilisaient déjà pas.
"""

import logging

from django.db.models import Q

from .dietary_matching import keyword_pattern, match_key, name_matches
from .models import (
    DietaryIngredientMatch,
    Ingredient,
    MealInvitation,
    PreferenceIngredientException,
    PreferenceMapping,
    RecipeIngredient,
    StepIngredient,
    UserPreferenceMapping,
)

logger = logging.getLogger(__name__)

PREFERENCE_KINDS = ('allergy', 'diet', 'dislike')
# Ordre UX : allergy > diet > dislike
KIND_ORDER = {k: i for i, k in enumerate(PREFERENCE_KINDS)}


def _normalize_label_strings(raw_iterable):
    """Chaînes non vides uniques (ordre conservé)."""
    if not raw_iterable:
        return []
    out = []
    seen = set()
    for x in raw_iterable:
        if not isinstance(x, str):
            continue
        s = x.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


# Repérage large viande/poisson si l’utilisateur se déclare végétarien (complément des listes).
VEGETARIAN_EXTRA_LABELS = [
    'viande',
    'boeuf',
    'porc',
    'veau',
    'agneau',
    'mouton',
    'jambon',
    'lard',
    'bacon',
    'saucisse',
    'chorizo',
    'poulet',
    'canard',
    'dinde',
    'volaille',
    'lapin',
    'gibier',
    'poisson',
    'saumon',
    'thon',
    'cabillaud',
    'maquereau',
    'sardine',
    'anchois',
    'hareng',
    'crevette',
    'moule',
    'crabe',
    'homard',
    'langouste',
    'calamar',
    'poulpe',
]

REGIME_KEYWORDS = {
    # Conservative: these keywords are matched with word boundaries in "strict" mode.
    'vegetarian': VEGETARIAN_EXTRA_LABELS,
    'pescatarian': [
        # exclude meats, but allow fish/seafood
        'viande',
        'boeuf',
        'porc',
        'veau',
        'agneau',
        'mouton',
        'jambon',
        'lard',
        'bacon',
        'saucisse',
        'chorizo',
        'poulet',
        'canard',
        'dinde',
        'volaille',
        'lapin',
        'gibier',
    ],
    'vegan': [
        # animal products + meat/fish
        *VEGETARIAN_EXTRA_LABELS,
        'oeuf',
        'œuf',
        'lait',
        'fromage',
        'beurre',
        'crème',
        'creme',
        'yaourt',
        'miel',
    ],
    'gluten_free': [
        # prefer explicit mapping if present, but fallback works with these keywords too
        'gluten',
        'blé',
        'ble',
        'farine',
        'seigle',
        'orge',
    ],
    'halal': [
        # simplified: pork/alcohol are the biggest practical blockers
        'porc',
        'jambon',
        'lard',
        'bacon',
        'alcool',
        'vin',
        'bière',
        'biere',
    ],
    'kosher': [
        'porc',
        'jambon',
        'lard',
        'bacon',
        'fruits de mer',
        'crevette',
        'moule',
        'crabe',
        'homard',
        'langouste',
    ],
    'keto': [
        # conservative; keto is a preference/rule-of-thumb, kept mostly for scoring
        'sucre',
        'pâte',
        'pates',
        'riz',
        'pain',
        'farine',
    ],
}


def _normalize_regimes(raw_iterable):
    if not raw_iterable:
        return []
    out = []
    seen = set()
    for x in raw_iterable:
        if not isinstance(x, str):
            continue
        s = x.strip().lower()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _regimes_from_user(user):
    """
    Régimes déclarés. Compat: si `is_vegetarian` existe encore, on l’ajoute.
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return []
    regimes = _normalize_regimes(getattr(user, 'regimes', None) or [])
    if getattr(user, 'is_vegetarian', False) and 'vegetarian' not in regimes:
        regimes.append('vegetarian')
    return regimes


def _allergy_labels_from_user(user):
    if not user or not getattr(user, 'is_authenticated', False):
        return []
    return _normalize_label_strings(getattr(user, 'allergies', None) or [])


def _dislike_labels_from_user(user):
    if not user or not getattr(user, 'is_authenticated', False):
        return []
    return _normalize_label_strings(getattr(user, 'food_dislikes', None) or [])


def keywords_for_labels(labels):
    """
    Mots-clés pour la recherche textuelle à partir d’une liste de libellés :
    DietaryIngredientMatch ou libellé brut.
    """
    if not labels:
        return []

    keywords = []
    for lbl in labels:
        db_kws = list(
            DietaryIngredientMatch.objects.filter(preference_label=lbl).values_list('match_keyword', flat=True)
        )
        if db_kws:
            keywords.extend(db_kws)
        elif len(lbl) >= 2:
            keywords.append(lbl)

    # Variantes simples pour éviter les faux négatifs fréquents (singulier/pluriel FR),
    # sans retomber dans du substring trop agressif.
    expanded = []
    for k in keywords:
        t = (k or '').strip()
        if not t:
            continue
        expanded.append(t)
        low = t.lower()
        # Œuf(s): les ingrédients sont souvent enregistrés au pluriel ("oeufs").
        if low in {'oeuf', 'œuf'}:
            expanded.append('oeufs')
            expanded.append('œufs')
        # Blanc/jaune d'œuf(s)
        if low in {"blanc d'oeuf", 'blanc d’œuf'}:
            expanded.append("blanc d'oeufs")
            expanded.append('blanc d’œufs')
        if low in {"jaune d'oeuf", 'jaune d’œuf'}:
            expanded.append("jaune d'oeufs")
            expanded.append('jaune d’œufs')

    dedup = []
    seen = set()
    for k in expanded:
        t = (k or '').strip()
        if len(t) < 2:
            continue
        low = t.lower()
        if low in seen:
            continue
        seen.add(low)
        dedup.append(t)
    return dedup


def _normalize_keywords_list(raw_iterable):
    if not raw_iterable:
        return []
    out = []
    seen = set()
    for x in raw_iterable:
        if not isinstance(x, str):
            continue
        s = x.strip()
        if not s:
            continue
        low = s.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(s)
    # reuse variant expansion + dedup
    return keywords_for_labels(out)


def keywords_by_label_for_user(user, kind, labels):
    """
    ``{label: [mots-clés]}`` pour des libellés ``allergy`` | ``dislike`` :
    override user > mapping communautaire validé > ``DietaryIngredientMatch`` > libellé brut.
    Les mappings ``pending`` sont ignorés (jamais d'alerte sur une proposition non validée).
    """
    labels = _normalize_label_strings(labels)
    if not labels:
        return {}
    if kind not in {'allergy', 'dislike'}:
        return {lbl: keywords_for_labels([lbl]) for lbl in labels}

    user_map = {}
    if user and getattr(user, 'is_authenticated', False):
        for row in UserPreferenceMapping.objects.filter(user=user, kind=kind, label__in=labels).values(
            'label', 'keywords'
        ):
            user_map[row['label']] = _normalize_keywords_list(row.get('keywords') or [])

    global_map = {}
    for row in PreferenceMapping.objects.filter(kind=kind, status='validated', label__in=labels).values(
        'label', 'keywords'
    ):
        global_map[row['label']] = _normalize_keywords_list(row.get('keywords') or [])

    legacy_map = {}
    for row in DietaryIngredientMatch.objects.filter(preference_label__in=labels).values(
        'preference_label', 'match_keyword'
    ):
        legacy_map.setdefault(row['preference_label'], []).append(row['match_keyword'])

    out = {}
    for lbl in labels:
        if user_map.get(lbl):
            out[lbl] = user_map[lbl]
        elif global_map.get(lbl):
            out[lbl] = global_map[lbl]
        elif legacy_map.get(lbl):
            out[lbl] = _normalize_keywords_list(legacy_map[lbl])
        else:
            out[lbl] = keywords_for_labels([lbl])
    return out


def _usable_keywords(keywords):
    """Mots-clés matchables, dédoublonnés à la normalisation près (œuf/oeufs → un seul)."""
    out = []
    seen = set()
    for kw in keywords or []:
        key = match_key(kw)
        if not key or key in seen or keyword_pattern(kw) is None:
            continue
        seen.add(key)
        out.append(kw)
    return out


def _preference_rules(user, kinds=PREFERENCE_KINDS):
    """``[(kind, label, [mots-clés])]`` triés allergy > diet > dislike puis ordre déclaré."""
    if not user or not getattr(user, 'is_authenticated', False):
        return []
    rules = []
    if 'allergy' in kinds:
        labels = _allergy_labels_from_user(user)
        kws = keywords_by_label_for_user(user, 'allergy', labels)
        rules += [('allergy', lbl, _usable_keywords(kws.get(lbl))) for lbl in labels]
    if 'diet' in kinds:
        rules += [('diet', r, _usable_keywords(REGIME_KEYWORDS.get(r))) for r in _regimes_from_user(user)]
    if 'dislike' in kinds:
        labels = _dislike_labels_from_user(user)
        kws = keywords_by_label_for_user(user, 'dislike', labels)
        rules += [('dislike', lbl, _usable_keywords(kws.get(lbl))) for lbl in labels]
    return [r for r in rules if r[2]]


def declared_labels_for_kind(user, kind):
    """Libellés déclarés par ``user`` pour un type (codes de régime pour ``diet``)."""
    if kind == 'allergy':
        return _allergy_labels_from_user(user)
    if kind == 'dislike':
        return _dislike_labels_from_user(user)
    if kind == 'diet':
        return _regimes_from_user(user)
    return []


def _load_exceptions(target, viewer=None):
    """
    Exceptions applicables aux préférences de ``target`` vues par ``viewer`` :
    globales + celles posées par ``target`` lui-même + celles posées par ``viewer``.
    """
    creators = {target.id}
    if viewer is not None and getattr(viewer, 'id', None):
        creators.add(viewer.id)
    rows = PreferenceIngredientException.objects.filter(
        Q(user__isnull=True) | Q(user=target, created_by_id__in=creators)
    ).values_list('kind', 'label', 'ingredient_id', 'ingredient_pattern')
    return [(kind, match_key(label), ing_id, pattern) for kind, label, ing_id, pattern in rows]


def _is_excepted(exceptions, kind, label, keyword, ing_id, ing_name):
    keys = {match_key(label), match_key(keyword)}
    for e_kind, e_key, e_ing_id, e_pattern in exceptions:
        if e_kind != kind or e_key not in keys:
            continue
        if e_ing_id is not None and e_ing_id == ing_id:
            return True
        if e_pattern and name_matches(ing_name, e_pattern):
            return True
    return False


def _rule_hits(rule, ing_id, ing_name, exceptions):
    kind, label, keywords = rule
    for kw in keywords:
        if name_matches(ing_name, kw) and not _is_excepted(exceptions, kind, label, kw, ing_id, ing_name):
            return True
    return False


def _parse_ids(recipe_ids):
    out = []
    for rid in recipe_ids or []:
        try:
            out.append(int(rid))
        except (TypeError, ValueError):
            continue
    return list(dict.fromkeys(out))


def _ingredients_by_recipe(recipe_ids):
    """``{recipe_id: [(ingredient_id, nom), ...]}`` : liste recette puis étapes, sans doublon."""
    rows = list(
        RecipeIngredient.objects.filter(recipe_id__in=recipe_ids).values_list(
            'recipe_id', 'ingredient_id', 'ingredient__name'
        )
    )
    rows += list(
        StepIngredient.objects.filter(step__recipe_id__in=recipe_ids).values_list(
            'step__recipe_id', 'ingredient_id', 'ingredient__name'
        )
    )
    out = {}
    seen = set()
    for rid, iid, name in rows:
        if (rid, iid) in seen:
            continue
        seen.add((rid, iid))
        out.setdefault(rid, []).append((iid, name or ''))
    return out


def conflict_details_by_recipe_id(recipe_ids, user, viewer=None):
    """
    Conflits des préférences de ``user`` avec chaque recette de ``recipe_ids``.

    ``viewer`` (défaut : ``user``) détermine quelles exceptions personnelles s'appliquent
    (celles posées par ``user`` + celles posées par ``viewer``).

    Retour : ``{recipe_id: {'reasons': [...], 'hits': [...]}}`` (recettes sans conflit absentes),
    ``hits`` = au plus un par raison :
    ``{'reason', 'label', 'ingredient', 'ingredient_id'}``.
    """
    rules = _preference_rules(user)
    ids = _parse_ids(recipe_ids)
    if not rules or not ids:
        return {}
    exceptions = _load_exceptions(user, viewer or user)
    ingredients = _ingredients_by_recipe(ids)

    out = {}
    for rid in ids:
        ings = ingredients.get(rid)
        if not ings:
            continue
        hits = {}
        for rule in rules:
            kind, label, _kws = rule
            if kind in hits:
                continue
            for iid, name in ings:
                if _rule_hits(rule, iid, name, exceptions):
                    hits[kind] = {'reason': kind, 'label': label, 'ingredient': name, 'ingredient_id': iid}
                    break
        if hits:
            reasons = sorted(hits, key=KIND_ORDER.get)
            out[rid] = {'reasons': reasons, 'hits': [hits[k] for k in reasons]}
    return out


def conflict_reasons_by_recipe_id(recipe_ids, user, viewer=None):
    """``{recipe_id: ['allergy', 'diet', 'dislike']}`` (sous-ensemble, ordre UX)."""
    return {rid: d['reasons'] for rid, d in conflict_details_by_recipe_id(recipe_ids, user, viewer).items()}


def strict_excluded_recipe_ids_for_user(user):
    """
    Mode strict (suggestions) : recettes exclues pour allergies + régimes, jamais les goûts.
    Préfiltre SQL (même regex que le matching Python) puis confirmation + exceptions.
    """
    rules = _preference_rules(user, kinds=('allergy', 'diet'))
    if not rules:
        return []
    q = Q()
    for kw in _usable_keywords([kw for _k, _l, kws in rules for kw in kws]):
        q |= Q(name__iregex=keyword_pattern(kw))
    exceptions = _load_exceptions(user, user)
    matched = {
        iid
        for iid, name in Ingredient.objects.filter(q).values_list('id', 'name')
        if any(_rule_hits(rule, iid, name, exceptions) for rule in rules)
    }
    if not matched:
        return []
    ids = set(RecipeIngredient.objects.filter(ingredient_id__in=matched).values_list('recipe_id', flat=True))
    ids.update(StepIngredient.objects.filter(ingredient_id__in=matched).values_list('step__recipe_id', flat=True))
    return list(ids)


def meal_plan_recipe_id_list(meal_plan):
    """IDs recettes liées au meal plan (batches)."""
    ids = []
    for mprb in meal_plan.meal_plan_recipe_batches.all():
        rb = getattr(mprb, 'recipe_batch', None)
        if rb and getattr(rb, 'recipe_id', None):
            ids.append(rb.recipe_id)
    return list(dict.fromkeys(ids))


def meal_plan_dietary_flag_summary(meal_plan, request_user):
    """
    Indicateurs compacts pour l’app (badge) : conflits goût / allergie parmi les invités actifs.
    ``request_user`` doit pouvoir voir les préférences de l’invité (complice).
    """
    from accounts.privacy import are_complices_in_network

    recipe_ids = meal_plan_recipe_id_list(meal_plan)
    if not recipe_ids:
        return False, False, False
    dislike_any = False
    allergy_any = False
    diet_any = False
    qs = MealInvitation.objects.filter(meal_plan=meal_plan).exclude(status='declined').select_related('invitee')
    for inv in qs:
        target = inv.invitee
        if not target or not are_complices_in_network(request_user, target):
            continue
        rmap = conflict_reasons_by_recipe_id(recipe_ids, target, viewer=request_user)
        for _rid, reasons in rmap.items():
            if 'dislike' in reasons:
                dislike_any = True
            if 'allergy' in reasons:
                allergy_any = True
            if 'diet' in reasons:
                diet_any = True
            if dislike_any and allergy_any and diet_any:
                return dislike_any, allergy_any, diet_any
    return dislike_any, allergy_any, diet_any


def apply_dietary_exclusion(queryset, user):
    # Par défaut, on utilise le mode strict (allergies+régimes). Les dislikes ne doivent
    # pas exclure des recettes dans les suggestions, seulement déprioriser (scoring).
    ids = strict_excluded_recipe_ids_for_user(user)
    if not ids:
        return queryset
    return queryset.exclude(id__in=ids)
