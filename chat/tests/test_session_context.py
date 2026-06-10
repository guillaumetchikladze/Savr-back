"""Tests contexte session agent."""

from datetime import date, datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from chat.services.session_context import build_session_context_prompt

User = get_user_model()


class SessionContextTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='planner', email='planner@test.com', password='pass'
        )

    def test_includes_today_and_week_bounds(self):
        fixed_now = datetime(2026, 6, 10, 15, 30)  # mercredi
        with patch('chat.services.session_context.timezone.localdate', return_value=date(2026, 6, 10)):
            with patch('chat.services.session_context.timezone.localtime', return_value=fixed_now):
                prompt = build_session_context_prompt(self.user)

        self.assertIn('2026-06-10', prompt)
        self.assertIn('2026-06-08', prompt)  # lundi de la semaine
        self.assertIn('2026-06-14', prompt)  # dimanche
        self.assertIn('planner', prompt)
        self.assertIn('get_meal_plans', prompt)
