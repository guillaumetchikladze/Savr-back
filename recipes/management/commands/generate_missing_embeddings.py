"""
Commande Django pour générer les embeddings manquants pour les ingrédients et recettes.
Usage: python manage.py generate_missing_embeddings [--ingredients] [--recipes] [--batch-size N]
"""
import logging
from django.core.management.base import BaseCommand
from django.db import transaction
from recipes.models import Ingredient, Recipe
from recipes.services.ingredient_matcher import get_batch_embeddings

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Génère les embeddings manquants pour les ingrédients et/ou recettes'

    def add_arguments(self, parser):
        parser.add_argument(
            '--ingredients',
            action='store_true',
            help='Générer les embeddings manquants pour les ingrédients',
        )
        parser.add_argument(
            '--recipes',
            action='store_true',
            help='Générer les embeddings manquants pour les recettes',
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=32,
            help='Taille du batch pour la génération d\'embeddings (défaut: 32)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Affiche ce qui serait fait sans faire de modifications',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        batch_size = options['batch_size']
        
        if not options['ingredients'] and not options['recipes']:
            # Par défaut, générer pour les deux
            generate_ingredients = True
            generate_recipes = True
        else:
            generate_ingredients = options['ingredients']
            generate_recipes = options['recipes']

        if dry_run:
            self.stdout.write(self.style.WARNING('Mode DRY-RUN : aucune modification ne sera effectuée'))

        total_ingredients = 0
        total_recipes = 0

        if generate_ingredients:
            total_ingredients = self.generate_ingredient_embeddings(batch_size, dry_run)
        
        if generate_recipes:
            total_recipes = self.generate_recipe_embeddings(batch_size, dry_run)

        self.stdout.write(
            self.style.SUCCESS(
                f'\n✅ Terminé ! '
                f'Ingrédients traités: {total_ingredients}, '
                f'Recettes traitées: {total_recipes}'
            )
        )

    def generate_ingredient_embeddings(self, batch_size: int, dry_run: bool) -> int:
        """Génère les embeddings manquants pour les ingrédients"""
        self.stdout.write(self.style.SUCCESS('\n🔍 Recherche des ingrédients sans embedding...'))
        
        # Trouver tous les ingrédients sans embedding
        ingredients_without_embedding = Ingredient.objects.filter(embedding__isnull=True)
        
        total = ingredients_without_embedding.count()
        
        if total == 0:
            self.stdout.write(self.style.SUCCESS('✅ Tous les ingrédients ont déjà un embedding'))
            return 0

        self.stdout.write(f'📊 {total} ingrédient(s) sans embedding trouvé(s)')
        
        if dry_run:
            self.stdout.write(self.style.WARNING(f'[DRY-RUN] {total} ingrédient(s) seraient traités'))
            return total

        # Traiter par batch
        processed = 0
        failed = 0
        
        for i in range(0, total, batch_size):
            batch = list(ingredients_without_embedding[i:i + batch_size])
            ingredient_names = [ing.name for ing in batch]
            
            self.stdout.write(f'🔄 Traitement du batch {i // batch_size + 1} ({len(batch)} ingrédients)...')
            
            # Générer les embeddings en batch
            embeddings = get_batch_embeddings(ingredient_names)
            
            # Mettre à jour les ingrédients
            with transaction.atomic():
                for ingredient, embedding in zip(batch, embeddings):
                    if embedding:
                        ingredient.embedding = embedding
                        ingredient.save(update_fields=['embedding'])
                        processed += 1
                    else:
                        self.stdout.write(
                            self.style.WARNING(f'⚠️  Échec de génération pour: {ingredient.name}')
                        )
                        failed += 1
            
            self.stdout.write(f'✅ Batch traité: {processed} succès, {failed} échecs')

        self.stdout.write(
            self.style.SUCCESS(
                f'\n✅ Ingrédients: {processed} embeddings générés, {failed} échecs'
            )
        )
        
        return processed

    def generate_recipe_embeddings(self, batch_size: int, dry_run: bool) -> int:
        """Génère les embeddings manquants pour les recettes"""
        self.stdout.write(self.style.SUCCESS('\n🔍 Recherche des recettes sans embedding...'))
        
        # Trouver toutes les recettes sans embedding
        recipes_without_embedding = Recipe.objects.filter(
            embedding__isnull=True,
        ).select_related('created_by').prefetch_related(
            'recipe_ingredients__ingredient',
            'steps'
        )
        
        total = recipes_without_embedding.count()
        
        if total == 0:
            self.stdout.write(self.style.SUCCESS('✅ Toutes les recettes ont déjà un embedding'))
            return 0

        self.stdout.write(f'📊 {total} recette(s) sans embedding trouvée(s)')
        
        if dry_run:
            self.stdout.write(self.style.WARNING(f'[DRY-RUN] {total} recette(s) seraient traitées'))
            return total

        # Traiter les recettes une par une (car chaque recette nécessite un formatage spécifique)
        processed = 0
        failed = 0
        
        for idx, recipe in enumerate(recipes_without_embedding, 1):
            if idx % 10 == 0:
                self.stdout.write(f'🔄 Progression: {idx}/{total} recettes traitées...')
            
            try:
                # Formater la recette pour l'embedding
                # Créer une structure similaire à FormalizedRecipe pour _format_recipe_text
                class MockFormalizedRecipe:
                    def __init__(self, recipe):
                        self.title = recipe.title
                        self.description = recipe.description or ''
                        self.recipe_ingredients = [
                            type('obj', (object,), {
                                'ingredient_name': ri.ingredient.name,
                                'quantity': str(ri.quantity),
                                'unit': ri.unit,
                            })()
                            for ri in recipe.recipe_ingredients.all()
                        ]
                        self.steps = [
                            type('obj', (object,), {
                                'order': step.order,
                                'title': step.title or f'Étape {step.order}',
                                'instruction': step.instruction,
                            })()
                            for step in recipe.steps.all().order_by('order')
                        ]
                
                formalized_recipe = MockFormalizedRecipe(recipe)
                recipe_data = {
                    'categories': []  # Pas de catégories pour les recettes existantes
                }
                
                # Formater le texte de la recette (même logique que _format_recipe_text)
                parts = [
                    formalized_recipe.title or '',
                    formalized_recipe.description or '',
                ]
                
                ingredients_lines = []
                for recipe_ingredient in formalized_recipe.recipe_ingredients:
                    qty = recipe_ingredient.quantity or ''
                    unit = recipe_ingredient.unit or ''
                    line = f"{qty} {unit} {recipe_ingredient.ingredient_name}".strip()
                    ingredients_lines.append(line)
                
                parts.append("Ingredients:")
                parts.extend(ingredients_lines)
                
                step_lines = []
                for step in formalized_recipe.steps:
                    title = step.title or f"Step {step.order}"
                    instruction = step.instruction or ''
                    line = f"{title}: {instruction}"
                    step_lines.append(line)
                
                parts.append("Steps:")
                parts.extend(step_lines)
                
                if recipe_data.get('categories'):
                    parts.append(f"Categories: {', '.join(recipe_data['categories'])}")
                
                recipe_text = "\n".join(part for part in parts if part)
                
                # Générer l'embedding directement
                embeddings = get_batch_embeddings([recipe_text])
                embedding = embeddings[0] if embeddings and embeddings[0] else None
                
                if embedding:
                    recipe.embedding = embedding
                    recipe.save(update_fields=['embedding'])
                    processed += 1
                else:
                    self.stdout.write(
                        self.style.WARNING(f'⚠️  Échec de génération pour: {recipe.title} (ID: {recipe.id})')
                    )
                    failed += 1
                    
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f'❌ Erreur pour la recette {recipe.id}: {str(e)}')
                )
                failed += 1

        self.stdout.write(
            self.style.SUCCESS(
                f'\n✅ Recettes: {processed} embeddings générés, {failed} échecs'
            )
        )
        
        return processed

