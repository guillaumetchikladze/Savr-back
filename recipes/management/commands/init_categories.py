from django.core.management.base import BaseCommand
from recipes.models import Category, Ingredient


class Command(BaseCommand):
    help = 'Initialise les catégories et assigne les ingrédients existants'

    def handle(self, *args, **options):
        # Créer les catégories
        categories_data = [
            {'name': 'Fruits', 'display_order': 1},
            {'name': 'Légumes', 'display_order': 2},
            {'name': 'Viandes & Poissons', 'display_order': 3},
            {'name': 'Produits laitiers', 'display_order': 4},
            {'name': 'Pain & Pâtisserie', 'display_order': 5},
            {'name': 'Épicerie', 'display_order': 6},
            {'name': 'Autres', 'display_order': 7},
        ]
        
        categories = {}
        for cat_data in categories_data:
            category, created = Category.objects.get_or_create(
                name=cat_data['name'],
                defaults={'display_order': cat_data['display_order']}
            )
            categories[cat_data['name']] = category
            if created:
                self.stdout.write(self.style.SUCCESS(f'✓ Catégorie créée: {category.name}'))
            else:
                self.stdout.write(f'  Catégorie existante: {category.name}')
        
        # Mapping des ingrédients vers les catégories
        ingredient_mapping = {
            'Fruits': [
                'pomme', 'banane', 'orange', 'citron', 'fraise', 'framboise', 'myrtille',
                'mûre', 'cerise', 'pêche', 'abricot', 'prune', 'raisin', 'kiwi', 'mangue',
                'ananas', 'avocat', 'tomate', 'poivron'
            ],
            'Légumes': [
                'carotte', 'courgette', 'aubergine', 'oignon', 'ail', 'échalote',
                'champignon', 'salade', 'épinard', 'brocoli', 'chou', 'chou-fleur',
                'haricot', 'petit pois', 'maïs', 'pomme de terre', 'patate', 'patate douce',
                'courge', 'potiron', 'butternut', 'céleri', 'fenouil', 'radis', 'navet',
                'betterave', 'asperge', 'artichaut', 'poireau', 'endive', 'chicorée'
            ],
            'Viandes & Poissons': [
                'boeuf', 'bœuf', 'veau', 'porc', 'agneau', 'mouton', 'poulet', 'dinde',
                'canard', 'jambon', 'saucisse', 'bacon', 'lard', 'steak', 'escalope',
                'côte', 'côtelette', 'saumon', 'thon', 'cabillaud', 'sole', 'bar',
                'dorade', 'sardine', 'maquereau', 'crevette', 'crevettes', 'crabe',
                'homard', 'moule', 'huître', 'coquille', 'poisson'
            ],
            'Produits laitiers': [
                'lait', 'crème', 'beurre', 'fromage', 'yaourt', 'yogourt',
                'fromage blanc', 'ricotta', 'mozzarella', 'parmesan', 'chèvre',
                'brie', 'camembert'
            ],
            'Pain & Pâtisserie': [
                'pain', 'baguette', 'brioche', 'croissant', 'tarte', 'gâteau',
                'biscuit', 'cookie'
            ],
            'Épicerie': [
                'huile', 'vinaigre', 'sel', 'poivre', 'sucre', 'farine', 'levure',
                'pâte', 'riz', 'pâtes', 'semoule', 'couscous', 'quinoa', 'boulgour',
                'lentille', 'haricot sec', 'pois chiche', 'amande', 'noix',
                'noisette', 'cacahuète', 'épice', 'herbe', 'thym', 'romarin',
                'basilic', 'persil', 'coriandre', 'cumin', 'curry', 'paprika',
                'cannelle', 'vanille'
            ],
        }
        
        # Assigner les ingrédients aux catégories
        assigned = 0
        for category_name, keywords in ingredient_mapping.items():
            category = categories[category_name]
            for keyword in keywords:
                ingredients = Ingredient.objects.filter(
                    name__icontains=keyword,
                    category__isnull=True
                )
                count = ingredients.update(category=category)
                if count > 0:
                    assigned += count
                    self.stdout.write(f'  {count} ingrédient(s) assigné(s) à {category_name} (mot-clé: {keyword})')
        
        # Assigner les ingrédients restants à "Autres"
        remaining = Ingredient.objects.filter(category__isnull=True).count()
        if remaining > 0:
            others_category = categories['Autres']
            Ingredient.objects.filter(category__isnull=True).update(category=others_category)
            self.stdout.write(self.style.SUCCESS(f'✓ {remaining} ingrédient(s) assigné(s) à "Autres"'))
        
        self.stdout.write(self.style.SUCCESS(f'\n✓ Total: {assigned + remaining} ingrédient(s) catégorisé(s)'))

