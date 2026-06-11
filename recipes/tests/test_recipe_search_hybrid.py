"""Tests recherche hybride search_semantic."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from recipes.models import Recipe
from recipes.services.recipe_search import fuzzy_recipe_queryset, hybrid_recipe_queryset

User = get_user_model()

QUERY_VECTOR = [0.1] * 512


@override_settings(
    SEARCH_SEMANTIC_MAX_DISTANCE=0.45,
    SEARCH_HYBRID_WEIGHT_SEMANTIC=0.65,
    SEARCH_HYBRID_WEIGHT_TRIGRAM=0.35,
    SEARCH_TRIGRAM_MIN_SCORE=0.05,
    SEARCH_HYBRID_MIN_SCORE=0.05,
)
class RecipeSearchHybridTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='owner', password='pass')
        self.other = User.objects.create_user(username='other', password='pass')
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)

        self.public_recipe = Recipe.objects.create(
            title='Tartiflette savoyarde',
            description='Gratin pommes de terre reblochon',
            prep_time=20,
            cook_time=40,
            created_by=self.owner,
            is_public=True,
            search_index_status=Recipe.SearchIndexStatus.PENDING,
        )
        self.private_recipe = Recipe.objects.create(
            title='Tartiflette secrète',
            description='Privée',
            prep_time=10,
            cook_time=20,
            created_by=self.other,
            is_public=False,
            search_index_status=Recipe.SearchIndexStatus.PENDING,
        )

    def test_hybrid_queryset_annotates_scores(self):
        qs = hybrid_recipe_queryset(
            Recipe.objects.filter(is_public=True),
            'tartiflette',
            QUERY_VECTOR,
        )
        row = qs.filter(pk=self.public_recipe.pk).first()
        self.assertIsNotNone(row)
        self.assertTrue(hasattr(row, 'hybrid_score'))
        self.assertTrue(hasattr(row, 'trgm_title'))

    def test_pending_recipe_found_by_title_trigram(self):
        qs = hybrid_recipe_queryset(
            Recipe.objects.filter(is_public=True),
            'tartiflette',
            QUERY_VECTOR,
        )
        ids = list(qs.values_list('id', flat=True))
        self.assertIn(self.public_recipe.id, ids)

    def test_search_fuzzy_finds_public_recipe_without_embedding(self):
        url = reverse('recipe-search-fuzzy')
        response = self.client.get(url, {'q': 'tartiflette'})
        self.assertEqual(response.status_code, 200)
        titles = [r['title'] for r in response.data.get('results', response.data)]
        self.assertTrue(any('Tartiflette savoyarde' in t for t in titles))
        self.assertFalse(any('secrète' in t for t in titles))

    def test_fuzzy_queryset_title_only(self):
        qs = fuzzy_recipe_queryset(
            Recipe.objects.filter(is_public=True),
            'tartiflette',
        )
        ids = list(qs.values_list('id', flat=True))
        self.assertIn(self.public_recipe.id, ids)
        row = qs.filter(pk=self.public_recipe.pk).first()
        self.assertTrue(hasattr(row, 'trgm_title'))
        self.assertFalse(hasattr(row, 'trgm_index_word'))

    def test_search_fuzzy_filter_only_without_query(self):
        url = reverse('recipe-search-fuzzy')
        response = self.client.get(url, {'difficulty': 'medium', 'mine': 'true'})
        self.assertEqual(response.status_code, 200)
        titles = [r['title'] for r in response.data.get('results', response.data)]
        self.assertIn('Tartiflette savoyarde', titles)

    def test_search_fuzzy_without_query_or_filters(self):
        url = reverse('recipe-search-fuzzy')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        titles = [r['title'] for r in response.data.get('results', response.data)]
        self.assertTrue(any('Tartiflette savoyarde' in t for t in titles))

    @patch('recipes.views.get_batch_embeddings')
    def test_search_semantic_excludes_private_recipes_of_others(self, mock_embed):
        mock_embed.return_value = [QUERY_VECTOR]
        url = reverse('recipe-search-semantic')
        response = self.client.get(url, {'q': 'tartiflette'})
        self.assertEqual(response.status_code, 200)
        titles = [r['title'] for r in response.data.get('results', response.data)]
        self.assertTrue(any('Tartiflette savoyarde' in t for t in titles))
        self.assertFalse(any('secrète' in t for t in titles))
