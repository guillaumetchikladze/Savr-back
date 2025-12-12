from django.core.management.base import BaseCommand
from django.db import transaction

from recipes.models import MealPlan


class Command(BaseCommand):
    help = 'Legacy command deprecated after batch refactor (no-op).'

    def handle(self, *args, **options):
        count = MealPlan.objects.count()
        self.stdout.write(self.style.SUCCESS(f'No-op: {count} meal plans, grouping handled via batches.'))



