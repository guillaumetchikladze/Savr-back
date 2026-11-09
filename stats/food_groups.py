"""
Groupes alimentaires (liste fermée, libellés FR) + règles « de base » (staples) et seed déterministe.

Taxonomie inspirée des groupes Ciqual (ANSES), simplifiée à 13 valeurs. Aucune donnée nutritionnelle :
les groupes ne servent qu'à des compteurs de curiosité (diversité), jamais à un jugement.

Les ingrédients « de base » (sel, poivre, huile, eau, sucre, farine, épices sèches courantes…) sont
exclus des compteurs de diversité (spec gamification §4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .text import fold

FOOD_GROUPS: dict[str, str] = {
    'legumes': 'Légumes',
    'fruits': 'Fruits',
    'legumineuses': 'Légumineuses',
    'cereales_feculents': 'Céréales & féculents',
    'viandes': 'Viandes',
    'poissons_fruits_de_mer': 'Poissons & fruits de mer',
    'oeufs': 'Œufs',
    'produits_laitiers': 'Produits laitiers',
    'noix_graines': 'Noix & graines',
    'herbes_epices': 'Herbes & épices',
    'matieres_grasses': 'Matières grasses',
    'sucres_condiments': 'Sucres & condiments',
    'autres': 'Autres',
}
FOOD_GROUP_CHOICES = list(FOOD_GROUPS.items())

# Groupes végétaux par défaut (le LLM peut nuancer ingrédient par ingrédient).
PLANT_GROUPS = frozenset({'legumes', 'fruits', 'legumineuses', 'cereales_feculents', 'noix_graines', 'herbes_epices'})


@dataclass(frozen=True)
class SeedRule:
    pattern: re.Pattern
    food_group: str
    is_plant: bool
    is_basic_staple: bool


def _r(expr: str) -> re.Pattern:
    return re.compile(expr)


# Règles sur le nom « plié » (minuscules, sans accents). Ancrées : uniquement des noms non ambigus.
# Ordre = priorité. Couvre les ingrédients les plus fréquents pour épargner des appels LLM.
SEED_RULES: tuple[SeedRule, ...] = (
    # --- De base (exclus de la diversité) ---
    SeedRule(_r(r'^(gros |fleur de )?sel( fin| gros| de guerande| marin)?$'), 'sucres_condiments', False, True),
    SeedRule(_r(r'^(sel et poivre|poivre et sel)$'), 'herbes_epices', False, True),
    SeedRule(_r(r'^poivre( noir| blanc| du moulin| moulu| en grains)?$'), 'herbes_epices', True, True),
    SeedRule(
        _r(r'^huile( d olive| de tournesol| de colza| neutre| vegetale| d arachide| de pepins de raisin)?$'),
        'matieres_grasses',
        True,
        True,
    ),
    SeedRule(_r(r'^eau( froide| chaude| tiede| bouillante| gazeuse)?$'), 'autres', False, True),
    SeedRule(_r(r'^glacons?$'), 'autres', False, True),
    SeedRule(
        _r(r'^sucre( en poudre| semoule| glace| roux| blanc| de canne| vanille)?$'), 'sucres_condiments', True, True
    ),
    SeedRule(_r(r'^cassonade$'), 'sucres_condiments', True, True),
    SeedRule(_r(r'^farine( de ble)?( t ?\d+)?$'), 'cereales_feculents', True, True),
    SeedRule(_r(r'^(maizena|fecule de mais|fecule de pomme de terre)$'), 'cereales_feculents', True, True),
    SeedRule(_r(r'^levure( chimique| boulangere| de boulanger| seche)?$'), 'autres', False, True),
    SeedRule(_r(r'^bicarbonate( de soude| alimentaire)?$'), 'autres', False, True),
    SeedRule(
        _r(r'^(bouillon|cube de bouillon|fond)( de| d)?( volaille| legumes| boeuf| poulet| veau| cube)?$'),
        'sucres_condiments',
        False,
        True,
    ),
    SeedRule(
        _r(r'^vinaigre( balsamique| de vin| de cidre| blanc| de xeres| de riz)?$'), 'sucres_condiments', True, True
    ),
    SeedRule(_r(r'^beurre( doux| demi sel| sale| mou| fondu| pommade)?$'), 'matieres_grasses', False, True),
    SeedRule(
        _r(
            r'^(cumin|paprika( fume)?|curcuma|cannelle|muscade|noix de muscade|curry|piment d espelette|herbes de provence|thym|laurier|feuilles? de laurier|origan|clous? de girofle|gingembre en poudre|quatre epices|ras el hanout|garam masala|piment en poudre|cayenne|piment de cayenne|sel de celeri|melange d epices)$'
        ),
        'herbes_epices',
        True,
        True,
    ),
    SeedRule(_r(r'^(extrait de vanille|arome vanille)$'), 'sucres_condiments', True, True),
    # --- Fréquents non « de base » ---
    SeedRule(
        _r(
            r'^(oignons?|oignons? (jaunes?|rouges?|blancs?|nouveaux?)|echalotes?|ail|gousses? d ail|carottes?|courgettes?|aubergines?|poivrons?( rouges?| verts?| jaunes?)?|tomates?( cerises?)?|poireaux?|epinards?( frais)?|brocolis?|chou fleur|choux? (vert|rouge|blanc|frise|de bruxelles)|champignons?( de paris)?|concombres?|salade|laitue|celeri( branche)?|navets?|radis|betteraves?|haricots verts|petits pois|potiron|potimarron|courge( butternut)?|butternut|fenouil|artichauts?|asperges?|patate douce|patates douces|mais|endives?|roquette|mache|cebettes?|oignons? verts?)$'
        ),
        'legumes',
        True,
        False,
    ),
    SeedRule(_r(r'^(pommes? de terre|pommes? de terre .*)$'), 'cereales_feculents', True, False),
    SeedRule(
        _r(
            r'^(citrons?( vert| jaune)?|jus de citron|pommes?|poires?|bananes?|fraises?|framboises?|myrtilles?|oranges?|mangues?|ananas|abricots?|peches?|cerises?|raisins?( secs)?|kiwis?|avocats?|figues?|prunes?|pamplemousses?|clementines?|grenades?|fruits rouges)$'
        ),
        'fruits',
        True,
        False,
    ),
    SeedRule(
        _r(
            r'^(lentilles?( vertes| corail| beluga)?|pois chiches?|haricots (rouges|blancs|noirs)|feves?|pois casses|edamame|tofu)$'
        ),
        'legumineuses',
        True,
        False,
    ),
    SeedRule(
        _r(
            r'^(riz( basmati| thai| rond| arborio| complet)?|pates|spaghetti|tagliatelles?|penne|coquillettes|fusilli|semoule|quinoa|boulgour|pain|pain de mie|baguette|chapelure|flocons d avoine|nouilles( de riz)?|polenta|gnocchis?|lasagnes?|pate feuilletee|pate brisee|pate sablee|tortillas?|ble)$'
        ),
        'cereales_feculents',
        True,
        False,
    ),
    SeedRule(
        _r(
            r'^(poulet|blancs? de poulet|cuisses? de poulet|filets? de poulet|boeuf|boeuf hache|steak hache|viande hachee|porc|lardons?|jambon( blanc| cru)?|chorizo|saucisses?|agneau|veau|dinde|canard|magret de canard|bacon|merguez)$'
        ),
        'viandes',
        False,
        False,
    ),
    SeedRule(
        _r(
            r'^(saumon( fume)?|thon|cabillaud|crevettes?|moules|colin|lieu|sardines?|maquereau|dorade|bar|calamars?|noix de saint jacques|truite)$'
        ),
        'poissons_fruits_de_mer',
        False,
        False,
    ),
    SeedRule(_r(r'^(oeufs?|jaunes? d oeufs?|blancs? d oeufs?)$'), 'oeufs', False, False),
    SeedRule(
        _r(
            r'^(lait( entier| demi ecreme| ecreme)?|creme( fraiche)?( epaisse| liquide| entiere)?|creme fraiche|yaourts?( nature| grec)?|fromage rape|parmesan|gruyere( rape)?|emmental( rape)?|mozzarella|feta|chevre|fromage de chevre|comte|ricotta|mascarpone|cheddar|reblochon|roquefort|fromage blanc|creme liquide)$'
        ),
        'produits_laitiers',
        False,
        False,
    ),
    SeedRule(
        _r(
            r'^(amandes?( effilees| en poudre)?|poudre d amandes|noix|noisettes?|noix de cajou|pignons?( de pin)?|graines de sesame|sesame|graines de courge|graines de tournesol|cacahuetes?|pistaches?|graines de chia|graines de lin)$'
        ),
        'noix_graines',
        True,
        False,
    ),
    SeedRule(
        _r(
            r'^(basilic|persil|coriandre|menthe|ciboulette|aneth|estragon|romarin|sauge|cerfeuil|gingembre( frais)?|piment|piments?( rouges?| verts?)?|citronnelle)$'
        ),
        'herbes_epices',
        True,
        False,
    ),
    SeedRule(
        _r(
            r'^(moutarde|sauce soja|ketchup|mayonnaise|miel|sirop d erable|concentre de tomates?|coulis de tomates?|sauce tomate|chocolat( noir| au lait)?|cacao( en poudre)?|nuoc mam|sauce nuoc mam|pesto|tahini|harissa)$'
        ),
        'sucres_condiments',
        False,
        False,
    ),
)


@dataclass(frozen=True)
class SeedClassification:
    food_group: str
    is_plant: bool
    is_basic_staple: bool


def seed_classify(name: str) -> SeedClassification | None:
    """Classification déterministe d'un nom d'ingrédient (None = à confier au LLM)."""
    folded = fold(name)
    if not folded:
        return None
    for rule in SEED_RULES:
        if rule.pattern.match(folded):
            return SeedClassification(rule.food_group, rule.is_plant, rule.is_basic_staple)
    return None


def is_staple_name(name: str) -> bool:
    """Fallback quand l'ingrédient n'a pas (encore) de profil : staple d'après le seed."""
    seed = seed_classify(name)
    return bool(seed and seed.is_basic_staple)
