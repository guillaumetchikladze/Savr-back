from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.db import connection
from recipes.models import Recipe, Step, Ingredient, RecipeIngredient, MealPlan
from datetime import date, timedelta

User = get_user_model()


class Command(BaseCommand):
    help = 'Crée des données d\'exemple pour tester l\'application'

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('Création des données d\'exemple...'))
        
        # Activer l'extension pg_trgm pour la recherche fuzzy
        try:
            with connection.cursor() as cursor:
                cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
                self.stdout.write(self.style.SUCCESS('Extension pg_trgm activée'))
        except Exception as e:
            self.stdout.write(self.style.WARNING(f'Impossible d\'activer pg_trgm: {e}'))
            self.stdout.write(self.style.WARNING('La recherche fuzzy fonctionnera avec le fallback'))
        
        # Créer un utilisateur de test s'il n'existe pas
        user, created = User.objects.get_or_create(
            email='test@example.com',
            defaults={
                'username': 'testuser',
                'first_name': 'Test',
                'last_name': 'User',
            }
        )
        if created:
            user.set_password('testpass123')
            user.save()
            self.stdout.write(self.style.SUCCESS(f'Utilisateur créé: {user.email}'))
        else:
            self.stdout.write(self.style.WARNING(f'Utilisateur existe déjà: {user.email}'))
        
        # Créer des ingrédients
        ingredients_data = [
            'Tomate', 'Oignon', 'Ail', 'Basilic', 'Huile d\'olive',
            'Sel', 'Poivre', 'Pâtes', 'Parmesan', 'Œufs',
            'Farine', 'Beurre', 'Lait', 'Sucre', 'Chocolat',
            'Poulet', 'Riz', 'Carotte', 'Courgette', 'Poivron',
            'Fromage', 'Jambon', 'Pain', 'Salade', 'Vinaigrette',
            'Saumon', 'Citron', 'Aneth', 'Pomme de terre', 'Crème fraîche',
            'Champignons', 'Bouillon', 'Bacon', 'Vin blanc', 'Échalote',
            'Thym', 'Romarin', 'Persil', 'Ciboulette', 'Mozzarella',
            'Tomate cerise', 'Avocat', 'Concombre', 'Feta', 'Olives',
            'Pignons de pin', 'Vinaigre balsamique', 'Miel', 'Moutarde',
            'Yaourt grec', 'Ail en poudre', 'Paprika', 'Cumin', 'Quinoa'
        ]
        
        ingredients = {}
        for ing_name in ingredients_data:
            ingredient, created = Ingredient.objects.get_or_create(name=ing_name)
            ingredients[ing_name] = ingredient
            if created:
                self.stdout.write(self.style.SUCCESS(f'Ingrédient créé: {ingredient.name}'))
        
        # Créer des recettes avec des étapes bien détaillées
        recipes_data = [
            {
                'title': 'Spaghetti Carbonara',
                'description': 'Un classique italien crémeux et savoureux, sans crème !',
                'meal_type': 'dinner',
                'difficulty': 'medium',
                'prep_time': 15,
                'cook_time': 15,
                'servings': 4,
                'steps': [
                    {'order': 1, 'instruction': 'Mettre une grande casserole d\'eau salée à bouillir sur feu vif.'},
                    {'order': 2, 'instruction': 'Pendant ce temps, couper le bacon en petits dés de 1 cm.'},
                    {'order': 3, 'instruction': 'Dans une poêle à feu moyen, faire revenir le bacon jusqu\'à ce qu\'il soit croustillant (environ 5 minutes).'},
                    {'order': 4, 'instruction': 'Pendant que le bacon cuit, casser les œufs dans un grand bol et les battre légèrement.'},
                    {'order': 5, 'instruction': 'Ajouter le parmesan râpé et le poivre noir moulu dans le bol avec les œufs. Mélanger.'},
                    {'order': 6, 'instruction': 'Quand l\'eau bout, ajouter les pâtes et les cuire selon les instructions sur l\'emballage (al dente).'},
                    {'order': 7, 'instruction': 'Égoutter les pâtes en réservant une louche d\'eau de cuisson.'},
                    {'order': 8, 'instruction': 'Ajouter immédiatement les pâtes chaudes dans le bol avec les œufs et mélanger rapidement.'},
                    {'order': 9, 'instruction': 'Ajouter le bacon et son gras, puis mélanger énergiquement.'},
                    {'order': 10, 'instruction': 'Si la sauce est trop épaisse, ajouter un peu d\'eau de cuisson réservée. Servir immédiatement.'},
                ],
                'ingredients': [
                    {'ingredient': 'Pâtes', 'quantity': 400, 'unit': 'g'},
                    {'ingredient': 'Bacon', 'quantity': 200, 'unit': 'g'},
                    {'ingredient': 'Œufs', 'quantity': 4, 'unit': 'piece'},
                    {'ingredient': 'Parmesan', 'quantity': 100, 'unit': 'g'},
                    {'ingredient': 'Poivre', 'quantity': 1, 'unit': 'pinch'},
                    {'ingredient': 'Sel', 'quantity': 1, 'unit': 'pinch'},
                ]
            },
            {
                'title': 'Salade César',
                'description': 'Une salade fraîche et croquante avec une vinaigrette crémeuse',
                'meal_type': 'lunch',
                'difficulty': 'easy',
                'prep_time': 15,
                'cook_time': 0,
                'servings': 2,
                'steps': [
                    {'order': 1, 'instruction': 'Laver la salade romaine à l\'eau froide et l\'essorer délicatement.'},
                    {'order': 2, 'instruction': 'Couper la salade en morceaux de 3-4 cm et la mettre dans un grand saladier.'},
                    {'order': 3, 'instruction': 'Presser le citron pour obtenir le jus dans un petit bol.'},
                    {'order': 4, 'instruction': 'Hacher finement l\'ail et l\'ajouter au jus de citron.'},
                    {'order': 5, 'instruction': 'Ajouter l\'huile d\'olive, le parmesan râpé, le sel et le poivre dans le bol.'},
                    {'order': 6, 'instruction': 'Fouetter énergiquement avec une fourchette jusqu\'à obtenir une émulsion.'},
                    {'order': 7, 'instruction': 'Verser la vinaigrette sur la salade et mélanger délicatement avec les mains.'},
                    {'order': 8, 'instruction': 'Ajouter les croûtons et le parmesan en copeaux par-dessus.'},
                    {'order': 9, 'instruction': 'Servir immédiatement pour garder la fraîcheur.'},
                ],
                'ingredients': [
                    {'ingredient': 'Salade', 'quantity': 200, 'unit': 'g'},
                    {'ingredient': 'Parmesan', 'quantity': 50, 'unit': 'g'},
                    {'ingredient': 'Huile d\'olive', 'quantity': 3, 'unit': 'tbsp'},
                    {'ingredient': 'Citron', 'quantity': 1, 'unit': 'piece'},
                    {'ingredient': 'Ail', 'quantity': 2, 'unit': 'clove'},
                    {'ingredient': 'Pain', 'quantity': 2, 'unit': 'piece'},
                ]
            },
            {
                'title': 'Poulet Rôti aux Légumes',
                'description': 'Un plat complet et équilibré, parfait pour un dîner en famille',
                'meal_type': 'dinner',
                'difficulty': 'medium',
                'prep_time': 20,
                'cook_time': 45,
                'servings': 4,
                'steps': [
                    {'order': 1, 'instruction': 'Préchauffer le four à 200°C (thermostat 6-7).'},
                    {'order': 2, 'instruction': 'Laver et éplucher les carottes, puis les couper en bâtonnets de 5 cm.'},
                    {'order': 3, 'instruction': 'Laver les courgettes et les couper en rondelles de 1 cm d\'épaisseur.'},
                    {'order': 4, 'instruction': 'Laver les poivrons, retirer les graines et les couper en lanières.'},
                    {'order': 5, 'instruction': 'Mettre tous les légumes dans un plat allant au four et les arroser d\'huile d\'olive.'},
                    {'order': 6, 'instruction': 'Assaisonner les légumes avec du sel, du poivre et les herbes (thym, romarin).'},
                    {'order': 7, 'instruction': 'Frotter le poulet avec de l\'ail écrasé, du sel et du poivre.'},
                    {'order': 8, 'instruction': 'Placer le poulet au centre du plat, entouré des légumes.'},
                    {'order': 9, 'instruction': 'Enfourner pour 45 minutes. Retourner le poulet à mi-cuisson (après 20 minutes).'},
                    {'order': 10, 'instruction': 'Vérifier la cuisson : le jus doit être clair. Laisser reposer 5 minutes avant de servir.'},
                ],
                'ingredients': [
                    {'ingredient': 'Poulet', 'quantity': 1, 'unit': 'piece'},
                    {'ingredient': 'Carotte', 'quantity': 4, 'unit': 'piece'},
                    {'ingredient': 'Courgette', 'quantity': 2, 'unit': 'piece'},
                    {'ingredient': 'Poivron', 'quantity': 2, 'unit': 'piece'},
                    {'ingredient': 'Ail', 'quantity': 3, 'unit': 'clove'},
                    {'ingredient': 'Huile d\'olive', 'quantity': 3, 'unit': 'tbsp'},
                    {'ingredient': 'Thym', 'quantity': 2, 'unit': 'tsp'},
                    {'ingredient': 'Romarin', 'quantity': 1, 'unit': 'tsp'},
                ]
            },
            {
                'title': 'Risotto aux Champignons',
                'description': 'Un risotto crémeux aux champignons, technique mais délicieux',
                'meal_type': 'dinner',
                'difficulty': 'hard',
                'prep_time': 15,
                'cook_time': 30,
                'servings': 4,
                'steps': [
                    {'order': 1, 'instruction': 'Faire chauffer le bouillon dans une casserole et le maintenir à frémissement.'},
                    {'order': 2, 'instruction': 'Nettoyer les champignons avec un torchon humide et les couper en lamelles.'},
                    {'order': 3, 'instruction': 'Dans une grande poêle, faire revenir les champignons avec un peu d\'huile jusqu\'à ce qu\'ils rendent leur eau (5 minutes).'},
                    {'order': 4, 'instruction': 'Retirer les champignons et réserver. Dans la même poêle, faire revenir l\'oignon finement haché.'},
                    {'order': 5, 'instruction': 'Quand l\'oignon est translucide, ajouter l\'ail haché et cuire 1 minute.'},
                    {'order': 6, 'instruction': 'Ajouter le riz et le faire revenir 2 minutes en remuant constamment jusqu\'à ce qu\'il devienne translucide.'},
                    {'order': 7, 'instruction': 'Verser le vin blanc et remuer jusqu\'à évaporation complète.'},
                    {'order': 8, 'instruction': 'Ajouter une louche de bouillon chaud et remuer jusqu\'à absorption complète.'},
                    {'order': 9, 'instruction': 'Répéter l\'opération (louche par louche) pendant 18-20 minutes, en remuant constamment.'},
                    {'order': 10, 'instruction': 'Le riz doit être crémeux mais encore légèrement ferme (al dente).'},
                    {'order': 11, 'instruction': 'Hors du feu, ajouter les champignons réservés, le parmesan et le beurre. Mélanger énergiquement.'},
                    {'order': 12, 'instruction': 'Rectifier l\'assaisonnement et servir immédiatement.'},
                ],
                'ingredients': [
                    {'ingredient': 'Riz', 'quantity': 300, 'unit': 'g'},
                    {'ingredient': 'Champignons', 'quantity': 300, 'unit': 'g'},
                    {'ingredient': 'Oignon', 'quantity': 1, 'unit': 'piece'},
                    {'ingredient': 'Ail', 'quantity': 2, 'unit': 'clove'},
                    {'ingredient': 'Parmesan', 'quantity': 80, 'unit': 'g'},
                    {'ingredient': 'Huile d\'olive', 'quantity': 2, 'unit': 'tbsp'},
                    {'ingredient': 'Beurre', 'quantity': 30, 'unit': 'g'},
                    {'ingredient': 'Vin blanc', 'quantity': 100, 'unit': 'ml'},
                    {'ingredient': 'Bouillon', 'quantity': 1, 'unit': 'l'},
                ]
            },
            {
                'title': 'Omelette aux Herbes',
                'description': 'Une omelette légère et parfumée, parfaite pour le petit-déjeuner',
                'meal_type': 'breakfast',
                'difficulty': 'easy',
                'prep_time': 5,
                'cook_time': 5,
                'servings': 2,
                'steps': [
                    {'order': 1, 'instruction': 'Casser les œufs dans un bol et les battre légèrement avec une fourchette.'},
                    {'order': 2, 'instruction': 'Ajouter une pincée de sel et de poivre dans les œufs battus.'},
                    {'order': 3, 'instruction': 'Hacher finement le basilic et la ciboulette.'},
                    {'order': 4, 'instruction': 'Ajouter les herbes hachées dans le bol avec les œufs et mélanger.'},
                    {'order': 5, 'instruction': 'Faire chauffer une poêle anti-adhésive à feu moyen avec le beurre.'},
                    {'order': 6, 'instruction': 'Quand le beurre mousse, verser les œufs dans la poêle.'},
                    {'order': 7, 'instruction': 'Cuire 2-3 minutes en soulevant les bords avec une spatule pour laisser couler l\'œuf cru.'},
                    {'order': 8, 'instruction': 'Quand le dessous est doré mais le dessus encore légèrement liquide, plier l\'omelette en deux.'},
                    {'order': 9, 'instruction': 'Laisser cuire 30 secondes supplémentaires, puis servir immédiatement.'},
                ],
                'ingredients': [
                    {'ingredient': 'Œufs', 'quantity': 4, 'unit': 'piece'},
                    {'ingredient': 'Basilic', 'quantity': 10, 'unit': 'g'},
                    {'ingredient': 'Ciboulette', 'quantity': 5, 'unit': 'g'},
                    {'ingredient': 'Beurre', 'quantity': 20, 'unit': 'g'},
                    {'ingredient': 'Sel', 'quantity': 1, 'unit': 'pinch'},
                    {'ingredient': 'Poivre', 'quantity': 1, 'unit': 'pinch'},
                ]
            },
            {
                'title': 'Saumon en Papillote',
                'description': 'Un saumon cuit à la vapeur dans sa papillote, tendre et savoureux',
                'meal_type': 'dinner',
                'difficulty': 'easy',
                'prep_time': 10,
                'cook_time': 20,
                'servings': 2,
                'steps': [
                    {'order': 1, 'instruction': 'Préchauffer le four à 180°C (thermostat 6).'},
                    {'order': 2, 'instruction': 'Couper deux rectangles de papier sulfurisé d\'environ 30x40 cm.'},
                    {'order': 3, 'instruction': 'Laver le citron et le couper en rondelles fines.'},
                    {'order': 4, 'instruction': 'Hacher finement l\'aneth et l\'échalote.'},
                    {'order': 5, 'instruction': 'Placer chaque pavé de saumon au centre d\'un rectangle de papier.'},
                    {'order': 6, 'instruction': 'Assaisonner le saumon avec du sel et du poivre des deux côtés.'},
                    {'order': 7, 'instruction': 'Disposer les rondelles de citron sur le saumon.'},
                    {'order': 8, 'instruction': 'Parsemer d\'aneth et d\'échalote hachés.'},
                    {'order': 9, 'instruction': 'Arroser d\'un filet d\'huile d\'olive et refermer la papillote hermétiquement.'},
                    {'order': 10, 'instruction': 'Enfourner pour 15-20 minutes selon l\'épaisseur du saumon.'},
                    {'order': 11, 'instruction': 'Servir dans la papillote, à ouvrir à table pour profiter des arômes.'},
                ],
                'ingredients': [
                    {'ingredient': 'Saumon', 'quantity': 400, 'unit': 'g'},
                    {'ingredient': 'Citron', 'quantity': 1, 'unit': 'piece'},
                    {'ingredient': 'Aneth', 'quantity': 10, 'unit': 'g'},
                    {'ingredient': 'Échalote', 'quantity': 1, 'unit': 'piece'},
                    {'ingredient': 'Huile d\'olive', 'quantity': 2, 'unit': 'tbsp'},
                    {'ingredient': 'Sel', 'quantity': 1, 'unit': 'pinch'},
                    {'ingredient': 'Poivre', 'quantity': 1, 'unit': 'pinch'},
                ]
            },
            {
                'title': 'Salade de Quinoa aux Légumes',
                'description': 'Une salade complète et nutritive, parfaite pour un déjeuner équilibré',
                'meal_type': 'lunch',
                'difficulty': 'easy',
                'prep_time': 15,
                'cook_time': 15,
                'servings': 4,
                'steps': [
                    {'order': 1, 'instruction': 'Rincer le quinoa à l\'eau froide dans une passoire fine jusqu\'à ce que l\'eau soit claire.'},
                    {'order': 2, 'instruction': 'Faire cuire le quinoa dans deux fois son volume d\'eau salée pendant 15 minutes.'},
                    {'order': 3, 'instruction': 'Pendant ce temps, laver et couper les tomates cerise en deux.'},
                    {'order': 4, 'instruction': 'Éplucher et couper le concombre en petits dés.'},
                    {'order': 5, 'instruction': 'Couper l\'avocat en cubes et l\'arroser de citron pour éviter qu\'il noircisse.'},
                    {'order': 6, 'instruction': 'Émietter la feta en petits morceaux.'},
                    {'order': 7, 'instruction': 'Quand le quinoa est cuit, l\'égoutter et le laisser refroidir complètement.'},
                    {'order': 8, 'instruction': 'Dans un grand saladier, mélanger le quinoa refroidi avec tous les légumes.'},
                    {'order': 9, 'instruction': 'Préparer la vinaigrette : mélanger l\'huile d\'olive, le vinaigre balsamique, le miel et la moutarde.'},
                    {'order': 10, 'instruction': 'Verser la vinaigrette sur la salade et mélanger délicatement.'},
                    {'order': 11, 'instruction': 'Ajouter la feta et les olives au dernier moment. Servir frais.'},
                ],
                'ingredients': [
                    {'ingredient': 'Quinoa', 'quantity': 200, 'unit': 'g'},
                    {'ingredient': 'Tomate cerise', 'quantity': 200, 'unit': 'g'},
                    {'ingredient': 'Concombre', 'quantity': 1, 'unit': 'piece'},
                    {'ingredient': 'Avocat', 'quantity': 2, 'unit': 'piece'},
                    {'ingredient': 'Feta', 'quantity': 150, 'unit': 'g'},
                    {'ingredient': 'Olives', 'quantity': 50, 'unit': 'g'},
                    {'ingredient': 'Huile d\'olive', 'quantity': 3, 'unit': 'tbsp'},
                    {'ingredient': 'Vinaigre balsamique', 'quantity': 1, 'unit': 'tbsp'},
                    {'ingredient': 'Miel', 'quantity': 1, 'unit': 'tsp'},
                    {'ingredient': 'Moutarde', 'quantity': 1, 'unit': 'tsp'},
                ]
            },
            {
                'title': 'Pâtes à la Tomate et Basilic',
                'description': 'Une recette simple et classique, toujours appréciée',
                'meal_type': 'dinner',
                'difficulty': 'easy',
                'prep_time': 10,
                'cook_time': 20,
                'servings': 4,
                'steps': [
                    {'order': 1, 'instruction': 'Mettre une grande casserole d\'eau salée à bouillir.'},
                    {'order': 2, 'instruction': 'Pendant ce temps, éplucher et hacher finement l\'oignon et l\'ail.'},
                    {'order': 3, 'instruction': 'Dans une grande poêle, faire chauffer l\'huile d\'olive à feu moyen.'},
                    {'order': 4, 'instruction': 'Faire revenir l\'oignon jusqu\'à ce qu\'il soit translucide (3-4 minutes).'},
                    {'order': 5, 'instruction': 'Ajouter l\'ail et cuire 1 minute en remuant pour éviter qu\'il brûle.'},
                    {'order': 6, 'instruction': 'Ajouter les tomates coupées en dés et assaisonner avec du sel et du poivre.'},
                    {'order': 7, 'instruction': 'Laisser mijoter à feu doux pendant 15 minutes en remuant de temps en temps.'},
                    {'order': 8, 'instruction': 'Quand l\'eau bout, ajouter les pâtes et les cuire selon les instructions.'},
                    {'order': 9, 'instruction': 'Hacher finement le basilic frais.'},
                    {'order': 10, 'instruction': 'Égoutter les pâtes en réservant une louche d\'eau de cuisson.'},
                    {'order': 11, 'instruction': 'Mélanger les pâtes avec la sauce tomate dans la poêle.'},
                    {'order': 12, 'instruction': 'Ajouter le basilic haché et le parmesan. Si nécessaire, ajouter un peu d\'eau de cuisson pour lier.'},
                    {'order': 13, 'instruction': 'Servir immédiatement avec du parmesan supplémentaire.'},
                ],
                'ingredients': [
                    {'ingredient': 'Pâtes', 'quantity': 400, 'unit': 'g'},
                    {'ingredient': 'Tomate', 'quantity': 800, 'unit': 'g'},
                    {'ingredient': 'Oignon', 'quantity': 1, 'unit': 'piece'},
                    {'ingredient': 'Ail', 'quantity': 3, 'unit': 'clove'},
                    {'ingredient': 'Basilic', 'quantity': 20, 'unit': 'g'},
                    {'ingredient': 'Parmesan', 'quantity': 80, 'unit': 'g'},
                    {'ingredient': 'Huile d\'olive', 'quantity': 3, 'unit': 'tbsp'},
                    {'ingredient': 'Sel', 'quantity': 1, 'unit': 'pinch'},
                    {'ingredient': 'Poivre', 'quantity': 1, 'unit': 'pinch'},
                ]
            },
        ]
        
        for recipe_data in recipes_data:
            steps_data = recipe_data.pop('steps')
            ingredients_list = recipe_data.pop('ingredients')
            
            recipe, created = Recipe.objects.get_or_create(
                title=recipe_data['title'],
                defaults={
                    **recipe_data,
                    'created_by': user
                }
            )
            
            if created:
                # Créer les étapes
                for step_data in steps_data:
                    Step.objects.create(recipe=recipe, **step_data)
                
                # Créer les ingrédients
                for ing_data in ingredients_list:
                    ingredient = ingredients.get(ing_data['ingredient'])
                    if ingredient:
                        RecipeIngredient.objects.create(
                            recipe=recipe,
                            ingredient=ingredient,
                            quantity=ing_data['quantity'],
                            unit=ing_data['unit']
                        )
                
                self.stdout.write(self.style.SUCCESS(f'Recette créée: {recipe.title} ({len(steps_data)} étapes)'))
            else:
                self.stdout.write(self.style.WARNING(f'Recette existe déjà: {recipe.title}'))
        
        # Créer des repas planifiés pour la semaine en cours
        today = date.today()
        meal_plans_data = [
            {'date': today, 'meal_time': 'lunch', 'meal_type': 'cantine'},
            {'date': today, 'meal_time': 'dinner', 'meal_type': 'recipe', 'recipe_title': 'Spaghetti Carbonara'},
            {'date': today + timedelta(days=1), 'meal_time': 'lunch', 'meal_type': 'recipe', 'recipe_title': 'Salade César'},
            {'date': today + timedelta(days=1), 'meal_time': 'dinner', 'meal_type': 'takeaway'},
            {'date': today + timedelta(days=2), 'meal_time': 'lunch', 'meal_type': 'cantine'},
            {'date': today + timedelta(days=2), 'meal_time': 'dinner', 'meal_type': 'recipe', 'recipe_title': 'Poulet Rôti aux Légumes'},
        ]
        
        for meal_data in meal_plans_data:
            recipe_title = meal_data.pop('recipe_title', None)
            recipe = None
            if recipe_title:
                recipe = Recipe.objects.filter(title=recipe_title).first()
            
            meal_plan, created = MealPlan.objects.get_or_create(
                user=user,
                date=meal_data['date'],
                meal_time=meal_data['meal_time'],
                defaults={
                    **meal_data,
                    'recipe': recipe
                }
            )
            
            if created:
                self.stdout.write(
                    self.style.SUCCESS(
                        f'Repas planifié: {meal_plan.date} - {meal_plan.get_meal_time_display()} - {meal_plan.get_meal_type_display()}'
                    )
                )
        
        self.stdout.write(self.style.SUCCESS('\n✅ Données d\'exemple créées avec succès!'))
        self.stdout.write(self.style.SUCCESS(f'\nUtilisateur de test: {user.email} / testpass123'))
        self.stdout.write(self.style.SUCCESS(f'\n{len(recipes_data)} recettes créées avec des étapes détaillées'))
