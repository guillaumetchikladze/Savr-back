"""
Filtrage des recettes selon allergies / goûts.

Stratégie en plusieurs couches (complémentaires) :

1. **Table `DietaryIngredientMatch`** : correspondances explicites libellé → sous-chaînes
   dans les noms d’ingrédients. Haute précision, facile à faire évoluer en admin / migrations.

2. **Texte libre utilisateur** : chaque libellé sans entrée en base est utilisé tel quel
   en recherche `icontains` (avec variante sans accents via `unidecode`).

3. **Similarité vectorielle (optionnelle)** : les `Ingredient` ont un champ `embedding`.
   On embed chaque libellé utilisateur et on récupère les ingrédients dont la distance
   cosinus est sous un seuil. Cela aide pour :
   - noms d’ingrédients rares ou nouveaux qui ne matchent aucun mot-clé ;
   - formulations proches sémantiquement (« intolérance au blé » vs « gluten ») ;
   - limites : faux positifs possibles si le seuil est trop large ; ingrédients sans
     embedding ne passent que par (1) et (2).

Réglages Django (optionnels, voir `savr_back/settings.py`) :
- `DIETARY_SEMANTIC_MATCHING` : activer la couche sémantique (défaut True).
- `DIETARY_SEMANTIC_MAX_DISTANCE` : distance cosinus max (0 = identique, 2 = opposé).
- `DIETARY_SEMANTIC_MAX_INGREDIENTS_PER_LABEL` : plafond de candidats par libellé.

Améliorations possibles côté produit : tagger les ingrédients avec les 14 allergènes UE,
revue manuelle des faux positifs, signalement utilisateur (« cette recette aurait dû
être filtrée »).
"""
import logging
import re

from django.conf import settings
from django.db.models import Q
from pgvector.django import CosineDistance
from unidecode import unidecode

from .models import (
    DietaryIngredientMatch,
    Ingredient,
    MealInvitation,
    PreferenceMapping,
    RecipeIngredient,
    StepIngredient,
    UserPreferenceMapping,
)

logger = logging.getLogger(__name__)


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


def _regime_keywords_from_user(user):
    regimes = _regimes_from_user(user)
    kws = []
    for r in regimes:
        kws.extend(REGIME_KEYWORDS.get(r, []))
    return _normalize_label_strings(kws)


def _labels_from_user(user):
    if not user or not getattr(user, 'is_authenticated', False):
        return []
    fd = getattr(user, 'food_dislikes', None) or []
    al = getattr(user, 'allergies', None) or []
    merged = list(fd) + list(al)
    # Compat historique: `is_vegetarian` est converti en régime via `_regimes_from_user`.
    merged = list(merged) + list(_regime_keywords_from_user(user))
    return _normalize_label_strings(merged)


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
            DietaryIngredientMatch.objects.filter(preference_label=lbl).values_list(
                'match_keyword', flat=True
            )
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
        if low in {"blanc d'oeuf", "blanc d’œuf"}:
            expanded.append("blanc d'oeufs")
            expanded.append("blanc d’œufs")
        if low in {"jaune d'oeuf", "jaune d’œuf"}:
            expanded.append("jaune d'oeufs")
            expanded.append("jaune d’œufs")

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


def keywords_for_user_preference_labels(user, kind, labels, include_pending_global=False):
    """
    Mots-clés pour une liste de labels d'un type (allergy|dislike), en combinant:
    - override user (fork) si présent
    - mapping communautaire (validated, + pending optionnel)
    - table historique DietaryIngredientMatch comme source "validée" (si existe)

    Performance: batch queries.
    """
    labels = _normalize_label_strings(labels)
    if not labels:
        return []
    kind = (kind or '').strip().lower()
    if kind not in {'allergy', 'dislike'}:
        return keywords_for_labels(labels)

    # 1) overrides user
    user_map = {}
    if user and getattr(user, 'is_authenticated', False):
        for row in UserPreferenceMapping.objects.filter(user=user, kind=kind, label__in=labels).values(
            'label', 'keywords'
        ):
            user_map[row['label']] = _normalize_keywords_list(row.get('keywords') or [])

    # 2) global mappings
    statuses = ['validated']
    if include_pending_global:
        statuses.append('pending')
    global_map = {}
    for row in PreferenceMapping.objects.filter(kind=kind, status__in=statuses, label__in=labels).values(
        'label', 'keywords', 'status'
    ):
        global_map[row['label']] = _normalize_keywords_list(row.get('keywords') or [])

    # 3) legacy match table (validated-like)
    legacy_map = {}
    # One query instead of N queries:
    for row in DietaryIngredientMatch.objects.filter(preference_label__in=labels).values(
        'preference_label', 'match_keyword'
    ):
        legacy_map.setdefault(row['preference_label'], []).append(row['match_keyword'])

    out = []
    for lbl in labels:
        if lbl in user_map and user_map[lbl]:
            out.extend(user_map[lbl])
            continue
        if lbl in global_map and global_map[lbl]:
            out.extend(global_map[lbl])
            continue
        if lbl in legacy_map and legacy_map[lbl]:
            out.extend(_normalize_keywords_list(legacy_map[lbl]))
            continue
        out.extend(keywords_for_labels([lbl]))
    return _normalize_keywords_list(out)


def keywords_for_user_labels(user):
    """
    Mots-clés pour la recherche textuelle :
    - lignes DietaryIngredientMatch pour chaque libellé ;
    - sinon le libellé lui-même (texte libre).
    """
    return keywords_for_labels(_labels_from_user(user))


def build_ingredient_q(keywords):
    """icontains sur le nom + variante sans accents quand elle diffère."""
    q = Q()
    found = False
    for kw in keywords:
        t = (kw or '').strip()
        if len(t) < 2:
            continue
        found = True
        q |= Q(ingredient__name__icontains=t)
        try:
            alt = unidecode(t)
            if alt and alt.lower() != t.lower():
                q |= Q(ingredient__name__icontains=alt)
        except Exception:
            pass
    if not found:
        return None
    return q


def build_ingredient_q_safe_word_boundary(keywords):
    """
    Match "safe" par mots entiers via regex, pour limiter les faux positifs
    (ex. `lait` ne doit pas matcher `laitue`).

    On utilise une regex “portable” (SQLite tests + Postgres prod) basée sur des
    frontières non-alphanumériques.
    """
    q = Q()
    found = False
    for kw in keywords:
        t = (kw or '').strip()
        # On évite les mots trop courts en strict (souvent trop ambigus)
        if len(t) < 3:
            continue
        found = True
        # Frontière unicode simple (lettres latines + chiffres + underscore + œ/Œ).
        # Objectif: éviter `lait` -> `laitue` sans dépendre de \\m/\\M (pas portable).
        boundary = r'[^0-9A-Za-zÀ-ÖØ-öø-ÿœŒ_]'
        try:
            pat = r'(^|' + boundary + r')' + re.escape(t) + r'(' + boundary + r'|$)'
            q |= Q(ingredient__name__iregex=pat)
        except Exception:
            continue
        try:
            alt = unidecode(t)
            if alt and alt.lower() != t.lower() and len(alt) >= 3:
                pat2 = r'(^|' + boundary + r')' + re.escape(alt) + r'(' + boundary + r'|$)'
                q |= Q(ingredient__name__iregex=pat2)
        except Exception:
            pass
    if not found:
        return None
    return q


def _semantic_ingredient_ids(labels):
    """
    Ingrédients dont l’embedding est proche du libellé utilisateur (même embedding API
    que le reste du projet). Nécessite EMBEDDING_API_* et embeddings en base.
    """
    if not getattr(settings, 'DIETARY_SEMANTIC_MATCHING', True):
        return []
    if not labels:
        return []

    try:
        from .services.ingredient_matcher import get_batch_embeddings
    except Exception as e:
        logger.debug('dietary semantic: import ingredient_matcher failed: %s', e)
        return []

    vectors = get_batch_embeddings(list(labels), input_type='query')
    max_d = float(getattr(settings, 'DIETARY_SEMANTIC_MAX_DISTANCE', 0.42))
    max_per = int(getattr(settings, 'DIETARY_SEMANTIC_MAX_INGREDIENTS_PER_LABEL', 120))

    out = set()
    for label, vec in zip(labels, vectors):
        if not vec:
            continue
        try:
            qs = (
                Ingredient.objects.exclude(embedding__isnull=True)
                .annotate(d=CosineDistance('embedding', vec))
                .filter(d__lte=max_d)
                .order_by('d')[:max_per]
            )
            out.update(qs.values_list('id', flat=True))
        except Exception as e:
            logger.warning('dietary semantic: label=%r error=%s', label, e)
    return list(out)


def excluded_recipe_ids_for_labels(labels):
    """
    IDs de recettes à exclure pour une liste de libellés (allergies ou goûts seuls) :
    même logique texte + sémantique que l’union historique.
    """
    if not labels:
        return []

    recipe_ids = set()

    kws = keywords_for_labels(labels)
    if kws:
        q_kw = build_ingredient_q(kws)
        if q_kw is not None:
            ri = RecipeIngredient.objects.filter(q_kw).values_list('recipe_id', flat=True)
            si = StepIngredient.objects.filter(q_kw).values_list('step__recipe_id', flat=True)
            recipe_ids.update(ri)
            recipe_ids.update(si)

    sem_ids = _semantic_ingredient_ids(labels)
    if sem_ids:
        ri2 = RecipeIngredient.objects.filter(ingredient_id__in=sem_ids).values_list(
            'recipe_id', flat=True
        )
        si2 = StepIngredient.objects.filter(ingredient_id__in=sem_ids).values_list(
            'step__recipe_id', flat=True
        )
        recipe_ids.update(ri2)
        recipe_ids.update(si2)

    return list(recipe_ids)


def _recipe_ids_matching_keywords_safe(keywords, semantic=False):
    """
    Recettes dont les ingrédients matchent une liste de mots-clés via regex "safe"
    (mots entiers / frontières non-alphanumériques).
    """
    if not keywords:
        return []

    recipe_ids = set()
    q_kw = build_ingredient_q_safe_word_boundary(keywords)
    if q_kw is not None:
        ri = RecipeIngredient.objects.filter(q_kw).values_list('recipe_id', flat=True)
        si = StepIngredient.objects.filter(q_kw).values_list('step__recipe_id', flat=True)
        recipe_ids.update(ri)
        recipe_ids.update(si)

    if semantic:
        sem_ids = _semantic_ingredient_ids(keywords)
        if sem_ids:
            ri2 = RecipeIngredient.objects.filter(ingredient_id__in=sem_ids).values_list(
                'recipe_id', flat=True
            )
            si2 = StepIngredient.objects.filter(ingredient_id__in=sem_ids).values_list(
                'step__recipe_id', flat=True
            )
            recipe_ids.update(ri2)
            recipe_ids.update(si2)

    return list(recipe_ids)


def excluded_recipe_ids_for_user(user):
    """
    IDs de recettes à exclure : union des matchs **texte** et **sémantiques** sur les
    ingrédients (liste recette + ingrédients d’étapes).
    """
    labels = _labels_from_user(user)
    return excluded_recipe_ids_for_labels(labels)


def strict_excluded_recipe_ids_for_user(user):
    """
    Mode strict pour suggestions:
    - Exclut uniquement allergies + régimes (jamais les dislikes)
    - Matching texte "safe" par mots entiers (anti faux-positifs)
    - Matching sémantique désactivé par défaut (trop risqué en strict)
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return []

    al_labels = _allergy_labels_from_user(user)
    regime_kws = _regime_keywords_from_user(user)
    # Allergies strict: uniquement mappings validés + overrides user.
    allergy_kws = keywords_for_user_preference_labels(user, 'allergy', al_labels, include_pending_global=False)
    strict_kws = _normalize_label_strings(list(allergy_kws) + list(regime_kws))
    if not strict_kws:
        return []

    return _recipe_ids_matching_keywords_safe(
        strict_kws,
        semantic=bool(getattr(settings, 'DIETARY_STRICT_SEMANTIC_MATCHING', False)),
    )


def conflicting_recipe_ids_for_user(recipe_ids, user):
    """
    Sous-ensemble de ``recipe_ids`` qui seraient exclues par les préférences de ``user``
    (même logique que ``excluded_recipe_ids_for_user``, restreinte à une liste).
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return []
    excl = set(excluded_recipe_ids_for_user(user))
    out = []
    for rid in recipe_ids:
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            continue
        if rid in excl:
            out.append(rid)
    return out


def conflict_reasons_by_recipe_id(recipe_ids, user):
    """
    Pour chaque id dans ``recipe_ids`` en conflit avec les préférences de ``user``,
    indique si le match vient des **allergies**, des **goûts** (n’aime pas), ou les deux.

    Retour : dict ``{ recipe_id: ['allergy', 'dislike'] }`` (valeurs triées allergies d’abord).
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return {}

    al_labels = _allergy_labels_from_user(user)
    dl_labels = _dislike_labels_from_user(user)
    regimes = _regimes_from_user(user)

    # Allergies: fort signal, validated only.
    allergy_kws = keywords_for_user_preference_labels(user, 'allergy', al_labels, include_pending_global=False)
    ex_a = set(_recipe_ids_matching_keywords_safe(allergy_kws, semantic=False)) if allergy_kws else set()

    # Régimes: strict-safe (fort signal), basé sur keywords conservateurs.
    regime_kws = _regime_keywords_from_user(user)
    ex_r = set(_recipe_ids_matching_keywords_safe(regime_kws, semantic=False)) if regime_kws else set()

    # Dislikes: signal soft. On inclut pending global pour améliorer la couverture,
    # mais ça ne doit jamais être utilisé en strict exclusion.
    dislike_kws = keywords_for_user_preference_labels(user, 'dislike', dl_labels, include_pending_global=True)
    ex_d = set(excluded_recipe_ids_for_labels(dislike_kws)) if dislike_kws else set()

    out = {}
    for rid in recipe_ids:
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            continue
        reasons = []
        if rid in ex_a:
            reasons.append('allergy')
        if rid in ex_r:
            reasons.append('diet')
        if rid in ex_d:
            reasons.append('dislike')
        if reasons:
            # Ordre UX: allergy > diet > dislike
            order = {'allergy': 0, 'diet': 1, 'dislike': 2}
            out[rid] = sorted(reasons, key=lambda x: order.get(x, 99))
    return out


def _ingredient_objects_for_recipe(recipe_id):
    """Ingrédients uniques (liste recette + étapes)."""
    try:
        rid = int(recipe_id)
    except (TypeError, ValueError):
        return []
    seen = set()
    out = []
    for ri in RecipeIngredient.objects.filter(recipe_id=rid).select_related('ingredient'):
        i = ri.ingredient
        if i and i.id not in seen:
            seen.add(i.id)
            out.append(i)
    for si in StepIngredient.objects.filter(step__recipe_id=rid).select_related('ingredient'):
        i = si.ingredient
        if i and i.id not in seen:
            seen.add(i.id)
            out.append(i)
    return out


def _text_hit_for_labels(ingredients, labels, reason_key):
    """Premier match texte libellé → nom d’ingrédient."""
    for lbl in labels:
        kws = keywords_for_labels([lbl])
        for ing in ingredients:
            n = ing.name or ''
            n_low = n.lower()
            for kw in kws:
                t = (kw or '').strip()
                if len(t) < 2:
                    continue
                if t.lower() in n_low:
                    return {'reason': reason_key, 'label': lbl, 'ingredient': ing.name}
                try:
                    alt = unidecode(t)
                    if alt and alt.lower() != t.lower() and alt.lower() in n_low:
                        return {'reason': reason_key, 'label': lbl, 'ingredient': ing.name}
                except Exception:
                    pass
    return None


def _semantic_hit_for_labels(ingredients, labels, reason_key):
    """Premier match sémantique par libellé (chaque libellé embed séparément)."""
    ing_by_id = {i.id: i for i in ingredients}
    for lbl in labels:
        sem_ids = set(_semantic_ingredient_ids([lbl]))
        for iid in sem_ids:
            if iid in ing_by_id:
                return {'reason': reason_key, 'label': lbl, 'ingredient': ing_by_id[iid].name}
    return None


def hits_for_recipe_reasons(recipe_id, reasons, user):
    """
    Détail utilisable en UI : libellé déclaré + ingrédient repéré (max 1 par type allergy/dislike).
    """
    if not reasons:
        return []
    ingredients = _ingredient_objects_for_recipe(recipe_id)
    out = []
    if 'allergy' in reasons:
        labs = _allergy_labels_from_user(user)
        h = _text_hit_for_labels(ingredients, labs, 'allergy') or _semantic_hit_for_labels(
            ingredients, labs, 'allergy'
        )
        if h:
            out.append(h)
    if 'diet' in reasons:
        regimes = _regimes_from_user(user)
        # Best-effort: show a regime name and a matched ingredient.
        # We only do a text-safe hit here (no semantic) to keep credibility.
        boundary = r'[^0-9A-Za-zÀ-ÖØ-öø-ÿœŒ_]'
        found = False
        for r in regimes:
            kws = _normalize_label_strings(REGIME_KEYWORDS.get(r, []))
            for ing in ingredients:
                n = (ing.name or '')
                for kw in kws:
                    t = (kw or '').strip()
                    if len(t) < 3:
                        continue
                    try:
                        if re.search(
                            r'(^|' + boundary + r')' + re.escape(t) + r'(' + boundary + r'|$)',
                            n,
                            flags=re.IGNORECASE,
                        ):
                            out.append({'reason': 'diet', 'label': r, 'ingredient': ing.name})
                            found = True
                            break
                    except Exception:
                        continue
                if found:
                    break
            if found:
                break
        # Ignore if no hit found
    if 'dislike' in reasons:
        labs = _dislike_labels_from_user(user)
        h = _text_hit_for_labels(ingredients, labs, 'dislike') or _semantic_hit_for_labels(
            ingredients, labs, 'dislike'
        )
        if h:
            out.append(h)
    return out


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
    qs = MealInvitation.objects.filter(meal_plan=meal_plan).exclude(status='declined').select_related(
        'invitee'
    )
    for inv in qs:
        target = inv.invitee
        if not target or not are_complices_in_network(request_user, target):
            continue
        rmap = conflict_reasons_by_recipe_id(recipe_ids, target)
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
