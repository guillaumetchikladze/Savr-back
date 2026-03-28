"""
Tests pour MealPlanRecipeBatch (portions) et l'association RecipeBatch–MealPlan.

Couverture :
- Création et réutilisation de RecipeBatch
- Mise à jour des portions via `entries` ou `entry_portions`
- Breakdown / helpers de portions
- Cas multi–meal-plans avec portions différentes
"""
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from recipes.models import Recipe, MealPlan, RecipeBatch, MealPlanRecipeBatch


User = get_user_model()


class MealPlanBatchPortionsTestCase(APITestCase):
    """Tests portions et associations batch / meal plan"""

    def setUp(self):
        self.user = User.objects.create_user(
            username='batch_tester',
            email='batch_tester@example.com',
            password='password123',
        )
        self.client.force_authenticate(self.user)

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

        self.test_date = timezone.now().date()

    def test_create_mealplan_with_recipe_creates_batch(self):
        """Créer un meal plan avec une recette crée un RecipeBatch ; portions optionnelles."""
        url = reverse('mealplan-list')
        data = {
            'date': str(self.test_date),
            'meal_time': 'dinner',
            'meal_type': 'recipe',
            'entries': [
                {'recipe_id': self.recipe_1.id, 'batch_id': None, 'order': 0}
            ],
        }

        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        meal_plan = MealPlan.objects.get(id=response.data['id'])

        self.assertEqual(RecipeBatch.objects.count(), 1)
        batch = RecipeBatch.objects.first()
        self.assertEqual(batch.recipe_id, self.recipe_1.id)
        self.assertEqual(batch.created_by_id, self.user.id)

        mprb = MealPlanRecipeBatch.objects.get(meal_plan=meal_plan)
        self.assertEqual(mprb.recipe_batch_id, batch.id)
        self.assertIsNone(mprb.portions)
        self.assertFalse(mprb.is_portions_overridden)

    def test_update_mealplan_reuses_existing_batch(self):
        """Mise à jour : même recette réutilise le batch ; portions modifiables via entries."""
        meal_plan = MealPlan.objects.create(
            user=self.user,
            date=self.test_date,
            meal_time='dinner',
            meal_type='recipe',
        )
        batch_1 = RecipeBatch.objects.create(
            recipe=self.recipe_1,
            created_by=self.user,
        )
        MealPlanRecipeBatch.objects.create(
            meal_plan=meal_plan,
            recipe_batch=batch_1,
            portions=None,
            is_portions_overridden=False,
            order=0,
        )

        initial_batch_count = RecipeBatch.objects.count()
        self.assertEqual(initial_batch_count, 1)

        url = reverse('mealplan-detail', args=[meal_plan.id])
        data = {
            'date': str(self.test_date),
            'meal_time': 'dinner',
            'meal_type': 'recipe',
            'entries': [
                {'recipe_id': self.recipe_1.id, 'batch_id': None, 'portions': 5, 'order': 0}
            ],
        }

        response = self.client.put(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertEqual(RecipeBatch.objects.count(), initial_batch_count)

        mprb = MealPlanRecipeBatch.objects.get(meal_plan=meal_plan)
        self.assertEqual(mprb.recipe_batch_id, batch_1.id)
        self.assertEqual(mprb.portions, 5)
        self.assertTrue(mprb.is_portions_overridden)

    def test_update_mealplan_adds_recipe_reuses_batch(self):
        """Deux recettes sur le même meal plan : réutilisation des batches connus."""
        meal_plan = MealPlan.objects.create(
            user=self.user,
            date=self.test_date,
            meal_time='dinner',
            meal_type='recipe',
        )
        batch_1 = RecipeBatch.objects.create(
            recipe=self.recipe_1,
            created_by=self.user,
        )
        batch_2_existing = RecipeBatch.objects.create(
            recipe=self.recipe_2,
            created_by=self.user,
        )
        MealPlanRecipeBatch.objects.create(
            meal_plan=meal_plan,
            recipe_batch=batch_1,
            portions=None,
            is_portions_overridden=False,
            order=0,
        )
        MealPlanRecipeBatch.objects.create(
            meal_plan=meal_plan,
            recipe_batch=batch_2_existing,
            portions=None,
            is_portions_overridden=False,
            order=1,
        )

        initial_batch_count = RecipeBatch.objects.count()
        self.assertEqual(initial_batch_count, 2)

        url = reverse('mealplan-detail', args=[meal_plan.id])
        data = {
            'date': str(self.test_date),
            'meal_time': 'dinner',
            'meal_type': 'recipe',
            'entries': [
                {'recipe_id': self.recipe_1.id, 'batch_id': None, 'portions': 4, 'order': 0},
                {'recipe_id': self.recipe_2.id, 'batch_id': None, 'portions': 3, 'order': 1},
            ],
        }

        response = self.client.put(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertEqual(RecipeBatch.objects.count(), initial_batch_count)

        mprbs = MealPlanRecipeBatch.objects.filter(meal_plan=meal_plan).order_by('order')
        self.assertEqual(mprbs.count(), 2)

        mprb_recipe_2 = mprbs.filter(recipe_batch__recipe=self.recipe_2).first()
        self.assertIsNotNone(mprb_recipe_2)
        self.assertEqual(mprb_recipe_2.recipe_batch_id, batch_2_existing.id)
        self.assertEqual(mprb_recipe_2.portions, 3)

    def test_update_portions_via_entry_portions(self):
        """PATCH entry_portions : met à jour les portions sans créer de nouveaux batches."""
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
            portions=None,
            is_portions_overridden=False,
            order=0,
        )
        MealPlanRecipeBatch.objects.create(
            meal_plan=meal_plan,
            recipe_batch=batch_2,
            portions=None,
            is_portions_overridden=False,
            order=1,
        )

        initial_batch_count = RecipeBatch.objects.count()
        self.assertEqual(initial_batch_count, 2)

        url = reverse('mealplan-detail', args=[meal_plan.id])
        data = {
            'entry_portions': {
                str(batch_1.id): 6,
                str(batch_2.id): 8,
            }
        }

        response = self.client.patch(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertEqual(RecipeBatch.objects.count(), initial_batch_count)

        mprb_1 = MealPlanRecipeBatch.objects.get(meal_plan=meal_plan, recipe_batch=batch_1)
        mprb_2 = MealPlanRecipeBatch.objects.get(meal_plan=meal_plan, recipe_batch=batch_2)

        self.assertEqual(mprb_1.portions, 6)
        self.assertTrue(mprb_1.is_portions_overridden)
        self.assertEqual(mprb_2.portions, 8)
        self.assertTrue(mprb_2.is_portions_overridden)

    def test_servings_breakdown_with_portions(self):
        """compute_meal_plan_servings_with_ratio : breakdown avec portions effectives."""
        from recipes.serializers import compute_meal_plan_servings_with_ratio

        meal_plan = MealPlan.objects.create(
            user=self.user,
            date=self.test_date,
            meal_time='dinner',
            meal_type='recipe',
            guest_count=2,
        )

        batch_1 = RecipeBatch.objects.create(recipe=self.recipe_1, created_by=self.user)
        batch_2 = RecipeBatch.objects.create(recipe=self.recipe_2, created_by=self.user)

        from recipes.models import MealInvitation

        other_user = User.objects.create_user(
            username='participant',
            email='participant@example.com',
            password='password123',
        )
        MealInvitation.objects.create(
            meal_plan=meal_plan,
            inviter=self.user,
            invitee=other_user,
            status='accepted',
        )

        MealPlanRecipeBatch.objects.create(
            meal_plan=meal_plan,
            recipe_batch=batch_1,
            portions=2,
            is_portions_overridden=True,
            order=0,
        )
        MealPlanRecipeBatch.objects.create(
            meal_plan=meal_plan,
            recipe_batch=batch_2,
            portions=None,
            is_portions_overridden=False,
            order=1,
        )

        people_count, breakdown, base_servings = compute_meal_plan_servings_with_ratio(meal_plan)

        self.assertEqual(base_servings, 4)
        self.assertEqual(people_count, 4)
        self.assertEqual(len(breakdown), 2)

        b1 = next(b for b in breakdown if b['recipe_batch_id'] == batch_1.id)
        b2 = next(b for b in breakdown if b['recipe_batch_id'] == batch_2.id)
        self.assertEqual(b1['portions'], 2)
        self.assertEqual(b1['base_servings'], 4)
        self.assertEqual(b2['portions'], 4)
        self.assertEqual(b2['base_servings'], 4)

    def test_multiple_mealplans_same_batch_different_portions(self):
        """Même batch sur deux meal plans : portions indépendantes."""
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

        shared_batch = RecipeBatch.objects.create(
            recipe=self.recipe_1,
            created_by=self.user,
        )

        MealPlanRecipeBatch.objects.create(
            meal_plan=meal_plan_1,
            recipe_batch=shared_batch,
            portions=3,
            is_portions_overridden=True,
            order=0,
        )
        MealPlanRecipeBatch.objects.create(
            meal_plan=meal_plan_2,
            recipe_batch=shared_batch,
            portions=7,
            is_portions_overridden=True,
            order=0,
        )

        self.assertEqual(RecipeBatch.objects.count(), 1)

        mprb_1 = MealPlanRecipeBatch.objects.get(meal_plan=meal_plan_1)
        mprb_2 = MealPlanRecipeBatch.objects.get(meal_plan=meal_plan_2)

        self.assertEqual(mprb_1.recipe_batch_id, shared_batch.id)
        self.assertEqual(mprb_2.recipe_batch_id, shared_batch.id)
        self.assertEqual(mprb_1.portions, 3)
        self.assertEqual(mprb_2.portions, 7)

    def test_update_mealplan_removes_recipe_preserves_batch(self):
        """Retirer une recette du meal plan ne supprime pas les RecipeBatch."""
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
            portions=None,
            is_portions_overridden=False,
            order=0,
        )
        MealPlanRecipeBatch.objects.create(
            meal_plan=meal_plan,
            recipe_batch=batch_2,
            portions=None,
            is_portions_overridden=False,
            order=1,
        )

        initial_batch_count = RecipeBatch.objects.count()
        self.assertEqual(initial_batch_count, 2)

        url = reverse('mealplan-detail', args=[meal_plan.id])
        data = {
            'date': str(self.test_date),
            'meal_time': 'dinner',
            'meal_type': 'recipe',
            'entries': [
                {'recipe_id': self.recipe_1.id, 'batch_id': batch_1.id, 'order': 0},
            ],
        }

        response = self.client.put(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertEqual(RecipeBatch.objects.count(), initial_batch_count)
        self.assertTrue(RecipeBatch.objects.filter(id=batch_1.id).exists())
        self.assertTrue(RecipeBatch.objects.filter(id=batch_2.id).exists())

        mprbs = MealPlanRecipeBatch.objects.filter(meal_plan=meal_plan)
        self.assertEqual(mprbs.count(), 1)
        self.assertEqual(mprbs.first().recipe_batch_id, batch_1.id)

    def test_entry_portions_integer_values(self):
        """entry_portions accepte des entiers positifs."""
        meal_plan = MealPlan.objects.create(
            user=self.user,
            date=self.test_date,
            meal_time='dinner',
            meal_type='recipe',
        )
        batch = RecipeBatch.objects.create(recipe=self.recipe_1, created_by=self.user)
        MealPlanRecipeBatch.objects.create(
            meal_plan=meal_plan,
            recipe_batch=batch,
            portions=None,
            is_portions_overridden=False,
            order=0,
        )

        url = reverse('mealplan-detail', args=[meal_plan.id])
        data = {'entry_portions': {str(batch.id): 12}}

        response = self.client.patch(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        mprb = MealPlanRecipeBatch.objects.get(meal_plan=meal_plan, recipe_batch=batch)
        self.assertEqual(mprb.portions, 12)
        self.assertTrue(mprb.is_portions_overridden)
