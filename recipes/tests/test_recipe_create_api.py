from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from recipes.models import Ingredient, Recipe, RecipeBatch, RecipeIngredient, Step


class RecipeCreateAPITestCase(APITestCase):
    """POST /api/recipes/ — payload de l'éditeur (création manuelle)."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='recipe_creator',
            email='recipe_creator@example.com',
            password='password123',
        )
        self.user.validated_at = timezone.now()
        self.user.save(update_fields=['validated_at'])
        self.client.force_authenticate(self.user)
        self.url = reverse('recipe-list')
        self.flour = Ingredient.objects.create(name='Farine')

    def _editor_payload(self, **overrides):
        payload = {
            'title': 'Crêpes',
            'description': 'Classiques',
            'steps_summary': 'Mélanger et cuire',
            'meal_type': 'breakfast',
            'difficulty': 'easy',
            'prep_time': 10,
            'cook_time': 15,
            'servings': 4,
            'is_public': True,
            'source_type': 'user_created',
            'ingredients': [
                {'ingredient_name': 'Lait', 'quantity': 250, 'unit': 'ml'},
                {'ingredient_id': self.flour.id, 'quantity': 125, 'unit': 'g'},
            ],
            'steps': [
                {
                    'order': 0,
                    'title': 'Pâte',
                    'instruction': 'Mélanger farine et lait.',
                    'tip': '',
                    'has_timer': False,
                    'timer_duration': None,
                },
                {
                    'order': 1,
                    'title': 'Cuisson',
                    'instruction': 'Cuire 2 min de chaque côté.',
                    'tip': 'Poêle bien chaude',
                    'has_timer': True,
                    'timer_duration': 2,
                },
            ],
        }
        payload.update(overrides)
        return payload

    def test_create_sets_created_by_from_request_user(self):
        """Régression prod : created_by passé à la fois par save() et par le serializer."""
        response = self.client.post(self.url, self._editor_payload(), format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        recipe = Recipe.objects.get(id=response.data['id'])
        self.assertEqual(recipe.created_by_id, self.user.id)
        self.assertEqual(recipe.title, 'Crêpes')

    def test_create_persists_ingredients_by_name_and_id(self):
        """L'éditeur envoie ingredient_name, pas seulement ingredient_id."""
        response = self.client.post(self.url, self._editor_payload(), format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        recipe = Recipe.objects.get(id=response.data['id'])
        names = set(
            RecipeIngredient.objects.filter(recipe=recipe).values_list('ingredient__name', flat=True)
        )
        self.assertEqual(names, {'Lait', 'Farine'})
        milk = RecipeIngredient.objects.get(recipe=recipe, ingredient__name='Lait')
        self.assertEqual(milk.quantity, 250)
        self.assertEqual(milk.unit, 'ml')

    def test_create_persists_steps_and_initial_batch(self):
        response = self.client.post(self.url, self._editor_payload(), format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        recipe = Recipe.objects.get(id=response.data['id'])
        steps = list(Step.objects.filter(recipe=recipe).order_by('order'))
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0].instruction, 'Mélanger farine et lait.')
        self.assertTrue(steps[1].has_timer)
        self.assertEqual(steps[1].timer_duration, 2)
        self.assertTrue(RecipeBatch.objects.filter(recipe=recipe, created_by=self.user).exists())

    def test_create_response_includes_id(self):
        """L'app relie la recette au post via response.data.id."""
        response = self.client.post(self.url, self._editor_payload(), format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIsInstance(response.data.get('id'), int)
        self.assertEqual(response.data.get('title'), 'Crêpes')
        self.assertEqual(response.data.get('created_by'), self.user.id)

    def test_create_accepts_null_times_and_servings(self):
        """L'éditeur envoie null si les champs meta sont vides."""
        response = self.client.post(
            self.url,
            self._editor_payload(prep_time=None, cook_time=None, servings=None),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        recipe = Recipe.objects.get(id=response.data['id'])
        self.assertEqual(recipe.prep_time, 0)
        self.assertEqual(recipe.cook_time, 0)
        self.assertEqual(recipe.servings, 4)

    def test_create_ignores_duplicate_ingredient_names(self):
        payload = self._editor_payload(
            ingredients=[
                {'ingredient_name': 'Oeuf', 'quantity': 2, 'unit': 'piece'},
                {'ingredient_name': 'Oeuf', 'quantity': 1, 'unit': 'piece'},
            ]
        )
        response = self.client.post(self.url, payload, format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        recipe = Recipe.objects.get(id=response.data['id'])
        self.assertEqual(RecipeIngredient.objects.filter(recipe=recipe).count(), 1)

    def test_create_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self.client.post(self.url, self._editor_payload(), format='json')
        self.assertIn(response.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))
