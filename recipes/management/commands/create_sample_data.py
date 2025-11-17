from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.db import connection
from recipes.models import Recipe, Step, Ingredient, RecipeIngredient, StepIngredient, MealPlan
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
                'steps_summary': 'Faire cuire les pâtes, préparer le bacon croustillant, mélanger les œufs avec le parmesan, puis combiner le tout rapidement pour une sauce crémeuse.',
                'meal_type': 'dinner',
                'difficulty': 'medium',
                'prep_time': 15,
                'cook_time': 15,
                'servings': 4,
                'steps': [
                    {
                        'order': 1,
                        'title': 'Faire bouillir l\'eau',
                        'instruction': 'Mettre une grande casserole d\'eau salée à bouillir sur feu vif.',
                        'tip': '',
                        'step_ingredients': []
                    },
                    {
                        'order': 2,
                        'title': 'Couper le bacon',
                        'instruction': 'Pendant ce temps, couper le bacon en petits dés de 1 cm.',
                        'tip': 'Utilisez un couteau bien aiguisé pour des dés réguliers.',
                        'step_ingredients': [{'ingredient': 'Bacon', 'quantity': 200, 'unit': 'g'}]
                    },
                    {
                        'order': 3,
                        'title': 'Cuire le bacon',
                        'instruction': 'Dans une poêle à feu moyen, faire revenir le **bacon** jusqu\'à ce qu\'il soit croustillant (environ 5 minutes).',
                        'tip': 'Ne brûlez pas le bacon, il doit être doré et croustillant.',
                        'step_ingredients': [{'ingredient': 'Bacon', 'quantity': 200, 'unit': 'g'}]
                    },
                    {
                        'order': 4,
                        'title': 'Préparer les œufs',
                        'instruction': 'Pendant que le bacon cuit, casser les œufs dans un grand bol et les battre légèrement.',
                        'tip': '',
                        'step_ingredients': [{'ingredient': 'Œufs', 'quantity': 4, 'unit': 'piece'}]
                    },
                    {
                        'order': 5,
                        'title': 'Mélanger œufs et parmesan',
                        'instruction': 'Ajouter le **parmesan** râpé et le poivre noir moulu dans le bol avec les œufs. Mélanger.',
                        'tip': 'Râpez le parmesan frais pour un meilleur goût.',
                        'step_ingredients': [
                            {'ingredient': 'Parmesan', 'quantity': 100, 'unit': 'g'},
                            {'ingredient': 'Poivre', 'quantity': 1, 'unit': 'pinch'}
                        ]
                    },
                    {
                        'order': 6,
                        'title': 'Cuire les pâtes',
                        'instruction': 'Quand l\'eau bout, ajouter les **pâtes** et les cuire selon les instructions sur l\'emballage (al dente).',
                        'tip': 'Les pâtes doivent rester légèrement fermes.',
                        'step_ingredients': [{'ingredient': 'Pâtes', 'quantity': 400, 'unit': 'g'}]
                    },
                    {
                        'order': 7,
                        'title': 'Égoutter les pâtes',
                        'instruction': 'Égoutter les pâtes en réservant une louche d\'eau de cuisson.',
                        'tip': 'L\'eau de cuisson aidera à créer la sauce crémeuse.',
                        'step_ingredients': []
                    },
                    {
                        'order': 8,
                        'title': 'Mélanger pâtes et œufs',
                        'instruction': 'Ajouter immédiatement les pâtes chaudes dans le bol avec les œufs et mélanger rapidement.',
                        'tip': 'La chaleur des pâtes va cuire les œufs sans les faire coaguler.',
                        'step_ingredients': []
                    },
                    {
                        'order': 9,
                        'title': 'Ajouter le bacon',
                        'instruction': 'Ajouter le bacon et son gras, puis mélanger énergiquement.',
                        'tip': '',
                        'step_ingredients': []
                    },
                    {
                        'order': 10,
                        'title': 'Finaliser et servir',
                        'instruction': 'Si la sauce est trop épaisse, ajouter un peu d\'eau de cuisson réservée. Servir immédiatement.',
                        'tip': 'Servez rapidement pour éviter que les œufs ne coagulent.',
                        'step_ingredients': []
                    },
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
                'steps_summary': 'Laver et couper la salade, préparer la vinaigrette avec citron et parmesan, mélanger délicatement et ajouter les croûtons.',
                'meal_type': 'lunch',
                'difficulty': 'easy',
                'prep_time': 15,
                'cook_time': 0,
                'servings': 2,
                'steps': [
                    {'order': 1, 'title': 'Laver la salade', 'instruction': 'Laver la salade romaine à l\'eau froide et l\'essorer délicatement.', 'tip': '', 'step_ingredients': []},
                    {'order': 2, 'title': 'Couper la salade', 'instruction': 'Couper la salade en morceaux de 3-4 cm et la mettre dans un grand saladier.', 'tip': '', 'step_ingredients': []},
                    {'order': 3, 'title': 'Presser le citron', 'instruction': 'Presser le citron pour obtenir le jus dans un petit bol.', 'tip': '', 'step_ingredients': [{'ingredient': 'Citron', 'quantity': 1, 'unit': 'piece'}]},
                    {'order': 4, 'title': 'Préparer l\'ail', 'instruction': 'Hacher finement l\'ail et l\'ajouter au jus de citron.', 'tip': '', 'step_ingredients': [{'ingredient': 'Ail', 'quantity': 2, 'unit': 'clove'}]},
                    {'order': 5, 'title': 'Composer la vinaigrette', 'instruction': 'Ajouter l\'huile d\'olive, le parmesan râpé, le sel et le poivre dans le bol.', 'tip': 'Mélangez bien pour une émulsion parfaite.', 'step_ingredients': [{'ingredient': 'Huile d\'olive', 'quantity': 3, 'unit': 'tbsp'}, {'ingredient': 'Parmesan', 'quantity': 50, 'unit': 'g'}]},
                    {'order': 6, 'title': 'Émulsionner', 'instruction': 'Fouetter énergiquement avec une fourchette jusqu\'à obtenir une émulsion.', 'tip': '', 'step_ingredients': []},
                    {'order': 7, 'title': 'Assaisonner la salade', 'instruction': 'Verser la vinaigrette sur la salade et mélanger délicatement avec les mains.', 'tip': 'Utilisez vos mains pour bien enrober chaque feuille.', 'step_ingredients': []},
                    {'order': 8, 'title': 'Ajouter les garnitures', 'instruction': 'Ajouter les croûtons et le parmesan en copeaux par-dessus.', 'tip': '', 'step_ingredients': [{'ingredient': 'Pain', 'quantity': 2, 'unit': 'piece'}]},
                    {'order': 9, 'title': 'Servir', 'instruction': 'Servir immédiatement pour garder la fraîcheur.', 'tip': '', 'step_ingredients': []},
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
                'steps_summary': 'Préparer les légumes en morceaux, assaisonner le poulet, tout disposer dans un plat et cuire au four pendant 45 minutes.',
                'meal_type': 'dinner',
                'difficulty': 'medium',
                'prep_time': 20,
                'cook_time': 45,
                'servings': 4,
                'steps': [
                    {'order': 1, 'title': 'Préchauffer le four', 'instruction': 'Préchauffer le four à 200°C (thermostat 6-7).', 'tip': '', 'step_ingredients': []},
                    {'order': 2, 'title': 'Préparer les carottes', 'instruction': 'Laver et éplucher les carottes, puis les couper en bâtonnets de 5 cm.', 'tip': '', 'step_ingredients': [{'ingredient': 'Carotte', 'quantity': 4, 'unit': 'piece'}]},
                    {'order': 3, 'title': 'Couper les courgettes', 'instruction': 'Laver les courgettes et les couper en rondelles de 1 cm d\'épaisseur.', 'tip': '', 'step_ingredients': [{'ingredient': 'Courgette', 'quantity': 2, 'unit': 'piece'}]},
                    {'order': 4, 'title': 'Préparer les poivrons', 'instruction': 'Laver les poivrons, retirer les graines et les couper en lanières.', 'tip': '', 'step_ingredients': [{'ingredient': 'Poivron', 'quantity': 2, 'unit': 'piece'}]},
                    {'order': 5, 'title': 'Disposer les légumes', 'instruction': 'Mettre tous les légumes dans un plat allant au four et les arroser d\'huile d\'olive.', 'tip': '', 'step_ingredients': [{'ingredient': 'Huile d\'olive', 'quantity': 3, 'unit': 'tbsp'}]},
                    {'order': 6, 'title': 'Assaisonner les légumes', 'instruction': 'Assaisonner les légumes avec du sel, du poivre et les herbes (thym, romarin).', 'tip': '', 'step_ingredients': [{'ingredient': 'Thym', 'quantity': 2, 'unit': 'tsp'}, {'ingredient': 'Romarin', 'quantity': 1, 'unit': 'tsp'}]},
                    {'order': 7, 'title': 'Assaisonner le poulet', 'instruction': 'Frotter le poulet avec de l\'ail écrasé, du sel et du poivre.', 'tip': 'Frottez bien pour que les saveurs pénètrent.', 'step_ingredients': [{'ingredient': 'Poulet', 'quantity': 1, 'unit': 'piece'}, {'ingredient': 'Ail', 'quantity': 3, 'unit': 'clove'}]},
                    {'order': 8, 'title': 'Disposer le poulet', 'instruction': 'Placer le poulet au centre du plat, entouré des légumes.', 'tip': '', 'step_ingredients': []},
                    {'order': 9, 'title': 'Cuire au four', 'instruction': 'Enfourner pour 45 minutes. Retourner le poulet à mi-cuisson (après 20 minutes).', 'tip': 'Vérifiez la cuisson régulièrement.', 'has_timer': True, 'timer_duration': 45, 'step_ingredients': []},
                    {'order': 10, 'title': 'Vérifier et servir', 'instruction': 'Vérifier la cuisson : le jus doit être clair. Laisser reposer 5 minutes avant de servir.', 'tip': 'Le repos permet aux jus de se répartir.', 'step_ingredients': []},
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
                'steps_summary': 'Faire revenir les champignons, préparer le risotto en ajoutant le bouillon louche par louche en remuant constamment, puis incorporer les champignons et le parmesan.',
                'meal_type': 'dinner',
                'difficulty': 'hard',
                'prep_time': 15,
                'cook_time': 30,
                'servings': 4,
                'steps': [
                    {'order': 1, 'title': 'Chauffer le bouillon', 'instruction': 'Faire chauffer le bouillon dans une casserole et le maintenir à frémissement.', 'tip': 'Le bouillon doit rester chaud tout au long de la cuisson.', 'step_ingredients': [{'ingredient': 'Bouillon', 'quantity': 1, 'unit': 'l'}]},
                    {'order': 2, 'title': 'Nettoyer les champignons', 'instruction': 'Nettoyer les champignons avec un torchon humide et les couper en lamelles.', 'tip': 'Ne les lavez pas à l\'eau, ils absorberaient trop d\'humidité.', 'step_ingredients': [{'ingredient': 'Champignons', 'quantity': 300, 'unit': 'g'}]},
                    {'order': 3, 'title': 'Faire revenir les champignons', 'instruction': 'Dans une grande poêle, faire revenir les champignons avec un peu d\'huile jusqu\'à ce qu\'ils rendent leur eau (5 minutes).', 'tip': '', 'step_ingredients': []},
                    {'order': 4, 'title': 'Faire revenir l\'oignon', 'instruction': 'Retirer les champignons et réserver. Dans la même poêle, faire revenir l\'oignon finement haché.', 'tip': '', 'step_ingredients': [{'ingredient': 'Oignon', 'quantity': 1, 'unit': 'piece'}]},
                    {'order': 5, 'title': 'Ajouter l\'ail', 'instruction': 'Quand l\'oignon est translucide, ajouter l\'ail haché et cuire 1 minute.', 'tip': 'Attention à ne pas brûler l\'ail.', 'step_ingredients': [{'ingredient': 'Ail', 'quantity': 2, 'unit': 'clove'}]},
                    {'order': 6, 'title': 'Faire revenir le riz', 'instruction': 'Ajouter le riz et le faire revenir 2 minutes en remuant constamment jusqu\'à ce qu\'il devienne translucide.', 'tip': 'C\'est l\'étape cruciale pour un risotto crémeux.', 'step_ingredients': [{'ingredient': 'Riz', 'quantity': 300, 'unit': 'g'}]},
                    {'order': 7, 'title': 'Déglacer au vin', 'instruction': 'Verser le vin blanc et remuer jusqu\'à évaporation complète.', 'tip': '', 'step_ingredients': [{'ingredient': 'Vin blanc', 'quantity': 100, 'unit': 'ml'}]},
                    {'order': 8, 'title': 'Ajouter le bouillon', 'instruction': 'Ajouter une louche de bouillon chaud et remuer jusqu\'à absorption complète.', 'tip': 'Remuez constamment pour libérer l\'amidon.', 'step_ingredients': []},
                    {'order': 9, 'title': 'Continuer la cuisson', 'instruction': 'Répéter l\'opération (louche par louche) pendant 18-20 minutes, en remuant constamment.', 'tip': 'La patience est la clé d\'un bon risotto.', 'has_timer': True, 'timer_duration': 20, 'step_ingredients': []},
                    {'order': 10, 'title': 'Vérifier la texture', 'instruction': 'Le riz doit être crémeux mais encore légèrement ferme (al dente).', 'tip': '', 'step_ingredients': []},
                    {'order': 11, 'title': 'Finaliser', 'instruction': 'Hors du feu, ajouter les champignons réservés, le parmesan et le beurre. Mélanger énergiquement.', 'tip': 'Le beurre et le parmesan donnent la crémeuse finale.', 'step_ingredients': [{'ingredient': 'Parmesan', 'quantity': 80, 'unit': 'g'}, {'ingredient': 'Beurre', 'quantity': 30, 'unit': 'g'}]},
                    {'order': 12, 'title': 'Servir', 'instruction': 'Rectifier l\'assaisonnement et servir immédiatement.', 'tip': '', 'step_ingredients': []},
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
                'steps_summary': 'Battre les œufs avec les herbes hachées, cuire dans une poêle avec du beurre, puis plier en deux avant de servir.',
                'meal_type': 'breakfast',
                'difficulty': 'easy',
                'prep_time': 5,
                'cook_time': 5,
                'servings': 2,
                'steps': [
                    {'order': 1, 'title': 'Battre les œufs', 'instruction': 'Casser les œufs dans un bol et les battre légèrement avec une fourchette.', 'tip': '', 'step_ingredients': [{'ingredient': 'Œufs', 'quantity': 4, 'unit': 'piece'}]},
                    {'order': 2, 'title': 'Assaisonner', 'instruction': 'Ajouter une pincée de sel et de poivre dans les œufs battus.', 'tip': '', 'step_ingredients': []},
                    {'order': 3, 'title': 'Hacher les herbes', 'instruction': 'Hacher finement le basilic et la ciboulette.', 'tip': '', 'step_ingredients': [{'ingredient': 'Basilic', 'quantity': 10, 'unit': 'g'}, {'ingredient': 'Ciboulette', 'quantity': 5, 'unit': 'g'}]},
                    {'order': 4, 'title': 'Mélanger avec les herbes', 'instruction': 'Ajouter les herbes hachées dans le bol avec les œufs et mélanger.', 'tip': '', 'step_ingredients': []},
                    {'order': 5, 'title': 'Chauffer la poêle', 'instruction': 'Faire chauffer une poêle anti-adhésive à feu moyen avec le beurre.', 'tip': 'La poêle doit être bien chaude mais pas brûlante.', 'step_ingredients': [{'ingredient': 'Beurre', 'quantity': 20, 'unit': 'g'}]},
                    {'order': 6, 'title': 'Verser les œufs', 'instruction': 'Quand le beurre mousse, verser les œufs dans la poêle.', 'tip': '', 'step_ingredients': []},
                    {'order': 7, 'title': 'Cuire l\'omelette', 'instruction': 'Cuire 2-3 minutes en soulevant les bords avec une spatule pour laisser couler l\'œuf cru.', 'tip': 'Cette technique donne une omelette bien cuite mais moelleuse.', 'step_ingredients': []},
                    {'order': 8, 'title': 'Plier l\'omelette', 'instruction': 'Quand le dessous est doré mais le dessus encore légèrement liquide, plier l\'omelette en deux.', 'tip': '', 'step_ingredients': []},
                    {'order': 9, 'title': 'Finaliser', 'instruction': 'Laisser cuire 30 secondes supplémentaires, puis servir immédiatement.', 'tip': '', 'step_ingredients': []},
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
                'steps_summary': 'Assaisonner le saumon, l\'envelopper avec citron et herbes dans du papier sulfurisé, puis cuire au four pendant 15-20 minutes.',
                'meal_type': 'dinner',
                'difficulty': 'easy',
                'prep_time': 10,
                'cook_time': 20,
                'servings': 2,
                'steps': [
                    {'order': 1, 'title': 'Préchauffer le four', 'instruction': 'Préchauffer le four à 180°C (thermostat 6).', 'tip': '', 'step_ingredients': []},
                    {'order': 2, 'title': 'Préparer le papier', 'instruction': 'Couper deux rectangles de papier sulfurisé d\'environ 30x40 cm.', 'tip': '', 'step_ingredients': []},
                    {'order': 3, 'title': 'Couper le citron', 'instruction': 'Laver le citron et le couper en rondelles fines.', 'tip': '', 'step_ingredients': [{'ingredient': 'Citron', 'quantity': 1, 'unit': 'piece'}]},
                    {'order': 4, 'title': 'Hacher les herbes', 'instruction': 'Hacher finement l\'aneth et l\'échalote.', 'tip': '', 'step_ingredients': [{'ingredient': 'Aneth', 'quantity': 10, 'unit': 'g'}, {'ingredient': 'Échalote', 'quantity': 1, 'unit': 'piece'}]},
                    {'order': 5, 'title': 'Disposer le saumon', 'instruction': 'Placer chaque pavé de saumon au centre d\'un rectangle de papier.', 'tip': '', 'step_ingredients': [{'ingredient': 'Saumon', 'quantity': 400, 'unit': 'g'}]},
                    {'order': 6, 'title': 'Assaisonner', 'instruction': 'Assaisonner le saumon avec du sel et du poivre des deux côtés.', 'tip': '', 'step_ingredients': []},
                    {'order': 7, 'title': 'Ajouter le citron', 'instruction': 'Disposer les rondelles de citron sur le saumon.', 'tip': '', 'step_ingredients': []},
                    {'order': 8, 'title': 'Ajouter les herbes', 'instruction': 'Parsemer d\'aneth et d\'échalote hachés.', 'tip': '', 'step_ingredients': []},
                    {'order': 9, 'title': 'Fermer la papillote', 'instruction': 'Arroser d\'un filet d\'huile d\'olive et refermer la papillote hermétiquement.', 'tip': 'Assurez-vous que la papillote est bien fermée.', 'step_ingredients': [{'ingredient': 'Huile d\'olive', 'quantity': 2, 'unit': 'tbsp'}]},
                    {'order': 10, 'title': 'Cuire au four', 'instruction': 'Enfourner pour 15-20 minutes selon l\'épaisseur du saumon.', 'tip': 'Le saumon doit être opaque mais encore moelleux.', 'step_ingredients': []},
                    {'order': 11, 'title': 'Servir', 'instruction': 'Servir dans la papillote, à ouvrir à table pour profiter des arômes.', 'tip': '', 'step_ingredients': []},
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
                'steps_summary': 'Cuire le quinoa, préparer les légumes frais, préparer la vinaigrette, puis tout mélanger avec la feta et les olives.',
                'meal_type': 'lunch',
                'difficulty': 'easy',
                'prep_time': 15,
                'cook_time': 15,
                'servings': 4,
                'steps': [
                    {'order': 1, 'title': 'Rincer le quinoa', 'instruction': 'Rincer le quinoa à l\'eau froide dans une passoire fine jusqu\'à ce que l\'eau soit claire.', 'tip': 'Le rinçage enlève l\'amertume naturelle.', 'step_ingredients': [{'ingredient': 'Quinoa', 'quantity': 200, 'unit': 'g'}]},
                    {'order': 2, 'title': 'Cuire le quinoa', 'instruction': 'Faire cuire le quinoa dans deux fois son volume d\'eau salée pendant 15 minutes.', 'tip': '', 'step_ingredients': []},
                    {'order': 3, 'title': 'Couper les tomates', 'instruction': 'Pendant ce temps, laver et couper les tomates cerise en deux.', 'tip': '', 'step_ingredients': [{'ingredient': 'Tomate cerise', 'quantity': 200, 'unit': 'g'}]},
                    {'order': 4, 'title': 'Préparer le concombre', 'instruction': 'Éplucher et couper le concombre en petits dés.', 'tip': '', 'step_ingredients': [{'ingredient': 'Concombre', 'quantity': 1, 'unit': 'piece'}]},
                    {'order': 5, 'title': 'Couper l\'avocat', 'instruction': 'Couper l\'avocat en cubes et l\'arroser de citron pour éviter qu\'il noircisse.', 'tip': '', 'step_ingredients': [{'ingredient': 'Avocat', 'quantity': 2, 'unit': 'piece'}]},
                    {'order': 6, 'title': 'Émietter la feta', 'instruction': 'Émietter la feta en petits morceaux.', 'tip': '', 'step_ingredients': [{'ingredient': 'Feta', 'quantity': 150, 'unit': 'g'}]},
                    {'order': 7, 'title': 'Refroidir le quinoa', 'instruction': 'Quand le quinoa est cuit, l\'égoutter et le laisser refroidir complètement.', 'tip': 'Le quinoa froid absorbe mieux la vinaigrette.', 'step_ingredients': []},
                    {'order': 8, 'title': 'Mélanger les légumes', 'instruction': 'Dans un grand saladier, mélanger le quinoa refroidi avec tous les légumes.', 'tip': '', 'step_ingredients': []},
                    {'order': 9, 'title': 'Préparer la vinaigrette', 'instruction': 'Préparer la vinaigrette : mélanger l\'huile d\'olive, le vinaigre balsamique, le miel et la moutarde.', 'tip': '', 'step_ingredients': [{'ingredient': 'Huile d\'olive', 'quantity': 3, 'unit': 'tbsp'}, {'ingredient': 'Vinaigre balsamique', 'quantity': 1, 'unit': 'tbsp'}, {'ingredient': 'Miel', 'quantity': 1, 'unit': 'tsp'}, {'ingredient': 'Moutarde', 'quantity': 1, 'unit': 'tsp'}]},
                    {'order': 10, 'title': 'Assaisonner', 'instruction': 'Verser la vinaigrette sur la salade et mélanger délicatement.', 'tip': '', 'step_ingredients': []},
                    {'order': 11, 'title': 'Finaliser', 'instruction': 'Ajouter la feta et les olives au dernier moment. Servir frais.', 'tip': '', 'step_ingredients': [{'ingredient': 'Olives', 'quantity': 50, 'unit': 'g'}]},
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
                'steps_summary': 'Faire revenir l\'oignon et l\'ail, ajouter les tomates et laisser mijoter, cuire les pâtes, puis mélanger le tout avec le basilic et le parmesan.',
                'meal_type': 'dinner',
                'difficulty': 'easy',
                'prep_time': 10,
                'cook_time': 20,
                'servings': 4,
                'steps': [
                    {'order': 1, 'title': 'Faire bouillir l\'eau', 'instruction': 'Mettre une grande casserole d\'eau salée à bouillir.', 'tip': '', 'step_ingredients': []},
                    {'order': 2, 'title': 'Préparer l\'oignon et l\'ail', 'instruction': 'Pendant ce temps, éplucher et hacher finement l\'oignon et l\'ail.', 'tip': '', 'step_ingredients': [{'ingredient': 'Oignon', 'quantity': 1, 'unit': 'piece'}, {'ingredient': 'Ail', 'quantity': 3, 'unit': 'clove'}]},
                    {'order': 3, 'title': 'Chauffer l\'huile', 'instruction': 'Dans une grande poêle, faire chauffer l\'huile d\'olive à feu moyen.', 'tip': '', 'step_ingredients': [{'ingredient': 'Huile d\'olive', 'quantity': 3, 'unit': 'tbsp'}]},
                    {'order': 4, 'title': 'Faire revenir l\'oignon', 'instruction': 'Faire revenir l\'oignon jusqu\'à ce qu\'il soit translucide (3-4 minutes).', 'tip': '', 'step_ingredients': []},
                    {'order': 5, 'title': 'Ajouter l\'ail', 'instruction': 'Ajouter l\'ail et cuire 1 minute en remuant pour éviter qu\'il brûle.', 'tip': 'L\'ail brûlé devient amer.', 'step_ingredients': []},
                    {'order': 6, 'title': 'Ajouter les tomates', 'instruction': 'Ajouter les tomates coupées en dés et assaisonner avec du sel et du poivre.', 'tip': '', 'step_ingredients': [{'ingredient': 'Tomate', 'quantity': 800, 'unit': 'g'}]},
                    {'order': 7, 'title': 'Laisser mijoter', 'instruction': 'Laisser mijoter à feu doux pendant 15 minutes en remuant de temps en temps.', 'tip': 'Le mijotage développe les saveurs.', 'has_timer': True, 'timer_duration': 15, 'step_ingredients': []},
                    {'order': 8, 'title': 'Cuire les pâtes', 'instruction': 'Quand l\'eau bout, ajouter les pâtes et les cuire selon les instructions.', 'tip': '', 'step_ingredients': [{'ingredient': 'Pâtes', 'quantity': 400, 'unit': 'g'}]},
                    {'order': 9, 'title': 'Hacher le basilic', 'instruction': 'Hacher finement le basilic frais.', 'tip': '', 'step_ingredients': [{'ingredient': 'Basilic', 'quantity': 20, 'unit': 'g'}]},
                    {'order': 10, 'title': 'Égoutter les pâtes', 'instruction': 'Égoutter les pâtes en réservant une louche d\'eau de cuisson.', 'tip': '', 'step_ingredients': []},
                    {'order': 11, 'title': 'Mélanger avec la sauce', 'instruction': 'Mélanger les pâtes avec la sauce tomate dans la poêle.', 'tip': '', 'step_ingredients': []},
                    {'order': 12, 'title': 'Finaliser', 'instruction': 'Ajouter le basilic haché et le parmesan. Si nécessaire, ajouter un peu d\'eau de cuisson pour lier.', 'tip': '', 'step_ingredients': [{'ingredient': 'Parmesan', 'quantity': 80, 'unit': 'g'}]},
                    {'order': 13, 'title': 'Servir', 'instruction': 'Servir immédiatement avec du parmesan supplémentaire.', 'tip': '', 'step_ingredients': []},
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
            
            # Mettre à jour la recette si elle existe déjà
            if not created:
                for key, value in recipe_data.items():
                    setattr(recipe, key, value)
                recipe.save()
                # Supprimer les anciennes étapes et ingrédients
                recipe.steps.all().delete()
                recipe.recipe_ingredients.all().delete()
            
            # Créer ou mettre à jour les étapes et ingrédients
            if True:  # Toujours créer/mettre à jour
                # Créer les étapes avec leurs ingrédients
                for step_data in steps_data:
                    step_ingredients_data = step_data.pop('step_ingredients', [])
                    step = Step.objects.create(recipe=recipe, **step_data)
                    
                    # Créer les ingrédients pour cette étape
                    for step_ing_data in step_ingredients_data:
                        ingredient = ingredients.get(step_ing_data['ingredient'])
                        if ingredient:
                            StepIngredient.objects.create(
                                step=step,
                                ingredient=ingredient,
                                quantity=step_ing_data['quantity'],
                                unit=step_ing_data['unit']
                            )
                
                # Créer les ingrédients de la recette
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
