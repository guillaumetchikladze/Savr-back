from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Alias de migrate_supermarket_categories (taxonomie magasin + rattrapage ingrédients).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Transmis à migrate_supermarket_categories.',
        )

    def handle(self, *args, **options):
        call_command(
            'migrate_supermarket_categories',
            dry_run=options.get('dry_run', False),
        )
