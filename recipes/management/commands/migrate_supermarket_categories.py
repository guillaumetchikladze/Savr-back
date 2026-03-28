from django.core.management.base import BaseCommand
from django.db import transaction

from recipes.models import Category, Ingredient
from recipes.supermarket_categories import (
    LEGACY_CATEGORY_NAME_TO_LEAF_SLUG,
    SUPERMARKET_TREE,
    match_leaf_slug_from_name,
)


class Command(BaseCommand):
    help = (
        "Crée la taxonomie magasin (parents + feuilles), rattache les ingrédients "
        "(mots-clés > catégorie actuelle si déjà une feuille sluguée > mapping legacy > Autres), "
        "puis supprime les anciennes catégories sans slug."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Affiche les actions sans écrire en base.',
        )

    def handle(self, *args, **options):
        dry = options['dry_run']

        legacy_names = set(LEGACY_CATEGORY_NAME_TO_LEAF_SLUG.keys())
        legacy_id_to_slug = {}
        for name in legacy_names:
            c = Category.objects.filter(name=name, slug__isnull=True).first()
            if c:
                legacy_id_to_slug[c.id] = LEGACY_CATEGORY_NAME_TO_LEAF_SLUG[name]

        if dry:
            self.stdout.write(
                f'[dry-run] Anciennes catégories détectées: {len(legacy_id_to_slug)} '
                f'(ids {list(legacy_id_to_slug.keys())})'
            )

        def do_work():
            # Libérer les noms « Autres », « Fruits », etc. pour update_or_create
            for cid in legacy_id_to_slug:
                Category.objects.filter(pk=cid).update(name=f'__legacy_{cid}'[:100])

            for pslug, pname, porder, children in SUPERMARKET_TREE:
                parent, _ = Category.objects.update_or_create(
                    slug=pslug,
                    defaults={
                        'name': pname,
                        'display_order': porder,
                        'parent': None,
                    },
                )
                for cslug, cname, corder, _kws in children:
                    Category.objects.update_or_create(
                        slug=cslug,
                        defaults={
                            'name': cname,
                            'display_order': corder,
                            'parent': parent,
                        },
                    )

            leaf_by_slug = {c.slug: c for c in Category.objects.exclude(slug__isnull=True).exclude(slug='')}
            if 'autres' not in leaf_by_slug:
                raise RuntimeError('Catégorie feuille « autres » introuvable après seed.')

            updated = 0
            kept = 0
            for ing in Ingredient.objects.select_related('category').iterator():
                slug = match_leaf_slug_from_name(ing.name)
                if slug is None:
                    cat = ing.category
                    if cat and cat.slug:
                        kept += 1
                        continue
                    if ing.category_id and ing.category_id in legacy_id_to_slug:
                        slug = legacy_id_to_slug[ing.category_id]
                    else:
                        slug = 'autres'

                target = leaf_by_slug.get(slug) or leaf_by_slug['autres']

                if ing.category_id != target.id:
                    Ingredient.objects.filter(pk=ing.pk).update(category_id=target.id)
                    updated += 1

            if legacy_id_to_slug:
                n_deleted, _ = Category.objects.filter(id__in=legacy_id_to_slug.keys()).delete()
                self.stdout.write(
                    self.style.SUCCESS(f'Anciennes catégories supprimées ({n_deleted} ligne(s) d’objets liés).')
                )

            self.stdout.write(
                self.style.SUCCESS(
                    f'Ingrédients mis à jour: {updated}, inchangés (déjà feuille sans mot-clé): {kept}'
                )
            )

        if dry:
            self.stdout.write('[dry-run] Aucune écriture.')
            return

        with transaction.atomic():
            do_work()

        self.stdout.write(self.style.SUCCESS('Taxonomie magasin appliquée.'))
