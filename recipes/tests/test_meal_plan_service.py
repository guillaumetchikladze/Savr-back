"""Tests service meal_plan."""

from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase

from recipes.models import MealPlan, Recipe, RecipeBatch, MealPlanRecipeBatch
from recipes.services.meal_plan_service import (
    add_recipes_to_meal_plan,
    create_meal_plan_slot,
    get_meal_plans_for_user,
    propose_meal_deletion_data,
    remove_recipe_from_meal_plan,
)

User = get_user_model()


class MealPlanServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pass')
        self.recipe = Recipe.objects.create(
            title='Pâtes',
            prep_time=10,
            cook_time=20,
            created_by=self.user,
            is_public=True,
        )
        self.meal_plan = MealPlan.objects.create(
            user=self.user,
            date=date(2026, 6, 10),
            meal_time='lunch',
        )

    def test_get_meal_plans_for_user(self):
        plans = get_meal_plans_for_user(
            self.user, '2026-06-01', '2026-06-30'
        )
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].id, self.meal_plan.id)

    def test_create_meal_plan_slot(self):
        result = create_meal_plan_slot(self.user, '2026-06-11', 'dinner')
        self.assertTrue(result.created)
        self.assertEqual(result.meal_time, 'dinner')
        self.assertTrue(
            MealPlan.objects.filter(
                user=self.user, date=date(2026, 6, 11), meal_time='dinner'
            ).exists()
        )

    def test_create_meal_plan_slot_idempotent(self):
        first = create_meal_plan_slot(self.user, '2026-06-11', 'dinner')
        second = create_meal_plan_slot(self.user, '2026-06-11', 'dinner')
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.meal_plan_id, second.meal_plan_id)

    def test_add_recipes_to_meal_plan(self):
        result = add_recipes_to_meal_plan(self.user, self.meal_plan.id, [self.recipe.id])
        self.assertEqual(result.added_recipe_ids, [self.recipe.id])
        self.assertTrue(
            MealPlanRecipeBatch.objects.filter(meal_plan=self.meal_plan).exists()
        )

    def test_remove_recipe_from_meal_plan(self):
        batch = RecipeBatch.objects.create(recipe=self.recipe, created_by=self.user)
        MealPlanRecipeBatch.objects.create(meal_plan=self.meal_plan, recipe_batch=batch, order=1)
        result = remove_recipe_from_meal_plan(self.user, self.meal_plan.id, batch.id)
        self.assertTrue(result['batch_deleted'])

    def test_propose_meal_deletion_data(self):
        batch = RecipeBatch.objects.create(recipe=self.recipe, created_by=self.user)
        mprb = MealPlanRecipeBatch.objects.create(
            meal_plan=self.meal_plan, recipe_batch=batch, order=1
        )
        proposal = propose_meal_deletion_data(self.user, self.meal_plan.id, batch.id)
        self.assertEqual(proposal.card_type, 'meal_deletion')
        self.assertEqual(proposal.details['recipe_batch_id'], batch.id)
