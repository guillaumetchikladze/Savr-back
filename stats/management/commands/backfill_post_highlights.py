"""
Calcule l'ardoise (`Post.highlights`) des posts qui n'en ont pas encore, par lots (optionnel :
sans cette commande, l'ardoise est calculée paresseusement à la première lecture).

    python manage.py backfill_post_highlights              # tous les posts sans ardoise à jour
    python manage.py backfill_post_highlights --batch 200

Idempotent et reprenable (seuls les posts sans instantané à la version courante sont traités).
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Q

from recipes.models import Post
from stats.services.highlights import SNAPSHOT_VERSION, ensure_post_highlights


class Command(BaseCommand):
    help = "Calcule l'ardoise des posts « J'ai cuisiné » existants (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument('--batch', type=int, default=200)

    def handle(self, *args, **opts):
        stale = Post.objects.filter(Q(highlights__isnull=True) | ~Q(highlights__v=SNAPSHOT_VERSION))
        done, last_id = 0, 0
        while True:
            batch = list(stale.filter(id__gt=last_id).order_by('id')[: opts['batch']])
            if not batch:
                break
            ensure_post_highlights(batch)
            done += len(batch)
            last_id = batch[-1].id
            self.stdout.write(f'{done} posts traités')
        self.stdout.write(self.style.SUCCESS(f'Ardoise calculée pour {done} posts'))
