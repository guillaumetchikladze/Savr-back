"""
Pré-génère les fiches produit (courses) des N ingrédients les plus utilisés qui n'en ont pas.

    python manage.py pregenerate_ingredient_info --limit 200 [--batch-size 20] [--sync] [--dry-run]

Par défaut : un lot Celery (`generate_ingredient_info_batch_task`) par `--batch-size` ingrédients (worker requis).
`--sync` : génère dans ce processus. Idempotent (fiches existantes ignorées), reprenable.
"""

from django.core.management.base import BaseCommand

from core.celery_dispatch import publish
from recipes.models import Ingredient
from recipes.services.ingredient_info import generate_many, most_used_missing_ids
from recipes.tasks import generate_ingredient_info_batch_task


class Command(BaseCommand):
    help = 'Pré-génère les fiches produit des ingrédients les plus utilisés (LLM, par lots).'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=100)
        parser.add_argument('--batch-size', type=int, default=20)
        parser.add_argument('--sync', action='store_true', help='Générer ici plutôt que via Celery.')
        parser.add_argument('--dry-run', action='store_true', help='Lister sans générer.')

    def handle(self, *args, **opts):
        ids = most_used_missing_ids(opts['limit'])
        if not ids:
            self.stdout.write('Aucun ingrédient sans fiche.')
            return
        if opts['dry_run']:
            names = dict(Ingredient.objects.filter(id__in=ids).values_list('id', 'name'))
            for ingredient_id in ids:
                self.stdout.write(f'{ingredient_id}\t{names.get(ingredient_id, "")}')
            self.stdout.write(f'{len(ids)} ingrédient(s) sans fiche.')
            return
        size = max(opts['batch_size'], 1)
        batches = [ids[i : i + size] for i in range(0, len(ids), size)]
        if opts['sync']:
            totals = {'generated': 0, 'skipped': 0, 'failed': 0}
            for batch in batches:
                for key, value in generate_many(batch).items():
                    totals[key] += value
                self.stdout.write(f'… {totals}')
            self.stdout.write(self.style.SUCCESS(f'Terminé : {totals}'))
            return
        sent = sum(1 for batch in batches if publish(generate_ingredient_info_batch_task, args=(batch,)))
        self.stdout.write(
            self.style.SUCCESS(f'{sent}/{len(batches)} lot(s) envoyé(s) à Celery ({len(ids)} ingrédients).')
        )
