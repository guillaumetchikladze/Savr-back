"""
Sous-recettes « simplifié / compliqué » : une ligne d'ingrédient (« pâte brisée » d'une quiche) peut pointer vers
une recette (`RecipeIngredient.sub_recipe`). Pour chaque préparation (`RecipeBatch.sub_recipe_modes`), on l'achète
(`buy` : la ligne telle quelle, comportement historique) ou on la fait maison (`make` : ingrédients et étapes de la
sous-recette). Contrat front : `.claude-work/specs/sub-recipes.md`.

- Graphe sans cycle, profondeur ≤ `MAX_DEPTH` (Quiche → Pâte brisée = profondeur 1) : `validate_link`.
- Quantité : `sub_recipe_ratio` = fournées de la sous-recette (à ses portions de base) pour la recette parente à ses
  portions de base ; mis à l'échelle comme le reste (portions du repas / portions de base).
- Choix : `{"<recipe_ingredient_id>": "make" | "buy"}` sur le batch ; absent → `default_mode` de la ligne. Les clés
  des lignes de sous-recettes imbriquées vont dans la même carte (ids uniques).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.contrib.auth.models import AbstractBaseUser
from django.db import transaction
from django.db.models import Count, Q

from recipes.models import (
    Recipe,
    RecipeBatch,
    RecipeIngredient,
    ShoppingListBatch,
    ShoppingListItem,
    ShoppingListItemQuantity,
)
from stats.text import fold

MAX_DEPTH = 3
MODE_BUY = RecipeIngredient.MODE_BUY
MODE_MAKE = RecipeIngredient.MODE_MAKE
MODES = (MODE_BUY, MODE_MAKE)
MAX_RATIO = Decimal('50')
SUGGESTIONS_MAX = 5
_GRAPH_GUARD = 12  # itérations max d'un parcours (défensif : le graphe est borné par MAX_DEPTH)
_DIFFICULTY_RANK = {'easy': 0, 'medium': 1, 'hard': 2}
_DIFFICULTY_LABEL = dict(Recipe.DIFFICULTY_CHOICES)


class SubRecipeError(Exception):
    def __init__(self, message: str, status_code: int = 400, code: str = 'invalid'):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


# ---------------------------------------------------------------------------
# Graphe
# ---------------------------------------------------------------------------
def _edges_down(recipe_ids) -> list[tuple[int, int]]:
    return list(
        RecipeIngredient.objects.filter(recipe_id__in=recipe_ids, sub_recipe__isnull=False).values_list(
            'recipe_id', 'sub_recipe_id'
        )
    )


def _edges_up(recipe_ids) -> list[tuple[int, int]]:
    return list(RecipeIngredient.objects.filter(sub_recipe_id__in=recipe_ids).values_list('sub_recipe_id', 'recipe_id'))


def _walk(start_id: int, edges) -> tuple[set[int], int]:
    """Parcours en largeur : (recettes atteintes, nombre de niveaux). Une requête par niveau."""
    seen: set[int] = set()
    frontier = {start_id}
    levels = 0
    for _ in range(_GRAPH_GUARD):
        nxt = {b for _a, b in edges(frontier)} - seen - {start_id}
        if not nxt:
            break
        levels += 1
        seen |= nxt
        frontier = nxt
    return seen, levels


def descendants(recipe_id: int) -> tuple[set[int], int]:
    """Sous-recettes (récursif) et profondeur sous `recipe_id` (0 = aucune)."""
    return _walk(recipe_id, _edges_down)


def ancestors(recipe_id: int) -> tuple[set[int], int]:
    """Recettes qui utilisent `recipe_id` (récursif) et nombre de niveaux au-dessus."""
    return _walk(recipe_id, _edges_up)


def validate_link(parent: Recipe, sub: Recipe) -> None:
    """Refuse A→A, les cycles (A→B→A) et une chaîne de plus de `MAX_DEPTH` sous-recettes."""
    if sub.id == parent.id:
        raise SubRecipeError('Une recette ne peut pas être sa propre sous-recette.', 400, 'cycle')
    below, depth_below = descendants(sub.id)
    if parent.id in below:
        raise SubRecipeError(f'« {sub.title} » utilise déjà « {parent.title} » : ça ferait une boucle.', 400, 'cycle')
    _above, depth_above = ancestors(parent.id)
    if depth_above + 1 + depth_below > MAX_DEPTH:
        raise SubRecipeError(f'{MAX_DEPTH} niveaux de sous-recettes au maximum.', 400, 'too_deep')


# ---------------------------------------------------------------------------
# Choix acheter / faire maison
# ---------------------------------------------------------------------------
def clean_modes(value) -> dict[str, str]:
    """Carte `{line_id: mode}` validée (clés entières en texte, modes connus) ; sinon SubRecipeError."""
    if value in (None, ''):
        return {}
    if not isinstance(value, dict):
        raise SubRecipeError('sub_recipe_modes doit être un objet {recipe_ingredient_id: "buy" | "make"}.')
    out: dict[str, str] = {}
    for key, mode in value.items():
        try:
            line_id = int(key)
        except (TypeError, ValueError) as exc:
            raise SubRecipeError('Clé de sub_recipe_modes invalide (id de ligne attendu).') from exc
        if mode not in MODES:
            raise SubRecipeError('Mode invalide : « buy » ou « make ».')
        out[str(line_id)] = mode
    return out


def line_mode(line: RecipeIngredient, modes: dict | None) -> str:
    if not line.sub_recipe_id:
        return MODE_BUY
    mode = (modes or {}).get(str(line.id))
    return mode if mode in MODES else (line.default_mode or MODE_BUY)


def tree_line_ids(recipe_id: int) -> set[int]:
    """Ids des lignes à sous-recette de la recette et de ses sous-recettes (toutes profondeurs)."""
    ids: set[int] = set()
    frontier, seen = {recipe_id}, {recipe_id}
    for _ in range(MAX_DEPTH + 1):
        rows = list(
            RecipeIngredient.objects.filter(recipe_id__in=frontier, sub_recipe__isnull=False).values_list(
                'id', 'sub_recipe_id'
            )
        )
        ids |= {line_id for line_id, _ in rows}
        frontier = {sub_id for _, sub_id in rows} - seen
        seen |= frontier
        if not frontier:
            break
    return ids


def accessible_batches(user: AbstractBaseUser):
    """Préparations dont l'utilisateur choisit acheter / faire maison : les siennes, ses repas, ses envies."""
    return RecipeBatch.objects.filter(
        Q(created_by=user) | Q(meal_plan_recipe_batches__meal_plan__user=user) | Q(basket_items__user=user)
    ).distinct()


def set_batch_modes(batch: RecipeBatch, modes, *, replace: bool = False) -> dict[str, str]:
    """
    Enregistre les choix du batch (fusion, ou remplacement avec `replace`) ; lignes hors de l'arbre de la recette →
    400. Si le batch est déjà dans une liste de courses, ses quantités y sont recalculées.
    """
    modes = clean_modes(modes)
    allowed = tree_line_ids(batch.recipe_id)
    unknown = [k for k in modes if int(k) not in allowed]
    if unknown:
        raise SubRecipeError('Ligne sans sous-recette dans cette recette.', 400, 'unknown_line')
    current = {} if replace else dict(batch.sub_recipe_modes or {})
    current.update(modes)
    current = {k: v for k, v in current.items() if k.isdigit() and int(k) in allowed}
    with transaction.atomic():
        changed = current != (batch.sub_recipe_modes or {})
        batch.sub_recipe_modes = current
        batch.save(update_fields=['sub_recipe_modes', 'updated_at'])
        if changed:
            resync_batch_shopping(batch)
    return current


def resync_batch_shopping(batch: RecipeBatch) -> bool:
    """
    Recalcule les quantités du batch dans sa liste (acheter ↔ faire maison change les ingrédients) en gardant ce qui
    était déjà coché (par ingrédient + groupe d'unités, plafonné à la nouvelle quantité). False si aucune liste.
    """
    from recipes.services.shopping_list_service import add_batch_ingredients_to_list

    link = ShoppingListBatch.objects.filter(recipe_batch=batch).select_related('shopping_list').first()
    if link is None:
        return False
    shopping_list = link.shopping_list
    old = ShoppingListItemQuantity.objects.filter(recipe_batch=batch, shopping_list_item__shopping_list=shopping_list)
    checked = {
        (q.shopping_list_item.ingredient_id, q.shopping_list_item.unit_group): q
        for q in old.select_related('shopping_list_item')
        if q.checked_quantity
    }
    old.delete()
    add_batch_ingredients_to_list(shopping_list, batch)
    for q in ShoppingListItemQuantity.objects.filter(
        recipe_batch=batch, shopping_list_item__shopping_list=shopping_list
    ).select_related('shopping_list_item'):
        prev = checked.get((q.shopping_list_item.ingredient_id, q.shopping_list_item.unit_group))
        if prev is None:
            continue
        q.checked_quantity = min(Decimal(str(prev.checked_quantity)), Decimal(str(q.quantity)))
        q.checked_at, q.checked_by_id = prev.checked_at, prev.checked_by_id
        q.save(update_fields=['checked_quantity', 'checked_at', 'checked_by', 'updated_at'])
    ShoppingListItem.objects.filter(shopping_list=shopping_list).annotate(qcount=Count('quantities')).filter(
        qcount=0
    ).delete()
    return True


# ---------------------------------------------------------------------------
# Expansion (liste de courses)
# ---------------------------------------------------------------------------
def expand_ingredients(recipe: Recipe, ratio: float, modes: dict | None) -> list[tuple[RecipeIngredient, float]]:
    """
    Lignes à acheter pour `recipe` à l'échelle `ratio` : une ligne « faire maison » est remplacée par les lignes de sa
    sous-recette (× `sub_recipe_ratio`), récursivement jusqu'à `MAX_DEPTH`. Une requête par recette visitée.
    """
    out: list[tuple[RecipeIngredient, float]] = []

    def visit(r_id: int, r_ratio: float, depth: int, path: frozenset[int]) -> None:
        lines = RecipeIngredient.objects.filter(recipe_id=r_id).select_related('ingredient__category', 'sub_recipe')
        for line in lines:
            sub = line.sub_recipe
            if sub is not None and depth < MAX_DEPTH and sub.id not in path and line_mode(line, modes) == MODE_MAKE:
                sub_ratio = r_ratio * float(line.sub_recipe_ratio or 1)
                visit(sub.id, sub_ratio, depth + 1, path | {sub.id})
                continue
            out.append((line, float(line.quantity or 0) * r_ratio))

    visit(recipe.id, ratio, 0, frozenset({recipe.id}))
    return out


@dataclass
class MadeSubRecipe:
    """Sous-recette faite maison dans un plan (pour le plan de bataille)."""

    line_id: int
    recipe_id: int
    parent_recipe_id: int
    ingredient_id: int
    ingredient_name: str
    ratio: float  # fournées de la sous-recette (à ses portions de base)
    children: list[MadeSubRecipe] = field(default_factory=list)


def made_sub_recipes(recipe_id: int, ratio: float, modes: dict | None) -> list[MadeSubRecipe]:
    """Arbre des sous-recettes faites maison sous `recipe_id` (choix du batch, sinon `default_mode`)."""

    def visit(r_id: int, r_ratio: float, depth: int, path: frozenset[int]) -> list[MadeSubRecipe]:
        if depth >= MAX_DEPTH:
            return []
        made = []
        for line in RecipeIngredient.objects.filter(recipe_id=r_id, sub_recipe__isnull=False).select_related(
            'ingredient'
        ):
            if line.sub_recipe_id in path or line_mode(line, modes) != MODE_MAKE:
                continue
            sub_ratio = r_ratio * float(line.sub_recipe_ratio or 1)
            node = MadeSubRecipe(
                line_id=line.id,
                recipe_id=line.sub_recipe_id,
                parent_recipe_id=r_id,
                ingredient_id=line.ingredient_id,
                ingredient_name=line.ingredient.name,
                ratio=sub_ratio,
            )
            node.children = visit(line.sub_recipe_id, sub_ratio, depth + 1, path | {line.sub_recipe_id})
            made.append(node)
        return made

    return visit(recipe_id, ratio, 0, frozenset({recipe_id}))


# ---------------------------------------------------------------------------
# Lecture (détail de recette, chat)
# ---------------------------------------------------------------------------
def _total_time(recipe: Recipe) -> int:
    return int(recipe.prep_time or 0) + int(recipe.cook_time or 0)


def extra_time_label(minutes: int) -> str:
    from recipes.services.battle_plan import format_minutes

    return f'+ {format_minutes(minutes)} si tu la fais' if minutes > 0 else ''


def _homemade_cost(recipe: Recipe, depth: int, path: frozenset[int], memo: dict) -> tuple[int, str]:
    """(temps total, difficulté max) de `recipe` avec toutes ses sous-recettes faites maison."""
    if recipe.id in memo:
        return memo[recipe.id]
    minutes, difficulty = _total_time(recipe), recipe.difficulty or 'medium'
    if depth < MAX_DEPTH:
        for line in RecipeIngredient.objects.filter(recipe_id=recipe.id, sub_recipe__isnull=False).select_related(
            'sub_recipe'
        ):
            sub = line.sub_recipe
            if sub.id in path:
                continue
            sub_minutes, sub_difficulty = _homemade_cost(sub, depth + 1, path | {sub.id}, memo)
            minutes += sub_minutes
            if _DIFFICULTY_RANK.get(sub_difficulty, 1) > _DIFFICULTY_RANK.get(difficulty, 1):
                difficulty = sub_difficulty
    memo[recipe.id] = (minutes, difficulty)
    return memo[recipe.id]


def _visible_to(user, recipe: Recipe) -> bool:
    if recipe.is_public:
        return True
    return bool(user is not None and getattr(user, 'is_authenticated', False) and recipe.created_by_id == user.id)


def sub_recipe_summary(recipe: Recipe) -> dict:
    return {
        'id': recipe.id,
        'title': recipe.title,
        'image_url': recipe.image_url,
        'prep_time': recipe.prep_time,
        'cook_time': recipe.cook_time,
        'total_time': _total_time(recipe) or None,
        'difficulty': recipe.difficulty,
        'servings': recipe.servings,
    }


def recipe_sub_recipes_payload(recipe: Recipe, user=None, *, modes: dict | None = None) -> dict:
    """
    Bloc additif du détail de recette : `sub_recipes` (lignes liées) + « version maison » (`total_time_if_made`,
    `difficulty_if_made` : toutes les sous-recettes faites maison). Sans sous-recette : liste vide et nulls.
    """
    lines = [
        line
        for line in RecipeIngredient.objects.filter(recipe_id=recipe.id, sub_recipe__isnull=False)
        .select_related('ingredient', 'sub_recipe')
        .order_by('id')
        if _visible_to(user, line.sub_recipe)
    ]
    if not lines:
        return {'sub_recipes': [], 'total_time_if_made': None, 'difficulty_if_made': None}
    memo: dict = {}
    items = []
    total, difficulty = _total_time(recipe), recipe.difficulty or 'medium'
    for line in lines:
        sub = line.sub_recipe
        extra, sub_difficulty = _homemade_cost(sub, 1, frozenset({recipe.id, sub.id}), memo)
        total += extra
        if _DIFFICULTY_RANK.get(sub_difficulty, 1) > _DIFFICULTY_RANK.get(difficulty, 1):
            difficulty = sub_difficulty
        items.append(
            {
                'line_id': line.id,
                'ingredient': {'id': line.ingredient_id, 'name': line.ingredient.name},
                'quantity': str(line.quantity),
                'unit': line.unit,
                'sub_recipe': sub_recipe_summary(sub),
                'sub_recipe_ratio': str(line.sub_recipe_ratio),
                'default_mode': line.default_mode,
                'mode': line_mode(line, modes),
                'extra_time_min': extra,
                'extra_time_label': extra_time_label(extra),
                'difficulty_if_made': sub_difficulty,
            }
        )
    return {
        'sub_recipes': items,
        'total_time_if_made': total or None,
        'difficulty_if_made': difficulty,
        'difficulty_if_made_display': _DIFFICULTY_LABEL.get(difficulty, difficulty),
    }


def sub_recipe_hints(recipe_ids) -> dict[int, list[dict]]:
    """Lignes à sous-recette par recette (chat : « tu peux l'acheter ou la faire maison »). Une requête."""
    out: dict[int, list[dict]] = {}
    rows = (
        RecipeIngredient.objects.filter(recipe_id__in=list(recipe_ids), sub_recipe__isnull=False)
        .select_related('ingredient', 'sub_recipe')
        .order_by('id')
    )
    for line in rows:
        sub = line.sub_recipe
        if not sub.is_public:
            continue
        extra = _total_time(sub)
        out.setdefault(line.recipe_id, []).append(
            {
                'line_id': line.id,
                'ingredient_name': line.ingredient.name,
                'sub_recipe_id': sub.id,
                'sub_recipe_title': sub.title,
                'default_mode': line.default_mode,
                'extra_time_min': extra,
                'hint': f'{line.ingredient.name} : tu peux l’acheter ou la faire maison'
                + (f' ({extra_time_label(extra)})' if extra else '')
                + '.',
            }
        )
    return out


# ---------------------------------------------------------------------------
# Liaison (propriétaire de la recette)
# ---------------------------------------------------------------------------
def linkable_recipes(user: AbstractBaseUser):
    """Recettes qu'on peut lier : publiques ou à soi."""
    return Recipe.objects.filter(Q(is_public=True) | Q(created_by=user))


def get_owned_line(user: AbstractBaseUser, line_id) -> RecipeIngredient:
    from recipes.services.battle_plan_service import visible_recipes

    try:
        line_id = int(line_id)
    except (TypeError, ValueError) as exc:
        raise SubRecipeError('Ligne introuvable.', 404, 'not_found') from exc
    line = (
        RecipeIngredient.objects.filter(id=line_id, recipe__in=visible_recipes(user))
        .select_related('recipe', 'ingredient', 'sub_recipe')
        .first()
    )
    if line is None:
        raise SubRecipeError('Ligne introuvable.', 404, 'not_found')
    if line.recipe.created_by_id != user.id:
        raise SubRecipeError('Seul l’auteur de la recette peut lier une sous-recette.', 403, 'not_owner')
    return line


def _parse_ratio(value) -> Decimal:
    try:
        ratio = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SubRecipeError('sub_recipe_ratio doit être un nombre.') from exc
    if not ratio.is_finite() or ratio <= 0 or ratio > MAX_RATIO:
        raise SubRecipeError(f'sub_recipe_ratio doit être entre 0 et {MAX_RATIO}.')
    return ratio.quantize(Decimal('0.001'))


def link_sub_recipe(
    user: AbstractBaseUser, line: RecipeIngredient, *, sub_recipe_id=None, default_mode=None, sub_recipe_ratio=None
) -> RecipeIngredient:
    """Lie (ou met à jour) la sous-recette d'une ligne. `sub_recipe_id` absent = garde la sous-recette actuelle."""
    fields = []
    if sub_recipe_id not in (None, ''):
        try:
            sub = linkable_recipes(user).filter(id=int(sub_recipe_id)).first()
        except (TypeError, ValueError):
            sub = None
        if sub is None:
            raise SubRecipeError('Sous-recette introuvable.', 404, 'sub_recipe_not_found')
        if sub.id != line.sub_recipe_id:
            validate_link(line.recipe, sub)
            line.sub_recipe = sub
            fields.append('sub_recipe')
    elif not line.sub_recipe_id:
        raise SubRecipeError('sub_recipe_id requis.', 400, 'sub_recipe_required')
    if default_mode not in (None, ''):
        if default_mode not in MODES:
            raise SubRecipeError('default_mode : « buy » ou « make ».')
        line.default_mode = default_mode
        fields.append('default_mode')
    if sub_recipe_ratio not in (None, ''):
        line.sub_recipe_ratio = _parse_ratio(sub_recipe_ratio)
        fields.append('sub_recipe_ratio')
    if fields:
        line.save(update_fields=fields)
    return line


def unlink_sub_recipe(line: RecipeIngredient) -> RecipeIngredient:
    line.sub_recipe = None
    line.default_mode = MODE_BUY
    line.sub_recipe_ratio = Decimal('1')
    line.save(update_fields=['sub_recipe', 'default_mode', 'sub_recipe_ratio'])
    return line


def suggest_sub_recipes(user: AbstractBaseUser, line: RecipeIngredient, *, limit: int = SUGGESTIONS_MAX) -> list[dict]:
    """
    Recettes visibles dont le titre ressemble au nom de la ligne (« pâte brisée » → « Pâte brisée maison »), sans LLM :
    trigrammes Postgres + titre normalisé. Exclut la recette elle-même et celles qui feraient une boucle.
    """
    from django.contrib.postgres.search import TrigramSimilarity, TrigramWordSimilarity

    name = (line.ingredient.name or '').strip()
    if not name:
        return []
    above, _ = ancestors(line.recipe_id)
    excluded = above | {line.recipe_id}
    qs = (
        linkable_recipes(user)
        .exclude(id__in=excluded)
        .annotate(sim=TrigramSimilarity('title', name), word_sim=TrigramWordSimilarity(name, 'title'))
        .filter(Q(sim__gte=0.3) | Q(word_sim__gte=0.6) | Q(title__icontains=name))
        .order_by('-word_sim', '-sim', '-id')
        .only('id', 'title', 'image_path', 'prep_time', 'cook_time', 'difficulty', 'servings', 'created_by_id')
    )[: limit * 4]
    target = fold(name)
    scored = []
    for r in qs:
        title = fold(r.title)
        exact = title == target
        prefix = title.startswith(target) or target.startswith(title)
        score = (
            2 if exact else 1 if prefix else 0,
            float(r.word_sim or 0),
            float(r.sim or 0),
            r.created_by_id == user.id,
        )
        scored.append((score, r))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [
        {**sub_recipe_summary(r), 'is_mine': r.created_by_id == user.id, 'score': round(s[1] + s[2] + s[0], 3)}
        for s, r in scored[:limit]
    ]


# ---------------------------------------------------------------------------
# Édition des ingrédients d'une recette (garde les liens)
# ---------------------------------------------------------------------------
def replace_recipe_ingredients(recipe: Recipe, rows) -> None:
    """
    Remplace les lignes de la recette par `rows` = [(Ingredient, Decimal quantité, unité)]. Les lignes liées à une
    sous-recette dont l'ingrédient reste sont mises à jour en place (id, lien et choix des batches conservés) ;
    les autres sont recréées dans l'ordre du payload (doublons d'ingrédient : le premier gagne).
    """
    kept = {
        line.ingredient_id: line for line in RecipeIngredient.objects.filter(recipe=recipe, sub_recipe__isnull=False)
    }
    wanted: dict[int, tuple] = {}
    for ingredient, quantity, unit in rows:
        wanted.setdefault(ingredient.id, (ingredient, quantity, unit))
    RecipeIngredient.objects.filter(recipe=recipe).exclude(id__in=[line.id for line in kept.values()]).delete()
    for ingredient_id, line in kept.items():
        if ingredient_id not in wanted:
            line.delete()
    for ingredient_id, (ingredient, quantity, unit) in wanted.items():
        line = kept.get(ingredient_id)
        if line is not None:
            if line.quantity != quantity or line.unit != unit:
                line.quantity, line.unit = quantity, unit
                line.save(update_fields=['quantity', 'unit'])
            continue
        RecipeIngredient.objects.create(recipe=recipe, ingredient=ingredient, quantity=quantity, unit=unit)
