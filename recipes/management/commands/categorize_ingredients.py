"""
Pipeline de rattrapage : assigne les rayons magasin aux ingrédients via mots-clés.

Usage prod (recommandé) :
  python manage.py categorize_ingredients --dry-run
  python manage.py categorize_ingredients

Par défaut : uniquement category NULL ou feuille « Autres ».
Avec --force : re-trie tous les ingrédients (écrase les rayons déjà posés).
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from recipes.models import Category, Ingredient
from recipes.supermarket_categories import match_leaf_slug_from_name


class Command(BaseCommand):
    help = (
        "Assigne les catégories magasin aux ingrédients (mots-clés → Autres). "
        "Par défaut : NULL + Autres uniquement. Utiliser --force pour tout re-trier."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Affiche le plan sans écrire en base.',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Re-trie tous les ingrédients, y compris ceux déjà catégorisés.',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=0,
            help='Limite le nombre d’ingrédients traités (0 = tous).',
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=500,
            help='Taille des lots de commit (défaut 500).',
        )

    def handle(self, *args, **options):
        dry = options['dry_run']
        force = options['force']
        limit = options['limit'] or 0
        batch_size = max(1, options['batch_size'] or 500)

        leaf_by_slug = {
            c.slug: c
            for c in Category.objects.exclude(slug__isnull=True).exclude(slug='')
        }
        autres = leaf_by_slug.get('autres')
        if autres is None:
            self.stderr.write(
                self.style.ERROR(
                    'Catégorie « autres » introuvable. '
                    'Lancer d’abord : python manage.py migrate_supermarket_categories'
                )
            )
            return

        qs = Ingredient.objects.select_related('category').order_by('id')
        if not force:
            qs = qs.filter(Q(category__isnull=True) | Q(category__slug='autres'))

        total = qs.count()
        if limit > 0:
            qs = qs[:limit]
            planned = min(total, limit)
        else:
            planned = total

        mode = 'FORCE (tous)' if force else 'NULL + Autres'
        self.stdout.write(
            f'Cibles : {planned} ingrédient(s) [{mode}]'
            + (f' (sur {total} éligibles)' if limit > 0 and limit < total else '')
        )
        if dry:
            self.stdout.write(self.style.WARNING('[dry-run] Aucune écriture.'))

        updated = 0
        unchanged = 0
        to_autres = 0
        matched = 0
        samples: list[str] = []

        def flush_batch(batch_updates: list[tuple[int, int]]) -> None:
            nonlocal updated
            if dry or not batch_updates:
                return
            with transaction.atomic():
                for pk, category_id in batch_updates:
                    Ingredient.objects.filter(pk=pk).update(category_id=category_id)
            updated += len(batch_updates)

        batch: list[tuple[int, int]] = []

        for ing in qs.iterator(chunk_size=batch_size):
            slug = match_leaf_slug_from_name(ing.name)
            if slug and slug in leaf_by_slug:
                target = leaf_by_slug[slug]
                matched += 1
            else:
                target = autres
                to_autres += 1

            if ing.category_id == target.id:
                unchanged += 1
                continue

            if len(samples) < 20:
                old = ing.category.name if ing.category_id else '∅'
                samples.append(f'  {ing.name!r}: {old} → {target.name} ({target.slug})')

            if dry:
                updated += 1
            else:
                batch.append((ing.id, target.id))
                if len(batch) >= batch_size:
                    flush_batch(batch)
                    batch = []

        flush_batch(batch)

        if samples:
            self.stdout.write('Exemples de changements :')
            for line in samples:
                self.stdout.write(line)
            if updated > len(samples):
                self.stdout.write(f'  … et {updated - len(samples)} autre(s)')

        self.stdout.write(
            self.style.SUCCESS(
                f'Terminé : mis à jour={updated}, inchangés={unchanged}, '
                f'match mots-clés={matched}, fallback Autres={to_autres}'
                + (' [dry-run]' if dry else '')
            )
        )
