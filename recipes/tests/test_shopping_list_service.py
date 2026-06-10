"""Tests service shopping_list."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from recipes.models import (
    Ingredient,
    ShoppingList,
    ShoppingListItem,
    ShoppingListItemQuantity,
    ShoppingListMember,
)
from recipes.services.shopping_list_service import (
    add_item_to_shopping_list,
    get_shopping_list_items_for_user,
)

User = get_user_model()


class ShoppingListServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='shopper',
            email='shopper@test.com',
            password='pass',
        )
        self.other = User.objects.create_user(
            username='other',
            email='other@test.com',
            password='pass',
        )
        self.shopping_list = ShoppingList.objects.create(name='Maison')
        ShoppingListMember.objects.create(
            shopping_list=self.shopping_list,
            user=self.user,
            role='owner',
        )
        self.ingredient = Ingredient.objects.create(name='Lait')

    def test_get_shopping_list_items_empty(self):
        result = get_shopping_list_items_for_user(self.user, self.shopping_list.id)
        self.assertEqual(result.count, 0)
        self.assertEqual(result.shopping_list_name, 'Maison')

    def test_get_shopping_list_items_to_buy(self):
        item = ShoppingListItem.objects.create(
            shopping_list=self.shopping_list,
            ingredient=self.ingredient,
            unit_group='volume',
            pantry_unit='l',
        )
        ShoppingListItemQuantity.objects.create(
            shopping_list_item=item,
            recipe_batch=None,
            quantity=Decimal('2'),
            unit='l',
        )

        result = get_shopping_list_items_for_user(self.user, self.shopping_list.id)
        self.assertEqual(result.count, 1)
        self.assertEqual(result.items[0].ingredient_name, 'Lait')
        self.assertEqual(result.items[0].remaining_quantity, 2.0)
        self.assertEqual(result.items[0].status, 'to_buy')

    def test_get_shopping_list_items_denied(self):
        with self.assertRaises(ValueError):
            get_shopping_list_items_for_user(self.other, self.shopping_list.id)

    def test_add_shopping_list_item_creates(self):
        result = add_item_to_shopping_list(
            self.user,
            self.shopping_list.id,
            'Tomates',
            quantity=500,
            unit='g',
        )
        self.assertTrue(result.created)
        self.assertEqual(result.ingredient_name, 'Tomates')
        self.assertEqual(result.quantity, 500.0)
        self.assertEqual(result.unit, 'g')
        self.assertTrue(
            ShoppingListItem.objects.filter(
                shopping_list=self.shopping_list,
                ingredient__name__iexact='Tomates',
            ).exists()
        )

    def test_add_shopping_list_item_merges_quantity(self):
        add_item_to_shopping_list(
            self.user,
            self.shopping_list.id,
            'Oignons',
            quantity=2,
            unit='piece',
        )
        result = add_item_to_shopping_list(
            self.user,
            self.shopping_list.id,
            'Oignons',
            quantity=3,
            unit='piece',
        )
        self.assertFalse(result.created)
        self.assertEqual(result.quantity, 3.0)
        item = ShoppingListItem.objects.get(
            shopping_list=self.shopping_list,
            ingredient__name__iexact='Oignons',
        )
        manual_qty = ShoppingListItemQuantity.objects.get(
            shopping_list_item=item,
            recipe_batch=None,
        )
        self.assertEqual(float(manual_qty.quantity), 5.0)

    def test_get_shopping_list_items_excludes_fully_checked_stale(self):
        from django.utils import timezone
        from datetime import timedelta
        from recipes.models import Recipe, RecipeBatch

        recipe = Recipe.objects.create(
            title='Salade',
            prep_time=5,
            cook_time=0,
            created_by=self.user,
        )
        batch = RecipeBatch.objects.create(recipe=recipe, created_by=self.user)
        item = ShoppingListItem.objects.create(
            shopping_list=self.shopping_list,
            ingredient=self.ingredient,
            unit_group='volume',
            pantry_unit='l',
            checked_at=timezone.now() - timedelta(hours=30),
        )
        ShoppingListItemQuantity.objects.create(
            shopping_list_item=item,
            recipe_batch=batch,
            quantity=Decimal('2'),
            checked_quantity=Decimal('2'),
            unit='l',
            checked_at=timezone.now() - timedelta(hours=30),
        )

        result = get_shopping_list_items_for_user(self.user, self.shopping_list.id)
        self.assertEqual(result.count, 0)

    def test_add_shopping_list_item_invalid_unit(self):
        with self.assertRaises(ValueError):
            add_item_to_shopping_list(
                self.user,
                self.shopping_list.id,
                'Sel',
                unit='invalid',
            )
