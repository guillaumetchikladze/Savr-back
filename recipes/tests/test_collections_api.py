from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from recipes.models import Collection, CollectionRecipe, Recipe


class CollectionAPITestCase(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='tester',
            email='tester@example.com',
            password='password123',
        )
        self.client.force_authenticate(self.user)

        self.collection = Collection.objects.create(
            name='Brunch goals',
            owner=self.user,
            description='Recettes du week-end',
        )

        self.recipe_in_collection = Recipe.objects.create(
            title='Pain perdu vanille',
            description='Gourmand',
            steps_summary='Tremper, caraméliser, déguster',
            prep_time=10,
            cook_time=8,
            created_by=self.user,
            meal_type='breakfast',
            difficulty='easy',
            servings=2,
        )

        self.suggestion_recipe = Recipe.objects.create(
            title='Smoothie mangue',
            description='Frais et fruité',
            steps_summary='Mixer et servir',
            prep_time=5,
            cook_time=0,
            created_by=self.user,
            meal_type='breakfast',
            difficulty='easy',
            servings=1,
        )

        CollectionRecipe.objects.create(
            collection=self.collection,
            recipe=self.recipe_in_collection,
            added_by=self.user,
        )

    def test_collection_recipes_endpoint_returns_paginated_payload(self):
        url = reverse('collection-recipes', args=[self.collection.id])
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('results', response.data)
        self.assertEqual(len(response.data['results']), 1)
        first_entry = response.data['results'][0]
        self.assertEqual(first_entry['recipe']['id'], self.recipe_in_collection.id)
        self.assertEqual(first_entry['recipe']['title'], self.recipe_in_collection.title)

    def test_suggestions_endpoint_skips_existing_recipes(self):
        url = reverse('collection-suggestions', args=[self.collection.id])
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('results', response.data)
        suggestion_ids = [item['id'] for item in response.data['results']]
        self.assertIn(self.suggestion_recipe.id, suggestion_ids)
        self.assertNotIn(self.recipe_in_collection.id, suggestion_ids)

