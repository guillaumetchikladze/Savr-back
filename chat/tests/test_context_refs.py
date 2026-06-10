"""Tests injection contexte @ recette / repas / liste dans l'historique agent."""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from chat.models import Conversation, Message
from chat.services.context_refs import build_context_prompt_for_agent, format_context_refs_prompt
from chat.services.stream_adapter import build_agent_history
from recipes.models import (
    Ingredient,
    Recipe,
    RecipeBatch,
    RecipeIngredient,
    ShoppingList,
    ShoppingListItem,
    ShoppingListItemQuantity,
    ShoppingListMember,
)

User = get_user_model()


class ContextRefsPromptTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='chef', email='chef@test.com', password='pass'
        )
        self.recipe = Recipe.objects.create(
            title='Salade composée',
            description='Une salade fraîche et colorée.',
            steps_summary='Couper les légumes, assaisonner, servir.',
            prep_time=15,
            cook_time=0,
            difficulty='easy',
            servings=2,
            created_by=self.user,
        )
        tomato = Ingredient.objects.create(name='Tomate')
        RecipeIngredient.objects.create(
            recipe=self.recipe,
            ingredient=tomato,
            quantity=3,
            unit='piece',
        )

    def test_format_context_refs_includes_meta(self):
        prompt = format_context_refs_prompt([{
            'type': 'recipe',
            'id': self.recipe.id,
            'label': self.recipe.title,
            'meta': {'view_mode_label': 'Fiche recette'},
        }])
        self.assertIn('[Contexte utilisateur attaché]', prompt)
        self.assertIn('Salade composée', prompt)
        self.assertIn('Fiche recette', prompt)

    def test_build_context_prompt_resolves_recipe_from_db(self):
        prompt = build_context_prompt_for_agent(self.user, [{
            'type': 'recipe',
            'id': self.recipe.id,
            'label': self.recipe.title,
        }])
        self.assertIn('[Contenu résolu depuis Tchikook Agent]', prompt)
        self.assertIn('Tomate', prompt)
        self.assertIn('Résumé des étapes', prompt)

    def test_build_agent_history_prefixes_user_message_with_context(self):
        conv = Conversation.objects.create(user=self.user, title='Test')
        Message.objects.create(
            conversation=conv,
            role=Message.ROLE_USER,
            message_type=Message.TYPE_TEXT,
            content='Comment adapter les portions ?',
            ui_payload={
                'context_refs': [{
                    'type': 'recipe',
                    'id': self.recipe.id,
                    'label': self.recipe.title,
                }],
            },
        )
        msgs = list(conv.messages.order_by('created_at'))
        history = build_agent_history(msgs, self.user)

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['role'], 'user')
        self.assertIn('[Contexte utilisateur attaché]', history[0]['content'])
        self.assertIn('Tomate', history[0]['content'])
        self.assertIn('Comment adapter les portions ?', history[0]['content'])

    def test_build_agent_history_single_system_message_with_events(self):
        conv = Conversation.objects.create(user=self.user, title='Test')
        Message.objects.create(
            conversation=conv,
            role=Message.ROLE_SYSTEM,
            message_type=Message.TYPE_SYSTEM_EVENT,
            content='[Événement Tchikook Agent] Import terminé.',
            ui_payload={'hidden': True},
        )
        history = build_agent_history(list(conv.messages.order_by('created_at')), self.user)
        system_msgs = [h for h in history if h['role'] == 'system']
        self.assertEqual(len(system_msgs), 0)
        self.assertEqual(history[-1]['role'], 'user')
        self.assertIn('Événement Tchikook Agent', history[-1]['content'])

    def test_shopping_list_context_excludes_fully_checked_stale_items(self):
        shopping_list = ShoppingList.objects.create(name='ABC')
        ShoppingListMember.objects.create(
            shopping_list=shopping_list,
            user=self.user,
            role='owner',
        )
        ingredient = Ingredient.objects.create(name='Noix')
        batch = RecipeBatch.objects.create(recipe=self.recipe, created_by=self.user)
        item = ShoppingListItem.objects.create(
            shopping_list=shopping_list,
            ingredient=ingredient,
            unit_group='weight',
            pantry_unit='g',
            checked_at=timezone.now() - timedelta(hours=30),
        )
        ShoppingListItemQuantity.objects.create(
            shopping_list_item=item,
            recipe_batch=batch,
            quantity=Decimal('30'),
            checked_quantity=Decimal('30'),
            unit='g',
            checked_at=timezone.now() - timedelta(hours=30),
        )

        prompt = build_context_prompt_for_agent(self.user, [{
            'type': 'shopping_list',
            'id': shopping_list.id,
            'label': 'ABC',
        }])
        self.assertIn('tout est déjà acheté', prompt)
        self.assertNotIn('Noix', prompt)
