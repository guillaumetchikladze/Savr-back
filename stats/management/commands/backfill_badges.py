"""
Attribue les badges déjà mérités par l'historique « cuisiné » (sans push).

    python manage.py backfill_badges                 # tous les utilisateurs ayant au moins un plat cuisiné
    python manage.py backfill_badges --user 42

Idempotent (contrainte unique user+code) : peut être relancé sans risque. À lancer après
classify_ingredients et normalize_cuisines (badges ingrédients/cuisines/végétaux).
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from recipes.models import MealPlan
from stats.services.badges import evaluate_badges


class Command(BaseCommand):
    help = 'Attribue rétroactivement les badges (idempotent, sans notification).'

    def add_arguments(self, parser):
        parser.add_argument('--user', type=int, default=None, help='Un seul utilisateur (id).')

    def handle(self, *args, **opts):
        user_ids = MealPlan.objects.filter(confirmed=True).values('user_id').distinct()
        users = get_user_model().objects.filter(id__in=user_ids).order_by('id')
        if opts['user']:
            users = users.filter(id=opts['user'])
        total_users = total_badges = 0
        for user in users.iterator():
            new = evaluate_badges(user, notify=False)
            total_users += 1
            total_badges += len(new)
            if new:
                self.stdout.write(f'  {user.id} {user.username}: {", ".join(b["code"] for b in new)}')
        self.stdout.write(self.style.SUCCESS(f'{total_badges} badge(s) attribué(s) à {total_users} utilisateur(s)'))
