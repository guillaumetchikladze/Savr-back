"""
Couleur des végétaux courants (badges « Arc-en-ciel » et « Monochrome orange »).

Table statique, sans réseau ni LLM : nom d'ingrédient « plié » (`stats.text.fold` : minuscules, sans accents,
ponctuation → espace) → couleur dominante de ce qu'on voit dans l'assiette. Première règle qui correspond gagne
(ordre = priorité : les variétés colorées avant le nom générique, ex. « poivron jaune » avant « poivron »).

Couleurs de l'arc-en-ciel : rouge, orange, jaune, vert, violet. `blanc` et `brun` existent pour qu'un plat
« tout orange » ne compte pas l'oignon ou le champignon comme orange. Un nom absent de la table = couleur
inconnue : jamais comptée dans l'arc-en-ciel, et elle empêche le « tout orange » (on ne devine pas).

Pour ajouter un végétal : une ligne `(motif, couleur)` ci-dessous ; le motif est une regex sur le nom plié,
ancrée par `\\b` (le nom peut contenir d'autres mots : « tomates cerises bio »).
"""

from __future__ import annotations

import re

from .text import fold

RAINBOW = ('rouge', 'orange', 'jaune', 'vert', 'violet')

_RULES: tuple[tuple[str, str], ...] = (
    # --- Variétés colorées (prioritaires) ---
    (r'poivrons? (rouges?|rouge)', 'rouge'),
    (r'poivrons? (jaunes?)', 'jaune'),
    (r'poivrons? (orange)', 'orange'),
    (r'poivrons? (verts?)', 'vert'),
    (r'oignons? rouges?', 'violet'),
    (r'oignons? (nouveaux?|verts?)|cebettes?|ciboules?', 'vert'),
    (r'choux? rouges?', 'violet'),
    (r'choux? fleurs?', 'blanc'),
    (r'chou romanesco|romanesco', 'vert'),
    (r'tomates? (vertes?)', 'vert'),
    (r'tomates? (jaunes?)', 'jaune'),
    (r'courgettes? jaunes?', 'jaune'),
    (r'carottes? (violettes?|pourpres?)', 'violet'),
    (r'carottes? jaunes?', 'jaune'),
    (r'pommes? de terre|patates?(?! douces?)', 'jaune'),
    (r'patates? douces?', 'orange'),
    (r'haricots? verts?', 'vert'),
    (r'haricots? (rouges?)', 'rouge'),
    (r'haricots? (blancs?|cocos?|lingots?)', 'blanc'),
    (r'haricots? noirs?', 'violet'),
    (r'lentilles? (corail)', 'orange'),
    (r'lentilles? (vertes?|du puy)', 'vert'),
    (r'raisins? (noirs?|rouges?)', 'violet'),
    (r'raisins? (blancs?|verts?)', 'vert'),
    (r'pommes? (vertes?|granny)', 'vert'),
    (r'citrons? verts?|limes?', 'vert'),
    (r'piments? verts?|jalapenos?', 'vert'),
    (r'piments? (rouges?|oiseau)|piment d espelette|harissa', 'rouge'),
    (r'figues?', 'violet'),
    # --- Rouge ---
    (r'tomates?|tomates? cerises?|coulis de tomates?|concentre de tomates?', 'rouge'),
    (r'poivrons?', 'rouge'),
    (r'radis', 'rouge'),
    (r'fraises?|framboises?|groseilles?|cerises?|canneberges?|cranberr(y|ies)|grenades?|pasteques?', 'rouge'),
    (r'piments?', 'rouge'),
    (r'pommes?', 'rouge'),
    (r'rhubarbe', 'rouge'),
    # --- Orange ---
    (r'carottes?', 'orange'),
    (r'courges?|butternut|potirons?|potimarrons?|citrouilles?|giraumon', 'orange'),
    (r'oranges?|clementines?|mandarines?|abricots?|mangues?|papayes?|kakis?|melons?|nectarines?|peches?', 'orange'),
    (r'curcuma frais', 'orange'),
    # --- Jaune ---
    (r'mais', 'jaune'),
    (r'citrons?', 'jaune'),
    (r'bananes?|ananas|pamplemousses?|coings?|mirabelles?|fruits? de la passion|physalis', 'jaune'),
    (r'pois chiches?|pois casses?', 'jaune'),
    (r'courgettes? spaghetti|courge spaghetti', 'jaune'),
    # --- Vert ---
    (r'courgettes?|concombres?|cornichons?', 'vert'),
    (r'brocolis?|choux? de bruxelles|choux? (verts?|frises?|kale)|kale|pak choi|bok choy|choux?', 'vert'),
    (r'epinards?|blettes?|bettes?|salades?|laitues?|roquette|mache|cresson|endives?|frisee|batavia|sucrine', 'vert'),
    (r'petits? pois|pois gourmands?|feves?|edamame|asperges? vertes?|asperges?', 'vert'),
    (r'avocats?|kiwis?|poires?|olives? vertes?|olives?', 'vert'),
    (r'poireaux?|celeris?( branche)?|fenouils?|artichauts?|okras?|gombos?', 'vert'),
    (r'persil|basilic|coriandre|menthe|ciboulette|aneth|estragon|cerfeuil|sauge|romarin|thym frais', 'vert'),
    (r'pistaches?|petits? pois', 'vert'),
    # --- Violet ---
    (r'aubergines?', 'violet'),
    (r'betteraves?', 'violet'),
    (r'myrtilles?|mures?|cassis|prunes?|quetsches?|pruneaux?|raisins?', 'violet'),
    (r'olives? noires?', 'violet'),
    (r'echalotes?', 'violet'),
    # --- Blanc / brun (jamais orange, hors arc-en-ciel) ---
    (r'oignons?|ail|gousses? d ail|navets?|panais|topinambours?|celeri rave|chou rave', 'blanc'),
    (r'champignons?|cepes?|girolles?|shiitakes?|pleurotes?|morilles?|truffes?', 'brun'),
    (r'riz|pates|semoule|quinoa|boulgour|pain|farine|noix|noisettes?|amandes?|chataignes?|marrons?', 'brun'),
    (r'lentilles?|haricots?', 'brun'),
    (r'gingembre', 'jaune'),
    (r'noix de coco|coco', 'blanc'),
)

_COMPILED: tuple[tuple[re.Pattern, str], ...] = tuple((re.compile(rf'\b(?:{p})\b'), c) for p, c in _RULES)


def colour_of(name: str | None) -> str | None:
    """Couleur d'un ingrédient (nom brut ou plié), None si inconnue."""
    folded = fold(name)
    if not folded:
        return None
    for pattern, colour in _COMPILED:
        if pattern.search(folded):
            return colour
    return None
