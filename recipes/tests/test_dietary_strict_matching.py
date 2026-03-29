from django.contrib.auth import get_user_model
from django.test import TestCase

from recipes.dietary_filters import strict_excluded_recipe_ids_for_user
from recipes.models import Ingredient, Recipe, RecipeIngredient


class DietaryStrictMatchingTestCase(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
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

