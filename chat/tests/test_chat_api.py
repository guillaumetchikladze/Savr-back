"""Tests API REST chat."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from chat.models import Conversation, Message, PendingAction
from chat.services.rate_limit import default_action_expiry

User = get_user_model()


class ChatAPITests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='chatuser', email='chatuser@test.com', password='pass'
        )
        self.other = User.objects.create_user(
            username='other', email='other@test.com', password='pass'
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_create_and_list_conversations(self):
        resp = self.client.post('/api/chat/conversations/', {})
        self.assertEqual(resp.status_code, 201)
        conv_id = resp.data['id']

        resp = self.client.get('/api/chat/conversations/')
        self.assertEqual(resp.status_code, 200)
        items = resp.data.get('results', resp.data)
        ids = [c['id'] for c in items]
        self.assertIn(conv_id, ids)

    def test_messages_include_mutation_pending_action(self):
        conv = Conversation.objects.create(user=self.user, title='Test')
        msg = Message.objects.create(
            conversation=conv,
            role=Message.ROLE_ASSISTANT,
            message_type=Message.TYPE_MUTATION_PROPOSAL,
            content='Retirer Carbonara',
            ui_payload={'card_type': 'meal_deletion'},
        )
        PendingAction.objects.create(
            conversation=conv,
            message=msg,
            action_type=PendingAction.ACTION_MEAL_DELETION,
            payload={'meal_plan_id': 1},
            expires_at=default_action_expiry(),
        )

        resp = self.client.get(f'/api/chat/conversations/{conv.id}/messages/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['results'][0]['pending_action']['action_type'], 'meal_deletion')

    def test_isolation_user(self):
        conv = Conversation.objects.create(user=self.other)
        resp = self.client.get(f'/api/chat/conversations/{conv.id}/messages/')
        self.assertEqual(resp.status_code, 404)
