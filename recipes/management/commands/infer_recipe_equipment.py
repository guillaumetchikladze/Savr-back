"""
Backfill de `Recipe.required_equipment` (« Ta cuisine ») par mots-clés : titre + étapes, surcharge manuelle et
indices IA respectés. Idempotent et reprenable (par lots d'id croissants), sans appel réseau.

    python manage.py infer_recipe_equipment --only-missing      # recettes jamais calculées (prod, une fois)
    python manage.py infer_recipe_equipment                     # tout recalculer (après un changement de règles)
    python manage.py infer_recipe_equipment --dry-run --limit 50
"""

from __future__ import annotations

from collections import Counter

from django.core.management.base import BaseCommand
from django.db.models import Prefetch

from recipes.models import Recipe, Step
from recipes.services.equipment_service import compute_recipe_equipment


class Command(BaseCommand):
    help = 'Déduit les équipements requis des recettes (four, plaques, airfryer…) depuis leur texte.'

    def add_arguments(self, parser):
        parser.add_argument('--only-missing', action='store_true', help='Seulement required_equipment null.')
        parser.add_argument('--batch-size', type=int, default=500)
        parser.add_argument('--limit', type=int, default=0, help='Nombre max de recettes (0 = toutes).')
        parser.add_argument('--dry-run', action='store_true', help="N'écrit rien, affiche les compteurs.")

    def handle(self, *args, only_missing=False, batch_size=500, limit=0, dry_run=False, **options):
        qs = Recipe.objects.order_by('id').only(
            'id',
            'title',
            'servings',
            'required_equipment',
            'equipment_alternatives',
            'required_equipment_override',
            'equipment_ai_hints',
        )
        if only_missing:
            qs = qs.filter(required_equipment__isnull=True)
        steps_qs = Step.objects.order_by('order').only('id', 'recipe_id', 'order', 'title', 'instruction')
        last_id, seen, changed = 0, 0, 0
        keys = Counter()
        while True:
            batch = list(qs.filter(id__gt=last_id).prefetch_related(Prefetch('steps', queryset=steps_qs))[:batch_size])
            if not batch:
                break
            to_update = []
            for recipe in batch:
                last_id = recipe.id
                seen += 1
                required, alternatives, _ = compute_recipe_equipment(
                    recipe, [(s.title, s.instruction) for s in recipe.steps.all()]
                )
                keys.update(required)
                if recipe.required_equipment != required or (recipe.equipment_alternatives or {}) != alternatives:
                    recipe.required_equipment = required
                    recipe.equipment_alternatives = alternatives
                    to_update.append(recipe)
                if limit and seen >= limit:
                    break
            changed += len(to_update)
            if to_update and not dry_run:
                Recipe.objects.bulk_update(to_update, ['required_equipment', 'equipment_alternatives'])
            if limit and seen >= limit:
                break
        prefix = '[dry-run] ' if dry_run else ''
        self.stdout.write(f'{prefix}{seen} recette(s) lue(s), {changed} mise(s) à jour.')
        for key, count in keys.most_common():
            self.stdout.write(f'  {key}: {count}')
