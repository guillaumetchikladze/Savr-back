"""Tests d'intégration API pour shopping-list-items/with_quantities (masquage 24h des quantités)."""
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from recipes.models import (
    Ingredient,
    Recipe,
    RecipeBatch,
    ShoppingList,
    ShoppingListItem,
    ShoppingListItemQuantity,
    ShoppingListMember,
)

User = get_user_model()


class WithQuantitiesStaleIntegrationTests(APITestCase):
    """Vérifie que with_quantities exclut les sous-lignes entièrement cochées depuis > 24 h."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='shopper_int',
            email='shopper_int@example.com',
            password='password123',
        )
        self.client.force_authenticate(self.user)

        self.shopping_list = ShoppingList.objects.create(name='Liste test intégration')
        ShoppingListMember.objects.create(
            shopping_list=self.shopping_list,
            user=self.user,
            role='owner',
        )

        suffix = uuid.uuid4().hex[:8]
        self.ingredient = Ingredient.objects.create(name=f'Farine intégration {suffix}')

        self.recipe = Recipe.objects.create(
            title=f'Recette intégration {suffix}',
            description='',
            steps_summary='',
            prep_time=5,
            cook_time=0,
            created_by=self.user,
            meal_type='lunch',
            difficulty='easy',
            servings=2,
        )
        self.batch_old = RecipeBatch.objects.create(recipe=self.recipe, created_by=self.user)
        self.batch_new = RecipeBatch.objects.create(recipe=self.recipe, created_by=self.user)

        self.frozen_now = datetime(2026, 6, 15, 14, 0, 0, tzinfo=dt_timezone.utc)

    def _url(self):
        return reverse('shoppinglistitem-with-quantities')

    def _get_rows(self):
        with patch('recipes.views.timezone.now', return_value=self.frozen_now):
            r = self.client.get(
                self._url(),
                {'shopping_list_id': self.shopping_list.id},
            )
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        return r.data

    def test_excludes_line_when_only_stale_quantities(self):
        item = ShoppingListItem.objects.create(
            shopping_list=self.shopping_list,
            ingredient=self.ingredient,
            unit_group='other',
            pantry_quantity=0,
            checked_at=self.frozen_now - timedelta(hours=30),
            checked_by=self.user,
        )
        ShoppingListItemQuantity.objects.create(
            shopping_list_item=item,
            recipe_batch=self.batch_old,
            quantity=2,
            checked_quantity=2,
            unit='g',
            checked_at=self.frozen_now - timedelta(hours=30),
            checked_by=self.user,
        )

        rows = self._get_rows()
        self.assertEqual(rows, [])

    def test_hides_stale_batch_but_keeps_active_batch_and_totals(self):
        item = ShoppingListItem.objects.create(
            shopping_list=self.shopping_list,
            ingredient=self.ingredient,
            unit_group='other',
            pantry_quantity=0,
            checked_at=self.frozen_now - timedelta(hours=30),
            checked_by=self.user,
        )
        ShoppingListItemQuantity.objects.create(
            shopping_list_item=item,
            recipe_batch=self.batch_old,
            quantity=1,
            checked_quantity=1,
            unit='g',
            checked_at=self.frozen_now - timedelta(hours=30),
            checked_by=self.user,
        )
        ShoppingListItemQuantity.objects.create(
            shopping_list_item=item,
            recipe_batch=self.batch_new,
            quantity=4,
            checked_quantity=0,
            unit='g',
            checked_at=None,
            checked_by=None,
        )

        rows = self._get_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['item_id'], item.id)
        self.assertEqual(row['quantity'], 4.0)
        self.assertEqual(row['checked_quantity'], 0.0)
        self.assertIsNone(row['checked_at'])

        batch_ids = [b['batch_id'] for b in row['batches'] if not b.get('is_manual')]
        self.assertEqual(batch_ids, [self.batch_new.id])

    def test_keeps_fully_checked_line_when_checked_within_24h(self):
        item = ShoppingListItem.objects.create(
            shopping_list=self.shopping_list,
            ingredient=self.ingredient,
            unit_group='other',
            pantry_quantity=0,
            checked_at=self.frozen_now - timedelta(hours=6),
            checked_by=self.user,
        )
        ShoppingListItemQuantity.objects.create(
            shopping_list_item=item,
            recipe_batch=self.batch_old,
            quantity=1,
            checked_quantity=1,
            unit='g',
            checked_at=self.frozen_now - timedelta(hours=6),
            checked_by=self.user,
        )

        rows = self._get_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['quantity'], 1.0)
        self.assertEqual(row['checked_quantity'], 1.0)
        self.assertIsNotNone(row['checked_at'])
