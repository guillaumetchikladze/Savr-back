import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from recipes.dietary_filters import strict_excluded_recipe_ids_for_user
from recipes.dietary_matching import name_matches
from recipes.models import Ingredient, Recipe, RecipeIngredient


class DietaryStrictMatchingTestCase(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            validated_at=timezone.now(),
            username='dietary_tester',
            email='dietary_tester@example.com',
            password='password123',
        )

    def _recipe_with_ingredient(self, title, ingredient_name):
        ing, _ = Ingredient.objects.get_or_create(name=ingredient_name)
        r = Recipe.objects.create(
            title=title,
            description='',
            steps_summary='',
            prep_time=1,
            cook_time=1,
            created_by=self.user,
            meal_type='lunch',
            difficulty='easy',
            servings=2,
            source_type='user_created',
            is_public=True,
        )
        RecipeIngredient.objects.create(recipe=r, ingredient=ing, quantity='1', unit='')
        return r

    def test_strict_word_boundary_avoids_lait_matching_laitue(self):
        """
        Régression crédibilité: si l'utilisateur déclare 'lait', on ne doit pas exclure une recette
        contenant 'laitue' en mode strict.
        """
        r_laitue = self._recipe_with_ingredient('Salade', 'laitue')
        r_lait = self._recipe_with_ingredient('Verre de lait', 'lait')

        self.user.allergies = ['lait']
        self.user.save(update_fields=['allergies'])

        excluded = set(strict_excluded_recipe_ids_for_user(self.user))
        self.assertNotIn(r_laitue.id, excluded)
        self.assertIn(r_lait.id, excluded)

    def test_strict_matches_oeuf_and_oeufs_variants(self):
        r_sing = self._recipe_with_ingredient('Oeuf dur', 'oeuf')
        r_plur = self._recipe_with_ingredient('Oeufs brouillés', 'oeufs')

        self.user.allergies = ['Œufs']
        self.user.save(update_fields=['allergies'])

        excluded = set(strict_excluded_recipe_ids_for_user(self.user))
        self.assertIn(r_sing.id, excluded)
        self.assertIn(r_plur.id, excluded)

    def test_strict_exclusion_handles_accents_and_curated_exceptions(self):
        r_creme = self._recipe_with_ingredient('Gratin', 'Crème Fraîche')
        r_coco = self._recipe_with_ingredient('Curry', 'Lait de coco')
        r_laitue = self._recipe_with_ingredient('Salade', 'Laitues')

        self.user.allergies = ['Lactose / lait']
        self.user.save(update_fields=['allergies'])

        excluded = set(strict_excluded_recipe_ids_for_user(self.user))
        self.assertEqual(excluded & {r_creme.id, r_coco.id, r_laitue.id}, {r_creme.id})


@pytest.mark.parametrize(
    'keyword, name, expected',
    [
        ('olive', 'Olives noires', True),
        ('Olives', 'olive verte', True),
        ('oeuf', 'Œufs', True),
        ("blanc d'oeuf", 'Blanc d’œufs', True),
        ('crème', 'creme fraiche', True),
        ('chou', 'choux de Bruxelles', True),
        ('lait', 'laitue', False),
        ('ris', 'riz', False),
        ('bar', 'barbecue', False),
        ('noix de saint-jacques', 'Noix de Saint Jacques', True),
    ],
)
def test_word_boundary_matching(keyword, name, expected):
    assert name_matches(name, keyword) is expected
