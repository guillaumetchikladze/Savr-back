"""
Normalise Recipe.cuisine (liste fermée stats.cuisines) depuis search_context_tags.cuisine_style.

    python manage.py normalize_cuisines --only-missing             # prod : synonymes puis LLM pour le reste
    python manage.py normalize_cuisines --no-llm                   # synonymes uniquement
    python manage.py normalize_cuisines --only-missing --limit 500 --dry-run

Les recettes sans tag reconnu (tags génériques, absents ou inconnus) partent au LLM par lots
(titre + tags + ingrédients) ; repli « autre ». Idempotent et reprenable avec --only-missing.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from stats.services.classification import DEFAULT_BATCH_SIZE, normalize_recipe_cuisines, recipes_to_normalize


class Command(BaseCommand):
    help = 'Normalise la cuisine des recettes (synonymes + repli LLM).'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=0, help='Nombre max de recettes (0 = toutes).')
        parser.add_argument('--only-missing', action='store_true', help='Uniquement les recettes sans cuisine.')
        parser.add_argument('--batch-size', type=int, default=DEFAULT_BATCH_SIZE, help='Taille des lots LLM.')
        parser.add_argument('--no-llm', action='store_true', help='Synonymes uniquement (aucun appel LLM).')
        parser.add_argument('--dry-run', action='store_true', help='Calcule sans écrire en base.')

    def handle(self, *args, **opts):
        recipes = recipes_to_normalize(only_missing=opts['only_missing'], limit=opts['limit'] or None)
        self.stdout.write(f'{len(recipes)} recette(s) à normaliser')
        if not recipes:
            return
        report = normalize_recipe_cuisines(
            recipes, use_llm=not opts['no_llm'], batch_size=max(1, opts['batch_size']), dry_run=opts['dry_run']
        )
        self.stdout.write(
            self.style.SUCCESS(
                f'synonymes={report.synonyms} llm={report.llm} non_résolues={report.unresolved} '
                f'lots_en_échec={report.failed_batches}' + (' (dry-run)' if opts['dry_run'] else '')
            )
        )
