from unittest.mock import patch

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from recipes.models import Recipe, RecipeImportRequest
from recipes.services.recipe_importer import (
    import_recipe_from_url,
    is_ingredients_suspicious,
    extract_instagram_recipe,
    InstagramImportError,
)
from recipes.tasks import process_recipe_import_from_url


SAMPLE_RECIPE = {
    "title": "Pâtes au beurre",
    "description": "Simple et efficace",
    "ingredients_text": "- 200 g de pâtes\n- 30 g de beurre\n- sel",
    "instructions_text": "1. Cuire les pâtes.\n2. Ajouter le beurre.\n3. Saler.",
    "prep_time": 5,
    "cook_time": 10,
    "servings": 2,
    "image_path": "https://example.com/img.jpg",
}


class RecipeImportFromUrlTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="import_tester",
            email="import_tester@example.com",
            password="password123",
        )
        self.client.force_authenticate(self.user)

    def test_is_ingredients_suspicious_flags_empty(self):
        suspicious, reason = is_ingredients_suspicious("")
        self.assertTrue(suspicious)
        self.assertIn("Aucun ingrédient", reason)

    def test_extract_instagram_recipe_without_token_raises(self):
        # On ne teste ici que le comportement de garde-fou sur l'absence de token,
        # sans appeler réellement Apify.
        with self.assertRaises(InstagramImportError) as ctx:
            extract_instagram_recipe("https://www.instagram.com/p/DTQdIJoDJRI/")
        self.assertEqual(ctx.exception.code, "apify_not_configured")

    @patch("recipes.services.recipe_importer.extract_with_recipe_scrapers")
    @patch("recipes.services.recipe_importer.extract_marmiton_recipe")
    def test_import_recipe_from_url_uses_recipe_scrapers_first(self, marmiton_legacy, recipe_scrapers):
        url = "https://marmiton.org/recettes/recette_test_123.aspx"
        recipe_scrapers.return_value = dict(SAMPLE_RECIPE)
        marmiton_legacy.side_effect = AssertionError("Legacy extractor should not be called when recipe-scrapers succeeds")

        data, used = import_recipe_from_url(url)
        self.assertIsNotNone(data)
        self.assertEqual(used, "recipe-scrapers")
        recipe_scrapers.assert_called_once()

    @patch("recipes.services.recipe_importer.extract_with_recipe_scrapers")
    @patch("recipes.services.recipe_importer.extract_marmiton_recipe")
    def test_import_recipe_from_url_falls_back_when_recipe_scrapers_partial(self, marmiton_legacy, recipe_scrapers):
        url = "https://marmiton.org/recettes/recette_test_123.aspx"
        partial = dict(SAMPLE_RECIPE)
        partial["instructions_text"] = ""
        recipe_scrapers.return_value = partial
        marmiton_legacy.return_value = dict(SAMPLE_RECIPE)

        data, used = import_recipe_from_url(url)
        self.assertIsNotNone(data)
        self.assertEqual(used, "marmiton")
        marmiton_legacy.assert_called_once()

    @patch("recipes.tasks.import_recipe_from_url")
    @patch("recipes.tasks.formalize_recipe")
    def test_task_blocks_when_ingredients_suspicious(self, formalize_recipe_mock, importer_mock):
        importer_mock.return_value = (
            {
                "title": "Recette suspecte",
                "description": "",
                "ingredients_text": "",
                "instructions_text": "1. Faire quelque chose",
            },
            "recipe-scrapers",
        )

        req = RecipeImportRequest.objects.create(
            user=self.user,
            payload={"url": "https://marmiton.org/recettes/recette_test_123.aspx", "source_type": "imported"},
            status=RecipeImportRequest.STATUS_PENDING,
        )

        process_recipe_import_from_url.run(str(req.id))

        req.refresh_from_db()
        self.assertEqual(req.status, RecipeImportRequest.STATUS_ERROR)
        self.assertIn("Import bloqué", req.error_message)
        self.assertEqual(req.payload.get("import_progress", {}).get("step"), "ERROR")
        formalize_recipe_mock.assert_not_called()

    @patch("recipes.tasks.import_recipe_from_url")
    @patch("recipes.tasks.download_and_upload_image")
    @patch("recipes.tasks.create_recipe_from_formalized")
    @patch("recipes.tasks.formalize_recipe")
    def test_task_success_sets_progress_and_extractor(
        self,
        formalize_recipe_mock,
        create_recipe_mock,
        download_image_mock,
        importer_mock,
    ):
        importer_mock.return_value = (dict(SAMPLE_RECIPE), "recipe-scrapers")
        formalize_recipe_mock.return_value = None
        download_image_mock.return_value = None

        def _create_recipe(*args, **kwargs):
            return Recipe.objects.create(
                title="Pâtes au beurre",
                description="",
                steps_summary="",
                meal_type="lunch",
                difficulty="easy",
                prep_time=0,
                cook_time=0,
                servings=1,
                created_by=self.user,
                source_type="imported",
            )

        create_recipe_mock.side_effect = _create_recipe

        req = RecipeImportRequest.objects.create(
            user=self.user,
            payload={"url": "https://example.com/recette", "source_type": "imported"},
            status=RecipeImportRequest.STATUS_PENDING,
        )

        process_recipe_import_from_url.run(str(req.id))

        req.refresh_from_db()
        self.assertEqual(req.status, RecipeImportRequest.STATUS_SUCCESS)
        self.assertEqual(req.payload.get("import_extractor"), "recipe-scrapers")
        self.assertEqual(req.payload.get("import_progress", {}).get("step"), "DONE")
        self.assertEqual(req.payload.get("import_progress", {}).get("percent"), 100)

