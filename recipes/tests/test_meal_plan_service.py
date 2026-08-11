"""Tests service meal_plan."""

from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase

from recipes.models import MealPlan, PostPhoto, Recipe, RecipeBatch, MealPlanRecipeBatch
from recipes.services.meal_plan_service import (
    add_recipes_to_meal_plan,
    create_composer_slot,
    create_meal_plan_slot,
    discard_composer_slot,
    get_meal_plans_for_user,
    propose_meal_deletion_data,
    relink_composer_photos_to_meal_plan,
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

    def test_relink_composer_photos_to_meal_plan(self):
        old_plan = MealPlan.objects.create(
            user=self.user,
            date=date(2026, 6, 9),
            meal_time='lunch',
            confirmed=False,
        )
        photo = PostPhoto.objects.create(
            meal_plan=old_plan,
            photo_type='spontaneous',
            image_path='meal_plans/old/test.jpg',
            is_draft=False,
            uploaded_by=self.user,
        )
        updated = relink_composer_photos_to_meal_plan(
            self.user, self.meal_plan, [photo.id]
        )
        self.assertEqual(updated, 1)
        photo.refresh_from_db()
        self.assertEqual(photo.meal_plan_id, self.meal_plan.id)
        self.assertIsNone(photo.recipe_batch_id)

    def test_create_composer_slot_idempotent(self):
        first = create_composer_slot(self.user, '2026-06-12', 'dinner')
        second = create_composer_slot(self.user, '2026-06-12', 'dinner')
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.meal_plan_id, second.meal_plan_id)
        self.assertEqual(
            MealPlan.objects.filter(
                user=self.user, date=date(2026, 6, 12)
            ).count(),
            1,
        )

    def test_create_composer_slot_reuses_other_when_standard_taken(self):
        lunch = MealPlan.objects.create(
            user=self.user,
            date=date(2026, 6, 13),
            meal_time='lunch',
            slot_key='lunch',
            meal_type='recipe',
            confirmed=False,
        )
        batch = RecipeBatch.objects.create(recipe=self.recipe, created_by=self.user)
        MealPlanRecipeBatch.objects.create(
            meal_plan=lunch, recipe_batch=batch, order=1
        )

        first = create_composer_slot(self.user, '2026-06-13', 'lunch')
        second = create_composer_slot(self.user, '2026-06-13', 'lunch')
        self.assertEqual(first.meal_time, 'other')
        self.assertEqual(first.meal_plan_id, second.meal_plan_id)
        self.assertEqual(
            MealPlan.objects.filter(
                user=self.user, date=date(2026, 6, 13), meal_time='other'
            ).count(),
            1,
        )

    def test_discard_composer_slot(self):
        result = create_composer_slot(self.user, '2026-06-14', 'breakfast')
        discard_composer_slot(self.user, result.meal_plan_id)
        self.assertFalse(
            MealPlan.objects.filter(id=result.meal_plan_id).exists()
        )
