from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from notifications.services import open_rates


class Command(BaseCommand):
    help = "Taux d'ouverture des push par type (NotificationDelivery.opened_at)."

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=30)

    def handle(self, *args, days, **options):
        rows = open_rates(timezone.now() - timedelta(days=days))
        if not rows:
            self.stdout.write('Aucune push envoyée sur la période.')
            return
        for r in rows:
            self.stdout.write(
                f'{r["type"]:<28} envoyées={r["sent"]:<6} ouvertes={r["opened"]:<6} taux={r["open_rate"]:.1%}'
            )
