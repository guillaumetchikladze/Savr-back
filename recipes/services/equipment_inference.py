"""
Détection des équipements dans le texte d'une recette (titre + étapes) — module partagé, pur (aucune base).

Utilisé par :
- le plan de bataille (`battle_plan.analyze_step` : ressource d'une étape = four / plaques / frigo) ;
- `Recipe.required_equipment` (`equipment_service.refresh_recipe_equipment`, commande `infer_recipe_equipment`).

Mots-clés sur texte « fold » (minuscules, sans accents ni ponctuation : « Préchauffez le four à 180 °C » →
« prechauffez le four a 180 c »). Règles volontairement prudentes : on préfère rater un équipement « mou » (blender)
que d'en inventer un « dur » (four) qui exclurait le plat des suggestions.
"""

from __future__ import annotations

import re

from stats.text import fold

# ---------------------------------------------------------------------------
# Ressources du plan de bataille (historique : étaient dans battle_plan.py)
# ---------------------------------------------------------------------------
PREHEAT = re.compile(r'\bprechauff')
OVEN = re.compile(r'\benfourn\w*|\bfours?\b|\bgratin(?:er|ez)\b|\bsous le grill\b|\bau grill\b')
OUT_OF_OVEN = re.compile(r'\bsort\w*\s+(?:\w+\s+){0,3}du four\b|\bsortie du four\b')
MICROWAVE = re.compile(r'\bmicro\s*ondes?\b')
HOB = re.compile(r'\b(poele|casserole|sauteuse|wok|faitout|cocotte|feu|plaque)s?\b')
FRIDGE = re.compile(r'\b(refrigerat\w*|frigo|au frais|congelat\w*)\b')


def step_resource(folded: str) -> tuple[str | None, bool]:
    """(ressource du plan de bataille : 'oven' | 'fridge' | 'hob' | None, four utilisé) pour un texte « fold »."""
    oven = bool(OVEN.search(folded)) and not MICROWAVE.search(folded)
    if oven and OUT_OF_OVEN.search(folded) and 'enfourn' not in folded:
        oven = False
    if oven:
        return 'oven', True
    if FRIDGE.search(folded):
        return 'fridge', False
    if HOB.search(folded):
        return 'hob', False
    return None, False


# ---------------------------------------------------------------------------
# Équipements requis par une recette
# ---------------------------------------------------------------------------
_AIRFRYER = re.compile(r'\b(air ?fryer|friteuse (?:sans huile|a air)|actifry)\b')
_COCOTTE_MINUTE = re.compile(r'\b(cocotte minute|autocuiseur(?! vapeur)|sous pression|soupape)\b')
_STEAMER = re.compile(r'\b(cuiseur vapeur|cuit vapeur|couscoussier|autocuiseur vapeur)\b')
_ROBOT_CUISEUR = re.compile(
    r'\b(thermomix|cookeo|monsieur cuisine|companion|robot cuiseur|robot multi ?fonctions?|varoma|'
    r'sens inverse|vitesse \d+(?: \d+)?)\b'
)
_ROBOT_PATISSIER = re.compile(
    r'\b(robot patissier|kitchenaid|kenwood|robot petrin|crochet petrisseur|batteur electrique|fouet electrique|'
    r'feuille du robot|fouet du robot|cuve du robot)\b'
)
_BLENDER = re.compile(r'\b(blender|smoothies?|milk ?shakes?)\b')
_IMMERSION = re.compile(
    r'\b(mixeur plongeant|mixer plongeant|plongeur|mixe[rz]?|mixee?s?|mixant|moulinez|mouliner|mixeur)\b'
)
_VELOUTE = re.compile(r'\bvelout\w*\b')
_BBQ = re.compile(r'\b(barbecue|bbq|plancha|braises|grill(?:ez|er)? au charbon)\b')
_COCOTTE_FONTE = re.compile(r'\b(cocotte en fonte|cocotte fonte|staub|le creuset|doufeu|cocotte lutee)\b')
_BREAD_MACHINE = re.compile(r'\bmachine a pain\b')
_ICE_CREAM = re.compile(r'\b(sorbetiere|turbine[rz]?|turbinee?s?|machine a glaces?)\b')
_WAFFLE_APPLIANCE = re.compile(r'\b(gaufrier|appareil a gaufres?|moules? a gaufres?)\b')
_WAFFLE_TITLE = re.compile(r'\bgaufres?\b')
_RACLETTE_APPLIANCE = re.compile(
    r'\b(appareil a (?:raclette|fondue)|caquelon|pierrade|service a fondue|poelons? a raclette|'
    r'fondue (?:savoyarde|bourguignonne|chinoise|au fromage))\b'
)
_RACLETTE_TITLE = re.compile(r'\b(raclette|fondue (?:savoyarde|bourguignonne|chinoise|au fromage))\b')
_WOK = re.compile(r'\bwok\b')
_OVEN_TITLE = re.compile(r'\b(gratins?|au four|soufflee?s?|lasagnes?|cakes?|clafoutis|crumble)\b')
_HOB_VESSEL = re.compile(r'\b(poele|casserole|sauteuse|faitout|plaque)s?\b')
_HOB_ACTION = re.compile(
    r'\b(feu (?:doux|vif|moyen)|a feu|faire revenir|faites revenir|porte[rz]? a ebullition|faire bouillir|'
    r'mijote[rz]?|faire fondre|faites fondre|faire sauter|faites sauter|saisi(?:r|ssez)|poele[rz]|cocotte)\b'
)
# « Huile à 180 °C », « caramel à 160 °C » : pas un four.
_NOT_BAKING = re.compile(r'\b(friture|friteuse|frire|huile|caramel|sucre|sirop|thermometre)\b')
_THERMOSTAT = re.compile(r'\bth(?:ermostat)? ?\d{1,2}\b')
_BAKING_TEMP = re.compile(r'(\d{3})\s*(?:°|degr)')
_MICROWAVE_OVEN = re.compile(r'\bfours? (?:a )?micro ?ondes?\b')


def _uses_oven(folded: str, raw: str) -> bool:
    folded = _MICROWAVE_OVEN.sub(' ', folded)  # « four à micro-ondes » = micro-ondes
    if _AIRFRYER.search(folded) or _ROBOT_CUISEUR.search(folded):
        return False  # « à l'airfryer (ou au four) » : l'airfryer suffit
    if OVEN.search(folded) or _THERMOSTAT.search(folded) or (PREHEAT.search(folded) and not _HOB_VESSEL.search(folded)):
        return True  # « sortir du four » compris : le plat est passé au four
    if _NOT_BAKING.search(folded) or _HOB_VESSEL.search(folded):
        return False
    return any(140 <= int(m.group(1)) <= 300 for m in _BAKING_TEMP.finditer(raw or ''))


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r'(?<=[.!?;\n])\s+', text or '') if s.strip()]


_SIMPLE_PATTERNS = (
    ('cocotte_minute', _COCOTTE_MINUTE),
    ('autocuiseur_vapeur', _STEAMER),
    ('robot_cuiseur', _ROBOT_CUISEUR),
    ('robot_patissier', _ROBOT_PATISSIER),
    ('blender', _BLENDER),
    ('barbecue_plancha', _BBQ),
    ('cocotte_fonte', _COCOTTE_FONTE),
    ('machine_a_pain', _BREAD_MACHINE),
    ('sorbetiere', _ICE_CREAM),
    ('wok', _WOK),
    ('gaufrier', _WAFFLE_APPLIANCE),
    ('appareil_raclette_fondue', _RACLETTE_APPLIANCE),
    ('micro_ondes', MICROWAVE),
    ('airfryer', _AIRFRYER),
)


def infer_equipment(title: str, steps: list[tuple[str, str]], *, servings: int | None = None) -> tuple[list[str], dict]:
    """
    Équipements requis par une recette, d'après son titre et ses étapes `[(titre, instruction), …]`.

    Renvoie (clés triées dans l'ordre du catalogue, alternatives `{clé: [clés]}`). Alternatives propres à la
    recette (le catalogue fournit les génériques) : four → airfryer seulement pour ≤ 4 portions (au-delà : `[]`).
    """
    from recipes.services.equipment_catalog import sort_keys

    title_folded = fold(title)
    found: set[str] = set()
    vessel = action = False
    mixing = False
    for step_title, instruction in steps:
        for sentence in _sentences(f'{step_title or ""}. {instruction or ""}'):
            folded = fold(sentence)
            if not folded:
                continue
            if _uses_oven(folded, sentence):
                found.add('four')
            for key, pattern in _SIMPLE_PATTERNS:
                if pattern.search(folded):
                    found.add(key)
            vessel = vessel or bool(_HOB_VESSEL.search(folded))
            action = action or bool(_HOB_ACTION.search(folded))
            mixing = mixing or bool(_IMMERSION.search(folded))

    # Titre : indices forts seulement (« Gratin dauphinois », « Gaufres », « Raclette », « Smoothie »).
    if _OVEN_TITLE.search(title_folded) and 'airfryer' not in found:
        found.add('four')
    if _WAFFLE_TITLE.search(title_folded):
        found.add('gaufrier')
    if _RACLETTE_TITLE.search(title_folded):
        found.add('appareil_raclette_fondue')
    for key, pattern in (('barbecue_plancha', _BBQ), ('blender', _BLENDER), ('airfryer', _AIRFRYER)):
        if pattern.search(title_folded):
            found.add(key)
    if (mixing or _VELOUTE.search(title_folded)) and not found & {'blender', 'robot_cuiseur'}:
        found.add('mixeur_plongeant')

    # Robot cuiseur : il « mijote » lui-même ; plaques seulement si une poêle / casserole est citée.
    if vessel or (action and 'robot_cuiseur' not in found) or found & {'wok', 'cocotte_minute'}:
        found.add('plaques')

    alternatives: dict[str, list[str]] = {}
    if 'four' in found:
        alternatives['four'] = ['airfryer'] if (servings or 4) <= 4 else []
    return sort_keys(found), alternatives
