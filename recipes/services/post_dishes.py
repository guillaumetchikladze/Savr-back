"""
Plats d'un post (ckd2 lots 3b / 3c) : `dishes` des serializers de post.

Une entrée par recette du repas du post, dans l'ordre du repas (un menu « entrée, plat, dessert » en a
plusieurs ; un post à une recette, une seule) :
    {recipe_batch_id, recipe: {id, title, image_url}, changes, author}
- `changes` = « Ce que tu as changé » (`MealPlanRecipeBatch.changes`), '' sinon. Il a l'audience du post :
  il n'est exposé que là, à qui peut voir le post (le fil, le détail et la page du plat filtrent déjà les
  posts visibles) ; jamais dans les API du planning (invités, foyer), jamais pour du profilage.
- `author` = {id, username, first_name} de l'auteur de la recette quand ce n'est pas l'auteur du post et
  qu'il y a des changements (« D'après Léa · Sa version : … ») ; null sinon (ou auteur bloqué).
Calculé sur les préchargements du fil (`meal_plan__meal_plan_recipe_batches` + recette) : 0 requête,
sauf l'auteur d'une recette d'un autre quand il n'est pas préchargé (rare, mis en cache par requête).
"""

from __future__ import annotations

from recipes.models import Post, Recipe


def _author_payload(recipe: Recipe, post: Post, context: dict) -> dict | None:
    author_id = recipe.created_by_id
    if not author_id or author_id == post.user_id:
        return None
    request = context.get('request')
    from .post_visibility import request_blocked_user_ids

    if author_id in request_blocked_user_ids(request):
        return None
    cache = context.setdefault('recipe_author_cache', {})
    if author_id not in cache:
        author = recipe.created_by  # préchargé ou 1 requête (mise en cache pour la page)
        cache[author_id] = (
            {
                'id': author.id,
                'username': author.username,
                'first_name': (author.first_name or '').strip() or author.username,
            }
            if author is not None and author.is_active
            else None
        )
    return cache[author_id]


def post_dishes(post: Post, context: dict | None = None) -> list[dict]:
    context = context if context is not None else {}
    entries: list[dict] = []
    seen: set[int] = set()

    def add(batch, changes: str = '') -> None:
        if batch is None or not batch.id or batch.id in seen:
            return
        recipe = getattr(batch, 'recipe', None)
        if recipe is None:
            return
        seen.add(batch.id)
        changes = (changes or '').strip()
        entries.append(
            {
                'recipe_batch_id': batch.id,
                'recipe': {
                    'id': recipe.id,
                    'title': recipe.title,
                    'image_url': getattr(recipe, 'image_url', None),
                },
                'changes': changes,
                'author': _author_payload(recipe, post, context) if changes else None,
            }
        )

    meal_plan = post.meal_plan if post.meal_plan_id else None
    if meal_plan is not None:
        for mprb in meal_plan.meal_plan_recipe_batches.all():
            add(getattr(mprb, 'recipe_batch', None), mprb.changes)
    if not entries and post.recipe_batch_id:
        add(post.recipe_batch)
    return entries
