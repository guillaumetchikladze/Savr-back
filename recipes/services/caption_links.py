"""
Mots de l'auteur → recettes : passages de la légende d'un post qui nomment une de ses recettes (hl1).

« Salade d'été, cake salé et omelette : le brunch parfait » + recette « Cake salé tomates séchées et chèvre »
→ ``[{start: 15, end: 24, recipe_id: …}]`` (« cake salé ») ; l'app surligne ce passage comme le nom du plat
sur le tirage et l'ouvre au tap.

Correspondance **déterministe** (pas de LLM) :

- normalisation des mots : minuscules, sans accents (``stats.text.fold``), apostrophes / tirets = séparateurs
  (« d'été » → « d », « ete »), pluriels simples ramenés au singulier (-s / -x final des mots de plus de 3 lettres,
  « aux » → « au », même règle que ``dish_service.normalize_label``) ;
- mots vides ignorés (articles, prépositions, possessifs…, et tout mot de moins de 3 lettres ou sans lettre) ;
- un passage = une suite de mots significatifs **consécutifs dans la légende** (seuls des mots vides, espaces,
  apostrophes ou tirets entre eux ; une virgule, deux-points, un point, un emoji… coupe la suite) qui
  retrouvent des mots du titre **dans l'ordre du titre** (le titre peut en sauter : « risotto aux champignons »
  ↔ « Risotto crémeux aux champignons ») ; égalité exacte des mots normalisés (« sel » ≠ « salé » ≠ « salade ») ;
- au moins un mot d'**au moins 4 lettres** qui n'est pas générique (« recette », « maison », « facile »…) ;
- un passage d'**un seul mot** n'est retenu que si c'est le 1er mot significatif du titre (« risotto » pour
  « Risotto aux champignons », jamais « salé » de « Caramel au beurre salé ») et pas un nom de catégorie de plat
  (« salade », « tarte », « soupe », « cake »… : « salade d'été » ne renvoie pas à « Salade de lentilles ») ;
- meilleur passage d'abord (plus de mots, puis titre mieux couvert, puis le plus tôt) ; **un passage par recette**,
  jamais de chevauchement ; deux recettes ex aequo sur le même passage → personne (doute = rien).

Positions renvoyées en **unités UTF-16** sur la légende telle qu'enregistrée (indices de chaîne JavaScript :
un emoji compte 2), pour que l'app découpe ``post.comment`` sans conversion.

Calcul **à la lecture** (pas de champ stocké) : la légende se modifie (``update-content``), les recettes d'un repas
aussi (titre renommé, recette ajoutée au repas) ; un calcul à la volée reste juste sans migration ni backfill.
Il ne lit que les recettes déjà préchargées par le feed (``post_recipes``, mêmes accès que ``dish``) : 0 requête
par post ; le calcul pur (quelques dizaines de mots) est mis en cache par (légende, titres) dans le processus.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from stats.text import fold

# Lettres / chiffres (accents décomposés compris) ; apostrophes, tirets, ponctuation, emoji = séparateurs.
_TOKEN_RE = re.compile('[^\\W_](?:[^\\W_]|[\u0300-\u036f])*')
# Entre deux mots d'un même passage : seulement espaces, apostrophes, tirets.
_SOFT_GAP_RE = re.compile(r"^[\s'’ʼ`\-‐‑–]*$")

MIN_ANCHOR_LETTERS = 4
MAX_CAPTION_CHARS = 2000
MAX_RECIPES = 12

STOPWORDS = frozenset(
    (
        # articles, prépositions, conjonctions
        'le la les l un une des du de d au aux a et ou en avec sans pour par sur sous dans chez facon comme mais '
        'puis plus moins tres trop bien tout tous toute toutes peu encore '
        # possessifs, démonstratifs, pronoms, verbes courants
        'mon ma mes ton ta tes son sa ses notre nos votre vos leur leurs ce cet cette ces ca cela qui que qu quoi '
        'dont je j tu il elle on nous vous ils elles me te se moi toi lui eux c n s y m t ne pas est etait sont '
        # anglais courant dans les titres importés
        'the of and with in to for my'
    ).split()
)

# Ne comptent jamais comme le mot d'ancrage (≥ 4 lettres) d'un passage.
GENERIC_WORDS = frozenset(
    (
        'recette maison facile rapide simple express minute classique traditionnel traditionnelle revisite '
        'revisitee version healthy light leger legere special speciale meilleur meilleure parfait parfaite super '
        'delicieu delicieuse bonne petit petite grand grande mini gros grosse plat repas menu dejeuner diner brunch '
        'apero gouter dessert entree soir midi semaine weekend dimanche samedi famille familial familiale enfant '
        'vegan vegetarien vegetarienne veggie recipe easy quick best homemade'
    ).split()
)

# Catégories de plat : ancrage possible dans un passage de plusieurs mots (« cake salé »), jamais seules.
DISH_CATEGORIES = frozenset(
    (
        'salade soupe potage veloute tarte tartelette tourte quiche gateau cake sauce gratin poelee pizza burger '
        'sandwich wrap bowl tartine crepe galette curry puree compote creme mousse terrine crumble cookie biscuit '
        'muffin smoothie jus brochette boulette filet pave escalope pate riz nouille'
    ).split()
)

_WORD_MAP = {'aux': 'au'}


def _singular(word: str) -> str:
    if word in _WORD_MAP:
        return _WORD_MAP[word]
    if len(word) > 3 and word[-1] in 'sx' and not word.endswith('ss'):
        return word[:-1]
    return word


def _norm(raw: str) -> str:
    return _singular(fold(raw).replace(' ', ''))


@dataclass(frozen=True)
class _Token:
    norm: str
    start: int  # indices Python (points de code) dans la légende
    end: int
    significant: bool
    soft_before: bool  # relié au mot précédent (seulement espaces / apostrophes / tirets entre eux)


def _is_significant(norm: str) -> bool:
    return len(norm) >= 3 and norm not in STOPWORDS and not norm.isdigit()


def _tokens(text: str) -> list[_Token]:
    out: list[_Token] = []
    prev_end = 0
    for m in _TOKEN_RE.finditer(text):
        norm = _norm(m.group())
        soft = bool(out) and bool(_SOFT_GAP_RE.match(text[prev_end : m.start()]))
        out.append(_Token(norm, m.start(), m.end(), bool(norm) and _is_significant(norm), soft))
        prev_end = m.end()
    return out


def _caption_runs(tokens: list[_Token]) -> list[list[_Token]]:
    """Suites de mots significatifs reliés (mots vides, espaces, apostrophes, tirets entre eux)."""
    runs: list[list[_Token]] = []
    current: list[_Token] = []
    broken = True
    for tok in tokens:
        if not tok.soft_before:
            broken = True
        if not tok.significant:
            continue
        if broken or not current:
            if current:
                runs.append(current)
            current = [tok]
        else:
            current.append(tok)
        broken = False
    if current:
        runs.append(current)
    return runs


def _title_words(title: str) -> tuple[str, ...]:
    return tuple(t.norm for t in _tokens(title or '') if t.significant)


def _anchored(words: list[str], title: tuple[str, ...]) -> bool:
    """
    Au moins un mot d'ancrage (≥ 4 lettres, non générique). Un passage d'un seul mot doit en plus être le 1er mot
    significatif du titre (le nom du plat en français : « Risotto aux champignons », pas « salé » de « Caramel au
    beurre salé ») et pas une catégorie de plat (« salade » seule ne dit pas laquelle).
    """
    anchors = [w for w in words if len(w) >= MIN_ANCHOR_LETTERS and w not in GENERIC_WORDS]
    if not anchors:
        return False
    if len(words) == 1:
        return words[0] == title[0] and words[0] not in DISH_CATEGORIES
    return True


def _candidates(runs: list[list[_Token]], title: tuple[str, ...]):
    """Pour un titre : (n_mots, couverture, -début, i_début, i_fin) de chaque passage acceptable de la légende."""
    found = []
    if not title:
        return found
    for run in runs:
        n = len(run)
        for i in range(n):
            # plus longue suite run[i..j] retrouvée dans l'ordre du titre (le titre peut sauter des mots)
            best_j = -1
            pos = 0
            for j in range(i, n):
                try:
                    k = title.index(run[j].norm, pos)
                except ValueError:
                    break
                pos = k + 1
                best_j = j
            for j in range(best_j, i - 1, -1):
                words = [t.norm for t in run[i : j + 1]]
                if _anchored(words, title):
                    found.append((len(words), len(words) / len(title), -run[i].start, run[i].start, run[j].end))
                    break
    return found


@lru_cache(maxsize=2048)
def _compute(caption: str, recipes: tuple[tuple[int, str], ...]) -> tuple[tuple[int, int, int], ...]:
    runs = _caption_runs(_tokens(caption))
    if not runs:
        return ()
    scored = []  # (n, couverture, -début, début, fin, recipe_id)
    for recipe_id, title in recipes:
        for cand in _candidates(runs, _title_words(title)):
            scored.append((*cand, recipe_id))
    scored.sort(key=lambda c: (c[0], c[1], c[2]), reverse=True)

    taken: list[tuple[int, int, int]] = []
    done: set[int] = set()
    for n, cover, _neg, start, end, rid in scored:
        if rid in done or any(start < e and s < end for s, e, _r in taken):
            continue
        # ex aequo d'une autre recette (encore libre) sur un passage qui chevauche : doute → personne ne le prend
        tie = any(
            o_rid != rid and o_rid not in done and (o_n, o_cover) == (n, cover) and o_start < end and start < o_end
            for o_n, o_cover, _o, o_start, o_end, o_rid in scored
        )
        if tie:
            continue
        taken.append((start, end, rid))
        done.add(rid)
    taken.sort()
    return tuple(taken)


def _utf16(text: str, index: int) -> int:
    return len(text[:index].encode('utf-16-le')) // 2


def caption_links(caption: str | None, recipes) -> list[dict]:
    """
    ``[{start, end, recipe_id}]`` (UTF-16, triés) des passages de ``caption`` qui nomment une des ``recipes``
    (objets avec ``id`` et ``title``, ou couples ``(id, title)``). [] si rien de sûr.
    """
    if not caption or not caption.strip():
        return []
    pairs = []
    seen = set()
    for r in recipes or ():
        rid, title = r if isinstance(r, tuple) else (getattr(r, 'id', None), getattr(r, 'title', None))
        if rid is None or not title or rid in seen:
            continue
        seen.add(rid)
        pairs.append((int(rid), str(title)))
        if len(pairs) >= MAX_RECIPES:
            break
    if not pairs:
        return []
    text = caption[:MAX_CAPTION_CHARS]
    return [
        {'start': _utf16(caption, s), 'end': _utf16(caption, e), 'recipe_id': rid}
        for s, e, rid in _compute(text, tuple(pairs))
    ]


def _cached_photo_recipes(post) -> list:
    """Recettes des photos (anciens posts multi-batches) seulement si déjà préchargées : jamais de requête."""
    from ..models import PostPhoto, RecipeBatch

    cache = getattr(post, '_prefetched_objects_cache', None) or {}
    photos = cache.get('photos')
    if photos is None:
        return []
    out = []
    for photo in photos:
        if not photo.recipe_batch_id or not PostPhoto.recipe_batch.is_cached(photo):
            continue
        batch = photo.recipe_batch
        if batch is not None and batch.recipe_id and RecipeBatch.recipe.is_cached(batch):
            out.append(batch.recipe)
    return out


def post_caption_links(post) -> list[dict]:
    """``caption_links`` d'un post : recettes de couverture + du repas (+ photos préchargées). Mémoïsé sur l'objet."""
    memo = getattr(post, '_caption_links_memo', None)
    if memo is not None:
        return memo
    from ..serializers import post_recipes

    caption = post.comment or ''
    links = caption_links(caption, [*post_recipes(post), *_cached_photo_recipes(post)]) if caption.strip() else []
    post._caption_links_memo = links
    return links
