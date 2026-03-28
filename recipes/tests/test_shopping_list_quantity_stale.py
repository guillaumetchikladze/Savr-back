"""Tests pour le masquage 24h des sous-lignes ShoppingListItemQuantity (with_quantities)."""
from datetime import timedelta
from types import SimpleNamespace

from django.test import TestCase
from django.utils import timezone

from recipes.utils import shopping_list_item_quantity_is_stale


class ShoppingListItemQuantityStaleTests(TestCase):
    def test_not_stale_when_remaining_to_buy(self):
        now = timezone.now()
        q = SimpleNamespace(quantity=2, checked_quantity=1, checked_at=now - timedelta(hours=30))
        self.assertFalse(shopping_list_item_quantity_is_stale(q, now, timedelta(days=1)))

    def test_not_stale_when_fully_checked_but_no_checked_at(self):
        now = timezone.now()
        q = SimpleNamespace(quantity=1, checked_quantity=1, checked_at=None)
        self.assertFalse(shopping_list_item_quantity_is_stale(q, now, timedelta(days=1)))

    def test_stale_when_fully_checked_long_ago(self):
        now = timezone.now()
        q = SimpleNamespace(
            quantity=1,
            checked_quantity=1,
            checked_at=now - timedelta(hours=25),
        )
        self.assertTrue(shopping_list_item_quantity_is_stale(q, now, timedelta(days=1)))

    def test_not_stale_when_fully_checked_within_window(self):
        now = timezone.now()
        q = SimpleNamespace(
            quantity=1,
            checked_quantity=1,
            checked_at=now - timedelta(hours=12),
        )
        self.assertFalse(shopping_list_item_quantity_is_stale(q, now, timedelta(days=1)))
