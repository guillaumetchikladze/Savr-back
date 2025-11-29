from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from recipes.models import Recipe


class RecipeSummaryAPITestCase(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='recipe_tester',
            email='recipe_tester@example.com',
            password='password123',
        )
        self.client.force_authenticate(self.user)

        self.import_recipe = Recipe.objects.create(
            title='Soupe de potiron',
            description='Douce et crémeuse',
            steps_summary='Cuire, mixer, savourer',
            prep_time=15,
            cook_time=30,
            created_by=self.user,
            meal_type='dinner',
            difficulty='easy',
            servings=4,
            source_type='imported',
        )

        self.favorite_recipe = Recipe.objects.create(
            title='Tarte aux pommes',
            description='Classique dorée',
            steps_summary='Pâte, garniture, cuisson',
            prep_time=20,
            cook_time=40,
            created_by=self.user,
            meal_type='snack',
            difficulty='medium',
            servings=6,
        )
        self.user.favorite_recipes.add(self.favorite_recipe)

    def test_my_imports_summary_mode_returns_count(self):
        url = reverse('recipe-my-imports')
        response = self.client.get(url, {'summary': 1})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get('count'), 1)
        self.assertIn('last_activity', response.data)

    def test_my_favorites_summary_mode_returns_count(self):
        url = reverse('recipe-my-favorites')
        response = self.client.get(url, {'summary': 1})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get('count'), 1)
        self.assertIn('last_activity', response.data)

