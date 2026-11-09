"""
Mesure de densité sociale (« Tes amis avant ton premier plat », .claude-work/specs/density.md) :

    python manage.py density_report --days 30 [--json]

Cohorte : comptes créés dans les N derniers jours **et** âgés d'au moins 7 jours (J7 atteint).
- % avec ≥ 3 amis à J7 : suivis approuvés (`Follow`, follower = l'utilisateur) créés avant J7 ;
  un suivi supprimé depuis n'est plus compté (pas d'historique des suppressions).
- miams donnés en semaine 1 : `PostCookie` de l'utilisateur sur les posts des autres, avant J7.
- ventilation invités (lien d'invitation à l'arrivée, `InviteRedemption.is_acquisition`) / autres.
"""

from __future__ import annotations

import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db.models import Count, Exists, IntegerField, OuterRef, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone

from accounts.models import Follow, InviteRedemption
from recipes.models import PostCookie
from recipes.services.density_service import TARGET_FRIENDS

WEEK = timedelta(days=7)


def _count(qs, group_field: str):
    return Coalesce(
        Subquery(
            qs.order_by().values(group_field).annotate(c=Count('id')).values('c')[:1], output_field=IntegerField()
        ),
        0,
    )


def density_rows(days: int, now=None) -> tuple[list[dict], int]:
    """(lignes de la cohorte mesurable, nombre de comptes trop récents pour J7)."""
    User = get_user_model()
    now = now or timezone.now()
    recent = User.objects.filter(is_active=True, created_at__gte=now - timedelta(days=days))
    too_young = recent.filter(created_at__gt=now - WEEK).count()
    d7 = OuterRef('created_at') + WEEK
    friends_d7 = Follow.objects.filter(follower_id=OuterRef('pk'), created_at__lt=d7).exclude(
        following_id=OuterRef('pk')
    )
    miams_w1 = PostCookie.objects.filter(user_id=OuterRef('pk'), created_at__lt=d7).exclude(
        post__user_id=OuterRef('pk')
    )
    rows = list(
        recent.filter(created_at__lte=now - WEEK)
        .annotate(
            friends_d7=_count(friends_d7, 'follower_id'),
            miams_w1=_count(miams_w1, 'user_id'),
            invited=Exists(InviteRedemption.objects.filter(invitee_id=OuterRef('pk'), is_acquisition=True)),
        )
        .values('id', 'friends_d7', 'miams_w1', 'invited')
    )
    return rows, too_young


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {'users': 0, 'pct_3_friends_d7': None, 'pct_gave_miam_w1': None, 'avg_miams_given_w1': None}
    with_friends = sum(1 for r in rows if r['friends_d7'] >= TARGET_FRIENDS)
    gave = sum(1 for r in rows if r['miams_w1'] > 0)
    return {
        'users': n,
        'pct_3_friends_d7': round(100.0 * with_friends / n, 1),
        'pct_gave_miam_w1': round(100.0 * gave / n, 1),
        'avg_miams_given_w1': round(sum(r['miams_w1'] for r in rows) / n, 2),
    }


class Command(BaseCommand):
    help = '% de nouveaux utilisateurs avec ≥ 3 amis à J7 et miams donnés en semaine 1.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=30, help='Fenêtre des inscriptions (jours, défaut 30).')
        parser.add_argument('--json', action='store_true', help='Sortie JSON.')

    def handle(self, *args, **options):
        days = max(int(options['days']), 1)
        rows, too_young = density_rows(days)
        report = {
            'days': days,
            'target_friends': TARGET_FRIENDS,
            'too_recent_for_d7': too_young,
            'all': summarize(rows),
            'invited': summarize([r for r in rows if r['invited']]),
            'not_invited': summarize([r for r in rows if not r['invited']]),
        }
        if options['json']:
            self.stdout.write(json.dumps(report, ensure_ascii=False))
            return

        self.stdout.write(
            f'Inscrits des {days} derniers jours ayant atteint J7 : {report["all"]["users"]} '
            f'(+ {too_young} trop récents)'
        )
        for key, label in (('all', 'Tous'), ('invited', 'Arrivés par invitation'), ('not_invited', 'Sans invitation')):
            s = report[key]
            if not s['users']:
                self.stdout.write(f'  {label} : aucun')
                continue
            self.stdout.write(
                f'  {label} ({s["users"]}) : ≥ {TARGET_FRIENDS} amis à J7 {s["pct_3_friends_d7"]} % · '
                f'miam donné en semaine 1 {s["pct_gave_miam_w1"]} % · '
                f'miams donnés en moyenne {s["avg_miams_given_w1"]}'
            )
