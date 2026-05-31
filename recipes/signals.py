"""Signaux recherche : désactivés — réindexation via schedule_recipe_search_reindex() uniquement."""

# Les post_save sur RecipeIngredient / Step provoquaient une tempête Celery
# (1 tâche par ligne) en plus du reindex de fin d'import/edit.
