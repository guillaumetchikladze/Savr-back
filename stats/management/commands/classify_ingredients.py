"""
Classe les ingrédients pour les stats (IngredientProfile : groupe alimentaire, végétal, « de base »).

    python manage.py classify_ingredients --only-missing            # prod : reprenable, idempotent
    python manage.py classify_ingredients --only-missing --limit 200 --dry-run
    python manage.py classify_ingredients --no-llm                  # seed déterministe uniquement

Ordre : ingrédients les plus utilisés d'abord. Seed déterministe (sel, huile, tomate…) puis Gemini par
lots de --batch-size (JSON structuré). Chaque lot est écrit dès qu'il est classé ; un lot en échec est
simplement repris au prochain passage. Les profils `validated` (relus en admin) ne sont jamais modifiés.
Sans --only-missing : reclasse aussi les profils existants non validés.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from stats.services.classification import DEFAULT_BATCH_SIZE, classify_ingredients, ingredients_to_classify


class Command(BaseCommand):
    help = 'Classe les ingrédients (groupe alimentaire, végétal, ingrédient de base) via seed + LLM.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=0, help='Nombre max d’ingrédients (0 = tous).')
        parser.add_argument('--only-missing', action='store_true', help='Uniquement les ingrédients sans profil.')
        parser.add_argument('--batch-size', type=int, default=DEFAULT_BATCH_SIZE, help='Taille des lots LLM.')
        parser.add_argument('--no-llm', action='store_true', help='Seed déterministe uniquement (aucun appel LLM).')
        parser.add_argument('--dry-run', action='store_true', help='Classe sans écrire en base.')

    def handle(self, *args, **opts):
        ingredients = ingredients_to_classify(only_missing=opts['only_missing'], limit=opts['limit'] or None)
        self.stdout.write(f'{len(ingredients)} ingrédient(s) à classer')
        if not ingredients:
            return
        batch_size = max(1, opts['batch_size'])
        report = classify_ingredients(
            ingredients, use_llm=not opts['no_llm'], batch_size=batch_size, dry_run=opts['dry_run']
        )
        self.stdout.write(
            self.style.SUCCESS(
                f'seed={report.seeded} llm={report.llm} ignorés={report.skipped} '
                f'lots_en_échec={report.failed_batches}' + (' (dry-run)' if opts['dry_run'] else '')
            )
        )
        for err in report.errors[:5]:
            self.stderr.write(f'  erreur lot : {err}')
