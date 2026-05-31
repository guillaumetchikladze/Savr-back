"""
Réindexe toutes les recettes publiques (Gemini + embedding 512d).
Usage: python manage.py regenerate_all_search_data [--force] [--batch-size 8] [--public-only]
"""

from django.core.management.base import BaseCommand

from recipes.models import Recipe
from recipes.services.recipe_search_index import index_recipe


class Command(BaseCommand):
    help = 'Régénère search_index_text, tags Gemini et embeddings pour les recettes'

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true', help='Réindexer même si le hash est inchangé')
        parser.add_argument('--batch-size', type=int, default=8, help='Taille de lot logique (défaut: 8)')
        parser.add_argument(
            '--all',
            action='store_true',
            help='Inclure aussi les recettes privées (défaut: publiques seulement)',
        )
        parser.add_argument('--recipe-id', type=int, help='Une seule recette')

    def handle(self, *args, **options):
        force = options['force']
        batch_size = max(1, options['batch_size'])
        recipe_id = options.get('recipe_id')

        qs = Recipe.objects.all().order_by('id')
        if recipe_id:
            qs = qs.filter(pk=recipe_id)
        elif not options['all']:
            qs = qs.filter(is_public=True)

        total = qs.count()
        self.stdout.write(f'Réindexation de {total} recette(s)...')

        ok = failed = skipped = 0
        ids = list(qs.values_list('id', flat=True))

        for i in range(0, len(ids), batch_size):
            chunk = ids[i : i + batch_size]
            for rid in chunk:
                recipe = Recipe.objects.filter(pk=rid).first()
                if not recipe:
                    continue
                if (
                    not force
                    and recipe.search_index_status == Recipe.SearchIndexStatus.READY
                    and recipe.embedding is not None
                    and recipe.search_index_hash
                ):
                    skipped += 1
                    continue
                if index_recipe(rid, force=force):
                    ok += 1
                else:
                    failed += 1

        self.stdout.write(
            self.style.SUCCESS(f'Terminé: {ok} ok, {failed} échecs, {skipped} ignorées (déjà à jour)')
        )
