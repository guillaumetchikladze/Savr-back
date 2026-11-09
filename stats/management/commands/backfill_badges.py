"""
Attribue les badges déjà mérités par l'historique « cuisiné » (sans notification ni push), puis recalcule
la rareté (% des cuisiniers actifs) pour que les nouveaux badges l'affichent tout de suite.

    python manage.py backfill_badges                 # tous les utilisateurs ayant au moins un plat cuisiné
    python manage.py backfill_badges --user 42
    python manage.py backfill_badges --skip-rarity   # sans recalcul de la rareté

Idempotent (contrainte unique user+code) : peut être relancé sans risque. À lancer après
classify_ingredients et normalize_cuisines (badges ingrédients/cuisines/végétaux), et après chaque ajout
de badges au registre (badges loufoques). Même sans ce rattrapage, un vieux badge attribué au fil de l'eau
n'est ni notifié ni célébré (`BADGE_NOTIFY_MAX_AGE_DAYS`).
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from recipes.models import MealPlan
from stats.services.badge_rarity import compute_badge_rarity
from stats.services.badges import evaluate_badges


class Command(BaseCommand):
    help = 'Attribue rétroactivement les badges (idempotent, sans notification).'

    def add_arguments(self, parser):
        parser.add_argument('--user', type=int, default=None, help='Un seul utilisateur (id).')
        parser.add_argument('--skip-rarity', action='store_true', help='Ne pas recalculer la rareté.')

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
        if not opts['skip_rarity']:
            rows = compute_badge_rarity()
            active = next(iter(rows.values())).active_users if rows else 0
            self.stdout.write(f'Rareté recalculée : {len(rows)} badge(s), {active} cuisinier(s) actif(s)')
