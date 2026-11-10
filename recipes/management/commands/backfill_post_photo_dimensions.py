"""
Renseigne `PostPhoto.width` / `height` des photos existantes (l'app cadre le post au ratio de la
1re photo ; sans taille elle garde le carré).

    python manage.py backfill_post_photo_dimensions                 # photos rattachées à un post
    python manage.py backfill_post_photo_dimensions --all-photos    # + photos de repas / brouillons
    python manage.py backfill_post_photo_dimensions --limit 500 --dry-run

Ne lit que l'en-tête de chaque image (quelques Ko). Idempotent et reprenable : seules les photos sans
taille sont traitées. Une photo absente / injoignable / illisible est comptée « échec » et laissée sans
taille (une relance la retentera) ; la commande ne s'arrête jamais sur une photo.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from recipes.models import PostPhoto
from recipes.services.photo_dimensions import fetch_image_dimensions


class Command(BaseCommand):
    help = 'Renseigne la taille (largeur / hauteur) des photos de post existantes (idempotent).'

    def add_arguments(self, parser):
        parser.add_argument('--batch', type=int, default=200, help='Photos chargées par lot (défaut 200)')
        parser.add_argument('--limit', type=int, default=0, help='Nombre max de photos traitées (0 = toutes)')
        parser.add_argument(
            '--all-photos', action='store_true', help='Aussi les photos non rattachées à un post (repas, galerie)'
        )
        parser.add_argument('--dry-run', action='store_true', help='Lit les tailles sans rien écrire')

    def handle(self, *args, **opts):
        qs = PostPhoto.objects.filter(width__isnull=True, is_draft=False).exclude(image_path='')
        if not opts['all_photos']:
            qs = qs.filter(post__isnull=False)
        limit = max(0, opts['limit'])
        batch_size = max(1, opts['batch'])
        dry_run = opts['dry_run']

        done = filled = failed = 0
        last_id = 0
        while not limit or done < limit:
            size = batch_size if not limit else min(batch_size, limit - done)
            batch = list(qs.filter(id__gt=last_id).order_by('id').only('id', 'image_path')[:size])
            if not batch:
                break
            for photo in batch:
                last_id = photo.id
                done += 1
                try:
                    dims = fetch_image_dimensions(photo.image_path)
                except Exception as exc:  # garde-fou : une photo ne bloque jamais la commande
                    self.stderr.write(f'photo {photo.id} : {exc}')
                    dims = None
                if not dims:
                    failed += 1
                    self.stdout.write(f'photo {photo.id} : taille illisible ({photo.image_path})')
                    continue
                filled += 1
                if not dry_run:
                    PostPhoto.objects.filter(pk=photo.pk).update(width=dims[0], height=dims[1])
            self.stdout.write(f'{done} photos lues ({filled} dimensionnées, {failed} échecs)')

        verb = 'lisibles (dry-run, rien écrit)' if dry_run else 'dimensionnées'
        self.stdout.write(self.style.SUCCESS(f'{filled} photos {verb}, {failed} échecs sur {done}'))
