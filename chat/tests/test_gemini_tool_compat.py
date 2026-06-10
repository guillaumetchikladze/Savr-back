"""Tests réparation JSON tool Gemini."""

import json

from django.test import SimpleTestCase

from chat.services.gemini_tool_compat import repair_tool_arguments_json


class GeminiToolCompatTests(SimpleTestCase):
    def test_empty_becomes_object(self):
        self.assertEqual(repair_tool_arguments_json(''), '{}')
        self.assertEqual(repair_tool_arguments_json('   '), '{}')

    def test_valid_json_unchanged(self):
        raw = '{"query": "curry", "limit": 10}'
        self.assertEqual(repair_tool_arguments_json(raw), raw)

    def test_python_dict_literal(self):
        repaired = repair_tool_arguments_json("{'query': 'curry', 'limit': 10}")
        self.assertEqual(repaired, '{"query": "curry", "limit": 10}')

    def test_unquoted_keys(self):
        repaired = repair_tool_arguments_json('{query: "curry", limit: 10}')
        self.assertEqual(json.loads(repaired), {'query': 'curry', 'limit': 10})

    def test_concatenated_json_objects(self):
        raw = '{"query":"curry"}{"query":"gratin"}{"query":"pâtes"}'
        repaired = repair_tool_arguments_json(raw)
        self.assertEqual(json.loads(repaired), {'query': 'curry'})
