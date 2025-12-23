"""
Tests pour les ratios MealPlanRecipeBatch et l'association RecipeBatch-MealPlan.

Ces tests couvrent:
- La création et réutilisation de RecipeBatch (éviter les doublons)
- La mise à jour des ratios via entry_ratios
- Les calculs de servings avec ratios
- Les cas multi-meal-plans avec ratios différents
"""
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase
from decimal import Decimal

from recipes.models import Recipe, MealPlan, RecipeBatch, MealPlanRecipeBatch


User = get_user_model()


class MealPlanBatchRatioTestCase(APITestCase):
    """Tests pour les ratios et associations batch/meal plan"""

    def setUp(self):
        """Créer les données de test"""
        self.user = User.objects.create_user(
            username='batch_tester',
            email='batch_tester@example.com',
            password='password123',
        )
        self.client.force_authenticate(self.user)

        # Créer des recettes de test
        self.recipe_1 = Recipe.objects.create(
            title='Curry de légumes',
            description='Curry végétarien',
            steps_summary='Préparer, cuire, servir',
            prep_time=15,
            cook_time=30,
            created_by=self.user,
            meal_type='dinner',
            difficulty='easy',
            servings=4,
        )

        self.recipe_2 = Recipe.objects.create(
            title='Feuilleté aux poires',
            description='Dessert gourmand',
            steps_summary='Préparer, cuire, servir',
            prep_time=20,
            cook_time=25,
            created_by=self.user,
            meal_type='dinner',
            difficulty='medium',
            servings=6,
        )

        # Date de test
        self.test_date = timezone.now().date()

    def test_create_mealplan_with_recipe_creates_batch(self):
        """Test: Créer un meal plan avec une recette crée automatiquement un RecipeBatch"""
        url = reverse('mealplan-list')
        data = {
            'date': str(self.test_date),
            'meal_time': 'dinner',
            'meal_type': 'recipe',
            'entries': [
                {'recipe_id': self.recipe_1.id, 'batch_id': None, 'ratio': 1.0, 'order': 0}
            ]
        }

        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        meal_plan = MealPlan.objects.get(id=response.data['id'])
        
        # Vérifier qu'un RecipeBatch a été créé
        self.assertEqual(RecipeBatch.objects.count(), 1)
        batch = RecipeBatch.objects.first()
        self.assertEqual(batch.recipe_id, self.recipe_1.id)
        self.assertEqual(batch.created_by_id, self.user.id)

        # Vérifier l'association MealPlanRecipeBatch
        mprb = MealPlanRecipeBatch.objects.get(meal_plan=meal_plan)
        self.assertEqual(mprb.recipe_batch_id, batch.id)
        self.assertEqual(float(mprb.ratio), 1.0)

    def test_update_mealplan_reuses_existing_batch(self):
        """Test: Mettre à jour un meal plan avec la même recette réutilise le RecipeBatch existant"""
        # Créer un meal plan initial avec recipe_1
        meal_plan = MealPlan.objects.create(
            user=self.user,
            date=self.test_date,
            meal_time='dinner',
            meal_type='recipe',
        )
        batch_1 = RecipeBatch.objects.create(
            recipe=self.recipe_1,
            created_by=self.user
        )
        MealPlanRecipeBatch.objects.create(
            meal_plan=meal_plan,
            recipe_batch=batch_1,
            ratio=Decimal('1.0'),
            order=0
        )

        initial_batch_count = RecipeBatch.objects.count()
        self.assertEqual(initial_batch_count, 1)

        # Mettre à jour le meal plan avec la même recette (via entries)
        url = reverse('mealplan-detail', args=[meal_plan.id])
        data = {
            'date': str(self.test_date),
            'meal_time': 'dinner',
            'meal_type': 'recipe',
            'entries': [
                {'recipe_id': self.recipe_1.id, 'batch_id': None, 'ratio': 1.2, 'order': 0}
            ]
        }

        response = self.client.put(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Vérifier qu'aucun nouveau batch n'a été créé
        self.assertEqual(RecipeBatch.objects.count(), initial_batch_count)
        
        # Vérifier que le ratio a été mis à jour
        mprb = MealPlanRecipeBatch.objects.get(meal_plan=meal_plan)
        self.assertEqual(float(mprb.ratio), 1.2)
        self.assertEqual(mprb.recipe_batch_id, batch_1.id)

    def test_update_mealplan_adds_recipe_reuses_batch(self):
        """
        Test: Ajouter une recette à un meal plan existant réutilise le batch 
        si elle existe déjà dans CE meal plan.
        
        Note: La logique actuelle réutilise uniquement les batches déjà associés
        au meal plan courant (avant la mise à jour), pas globalement. 
        Pour réutiliser un batch d'un autre meal plan, il faut passer explicitement batch_id.
        """
        # Créer un meal plan avec recipe_1 et recipe_2 déjà associées
        meal_plan = MealPlan.objects.create(
            user=self.user,
            date=self.test_date,
            meal_time='dinner',
            meal_type='recipe',
        )
        batch_1 = RecipeBatch.objects.create(
            recipe=self.recipe_1,
            created_by=self.user
        )
        batch_2_existing = RecipeBatch.objects.create(
            recipe=self.recipe_2,
            created_by=self.user
        )
        MealPlanRecipeBatch.objects.create(
            meal_plan=meal_plan,
            recipe_batch=batch_1,
            ratio=Decimal('1.0'),
            order=0
        )
        # batch_2 est déjà associé à ce meal plan
        MealPlanRecipeBatch.objects.create(
            meal_plan=meal_plan,
            recipe_batch=batch_2_existing,
            ratio=Decimal('1.0'),
            order=1
        )

        initial_batch_count = RecipeBatch.objects.count()
        self.assertEqual(initial_batch_count, 2)

        # Mettre à jour le meal plan en gardant les deux recettes mais en changeant le ratio de recipe_2
        # (simule le cas où on modifie les ratios via entries)
        url = reverse('mealplan-detail', args=[meal_plan.id])
        data = {
            'date': str(self.test_date),
            'meal_time': 'dinner',
            'meal_type': 'recipe',
            'entries': [
                {'recipe_id': self.recipe_1.id, 'batch_id': None, 'ratio': 1.0, 'order': 0},
                {'recipe_id': self.recipe_2.id, 'batch_id': None, 'ratio': 0.8, 'order': 1}
            ]
        }

        response = self.client.put(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Vérifier qu'aucun nouveau batch n'a été créé
        # (car les deux batches étaient déjà associés à ce meal plan)
        self.assertEqual(RecipeBatch.objects.count(), initial_batch_count)
        
        # Vérifier les associations
        mprbs = MealPlanRecipeBatch.objects.filter(meal_plan=meal_plan).order_by('order')
        self.assertEqual(mprbs.count(), 2)
        
        # Vérifier que recipe_2 utilise le batch existant avec le nouveau ratio
        mprb_recipe_2 = mprbs.filter(recipe_batch__recipe=self.recipe_2).first()
        self.assertIsNotNone(mprb_recipe_2)
        self.assertEqual(mprb_recipe_2.recipe_batch_id, batch_2_existing.id)
        self.assertEqual(float(mprb_recipe_2.ratio), 0.8)

    def test_update_ratios_via_entry_ratios(self):
        """Test: Mettre à jour uniquement les ratios via entry_ratios sans créer de nouveaux batches"""
        # Créer un meal plan avec deux recettes
        meal_plan = MealPlan.objects.create(
            user=self.user,
            date=self.test_date,
            meal_time='dinner',
            meal_type='recipe',
        )
        batch_1 = RecipeBatch.objects.create(recipe=self.recipe_1, created_by=self.user)
        batch_2 = RecipeBatch.objects.create(recipe=self.recipe_2, created_by=self.user)
        
        MealPlanRecipeBatch.objects.create(
            meal_plan=meal_plan,
            recipe_batch=batch_1,
            ratio=Decimal('1.0'),
            order=0
        )
        MealPlanRecipeBatch.objects.create(
            meal_plan=meal_plan,
            recipe_batch=batch_2,
            ratio=Decimal('1.0'),
            order=1
        )

        initial_batch_count = RecipeBatch.objects.count()
        self.assertEqual(initial_batch_count, 2)

        # Mettre à jour uniquement les ratios via entry_ratios
        url = reverse('mealplan-detail', args=[meal_plan.id])
        data = {
            'entry_ratios': {
                str(batch_1.id): 0.6,
                str(batch_2.id): 1.2
            }
        }

        response = self.client.patch(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Vérifier qu'aucun nouveau batch n'a été créé
        self.assertEqual(RecipeBatch.objects.count(), initial_batch_count)

        # Vérifier que les ratios ont été mis à jour
        mprb_1 = MealPlanRecipeBatch.objects.get(meal_plan=meal_plan, recipe_batch=batch_1)
        mprb_2 = MealPlanRecipeBatch.objects.get(meal_plan=meal_plan, recipe_batch=batch_2)
        
        self.assertAlmostEqual(float(mprb_1.ratio), 0.6, places=2)
        self.assertAlmostEqual(float(mprb_2.ratio), 1.2, places=2)

    def test_update_ratios_via_recipe_ids_and_ratios(self):
        """Test: Mettre à jour les ratios via recipe_ids et recipe_ratios (ancienne API)"""
        # Créer un meal plan avec deux recettes
        meal_plan = MealPlan.objects.create(
            user=self.user,
            date=self.test_date,
            meal_time='dinner',
            meal_type='recipe',
        )
        batch_1 = RecipeBatch.objects.create(recipe=self.recipe_1, created_by=self.user)
        batch_2 = RecipeBatch.objects.create(recipe=self.recipe_2, created_by=self.user)
        
        MealPlanRecipeBatch.objects.create(
            meal_plan=meal_plan,
            recipe_batch=batch_1,
            ratio=Decimal('1.0'),
            order=0
        )
        MealPlanRecipeBatch.objects.create(
            meal_plan=meal_plan,
            recipe_batch=batch_2,
            ratio=Decimal('1.0'),
            order=1
        )

        initial_batch_count = RecipeBatch.objects.count()

        # Mettre à jour via recipe_ids et recipe_ratios
        url = reverse('mealplan-detail', args=[meal_plan.id])
        data = {
            'recipe_ids': [self.recipe_1.id, self.recipe_2.id],
            'recipe_ratios': {
                str(self.recipe_1.id): 0.8,
                str(self.recipe_2.id): 1.1
            }
        }

        response = self.client.patch(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Vérifier qu'aucun nouveau batch n'a été créé
        self.assertEqual(RecipeBatch.objects.count(), initial_batch_count)

        # Vérifier que les ratios ont été mis à jour pour les batches existants
        mprb_1 = MealPlanRecipeBatch.objects.get(meal_plan=meal_plan, recipe_batch__recipe=self.recipe_1)
        mprb_2 = MealPlanRecipeBatch.objects.get(meal_plan=meal_plan, recipe_batch__recipe=self.recipe_2)
        
        self.assertAlmostEqual(float(mprb_1.ratio), 0.8, places=2)
        self.assertAlmostEqual(float(mprb_2.ratio), 1.1, places=2)

    def test_servings_calculation_with_ratios(self):
        """Test: Le calcul de servings prend en compte les ratios"""
        from recipes.serializers import compute_meal_plan_servings_with_ratio

        meal_plan = MealPlan.objects.create(
            user=self.user,
            date=self.test_date,
            meal_time='dinner',
            meal_type='recipe',
            guest_count=2,  # 2 invités
        )

        batch_1 = RecipeBatch.objects.create(recipe=self.recipe_1, created_by=self.user)
        batch_2 = RecipeBatch.objects.create(recipe=self.recipe_2, created_by=self.user)
        
        # Créer des invitations (simule 1 participant accepté)
        from recipes.models import MealInvitation
        other_user = User.objects.create_user(
            username='participant',
            email='participant@example.com',
            password='password123'
        )
        MealInvitation.objects.create(
            meal_plan=meal_plan,
            inviter=self.user,
            invitee=other_user,
            status='accepted'
        )

        # Associer les batches avec des ratios différents
        MealPlanRecipeBatch.objects.create(
            meal_plan=meal_plan,
            recipe_batch=batch_1,
            ratio=Decimal('0.6'),
            order=0
        )
        MealPlanRecipeBatch.objects.create(
            meal_plan=meal_plan,
            recipe_batch=batch_2,
            ratio=Decimal('1.0'),
            order=1
        )

        # Calculer les servings
        # base_servings = 1 (user) + 1 (participant accepté) + 2 (guests) = 4
        # batch_1: 4 * 0.6 = 2.4
        # batch_2: 4 * 1.0 = 4.0
        # total = 2.4 + 4.0 = 6.4
        total_servings, breakdown, base_servings = compute_meal_plan_servings_with_ratio(meal_plan)

        self.assertEqual(base_servings, 4)
        self.assertAlmostEqual(total_servings, 6.4, places=1)
        self.assertEqual(len(breakdown), 2)
        
        # Vérifier le breakdown
        breakdown_batch_1 = next((b for b in breakdown if b['recipe_batch_id'] == batch_1.id), None)
        breakdown_batch_2 = next((b for b in breakdown if b['recipe_batch_id'] == batch_2.id), None)
        
        self.assertIsNotNone(breakdown_batch_1)
        self.assertAlmostEqual(breakdown_batch_1['adjusted_servings'], 2.4, places=1)
        self.assertAlmostEqual(breakdown_batch_1['ratio'], 0.6, places=2)
        
        self.assertIsNotNone(breakdown_batch_2)
        self.assertAlmostEqual(breakdown_batch_2['adjusted_servings'], 4.0, places=1)
        self.assertAlmostEqual(breakdown_batch_2['ratio'], 1.0, places=2)

    def test_multiple_mealplans_same_batch_different_ratios(self):
        """Test: Plusieurs meal plans peuvent utiliser le même batch avec des ratios différents"""
        # Créer deux meal plans
        meal_plan_1 = MealPlan.objects.create(
            user=self.user,
            date=self.test_date,
            meal_time='dinner',
            meal_type='recipe',
        )
        meal_plan_2 = MealPlan.objects.create(
            user=self.user,
            date=self.test_date + timezone.timedelta(days=1),
            meal_time='dinner',
            meal_type='recipe',
        )

        # Créer un batch partagé
        shared_batch = RecipeBatch.objects.create(
            recipe=self.recipe_1,
            created_by=self.user
        )

        # Associer le même batch aux deux meal plans avec des ratios différents
        MealPlanRecipeBatch.objects.create(
            meal_plan=meal_plan_1,
            recipe_batch=shared_batch,
            ratio=Decimal('0.6'),
            order=0
        )
        MealPlanRecipeBatch.objects.create(
            meal_plan=meal_plan_2,
            recipe_batch=shared_batch,
            ratio=Decimal('1.2'),
            order=0
        )

        # Vérifier qu'un seul batch existe
        self.assertEqual(RecipeBatch.objects.count(), 1)

        # Vérifier les ratios différents
        mprb_1 = MealPlanRecipeBatch.objects.get(meal_plan=meal_plan_1)
        mprb_2 = MealPlanRecipeBatch.objects.get(meal_plan=meal_plan_2)
        
        self.assertEqual(mprb_1.recipe_batch_id, shared_batch.id)
        self.assertEqual(mprb_2.recipe_batch_id, shared_batch.id)
        self.assertAlmostEqual(float(mprb_1.ratio), 0.6, places=2)
        self.assertAlmostEqual(float(mprb_2.ratio), 1.2, places=2)

    def test_update_mealplan_removes_recipe_preserves_batch(self):
        """Test: Retirer une recette d'un meal plan ne supprime pas le RecipeBatch"""
        meal_plan = MealPlan.objects.create(
            user=self.user,
            date=self.test_date,
            meal_time='dinner',
            meal_type='recipe',
        )
        batch_1 = RecipeBatch.objects.create(recipe=self.recipe_1, created_by=self.user)
        batch_2 = RecipeBatch.objects.create(recipe=self.recipe_2, created_by=self.user)
        
        MealPlanRecipeBatch.objects.create(
            meal_plan=meal_plan,
            recipe_batch=batch_1,
            ratio=Decimal('1.0'),
            order=0
        )
        MealPlanRecipeBatch.objects.create(
            meal_plan=meal_plan,
            recipe_batch=batch_2,
            ratio=Decimal('1.0'),
            order=1
        )

        initial_batch_count = RecipeBatch.objects.count()
        self.assertEqual(initial_batch_count, 2)

        # Retirer recipe_2 du meal plan (ne garder que recipe_1)
        url = reverse('mealplan-detail', args=[meal_plan.id])
        data = {
            'date': str(self.test_date),
            'meal_time': 'dinner',
            'meal_type': 'recipe',
            'entries': [
                {'recipe_id': self.recipe_1.id, 'batch_id': batch_1.id, 'ratio': 1.0, 'order': 0}
            ]
        }

        response = self.client.put(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Vérifier que les batches existent toujours
        self.assertEqual(RecipeBatch.objects.count(), initial_batch_count)
        self.assertTrue(RecipeBatch.objects.filter(id=batch_1.id).exists())
        self.assertTrue(RecipeBatch.objects.filter(id=batch_2.id).exists())

        # Vérifier que seule l'association avec batch_1 reste
        mprbs = MealPlanRecipeBatch.objects.filter(meal_plan=meal_plan)
        self.assertEqual(mprbs.count(), 1)
        self.assertEqual(mprbs.first().recipe_batch_id, batch_1.id)

    def test_ratio_precision_rounding(self):
        """Test: Les ratios sont correctement arrondis à 2 décimales"""
        meal_plan = MealPlan.objects.create(
            user=self.user,
            date=self.test_date,
            meal_time='dinner',
            meal_type='recipe',
        )
        batch = RecipeBatch.objects.create(recipe=self.recipe_1, created_by=self.user)
        
        # Créer d'abord l'association MealPlanRecipeBatch (entry_ratios ne crée pas de nouvelles associations)
        MealPlanRecipeBatch.objects.create(
            meal_plan=meal_plan,
            recipe_batch=batch,
            ratio=Decimal('1.0'),
            order=0
        )

        # Tester avec un ratio à beaucoup de décimales
        url = reverse('mealplan-detail', args=[meal_plan.id])
        data = {
            'entry_ratios': {
                str(batch.id): 0.6666666666666666  # Devrait être arrondi à 0.67
            }
        }

        response = self.client.patch(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        mprb = MealPlanRecipeBatch.objects.get(meal_plan=meal_plan, recipe_batch=batch)
        # Vérifier que le ratio est arrondi à 2 décimales
        ratio_str = str(mprb.ratio)
        decimal_places = len(ratio_str.split('.')[-1]) if '.' in ratio_str else 0
        self.assertLessEqual(decimal_places, 2)
        # Vérifier aussi la valeur arrondie
        self.assertAlmostEqual(float(mprb.ratio), 0.67, places=2)

