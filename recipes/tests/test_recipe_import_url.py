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
from recipes.utils import canonicalize_import_url
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

    def _create_imported_recipe(self, url: str, is_public: bool = True, source_type: str = "imported"):
        """
        Helper pour créer une recette importée avec import_source_url canonique.
        """
        canonical = canonicalize_import_url(url)
        return Recipe.objects.create(
            title="Déjà importée",
            description="",
            steps_summary="",
            meal_type="lunch",
            difficulty="easy",
            prep_time=0,
            cook_time=0,
            servings=1,
            created_by=self.user,
            source_type=source_type,
            is_public=is_public,
            import_source_url=canonical,
        )

    def test_is_ingredients_suspicious_flags_empty(self):
        suspicious, reason = is_ingredients_suspicious("")
        self.assertTrue(suspicious)
        self.assertIn("Aucun ingrédient", reason)

    def test_extract_instagram_recipe_without_token_raises(self):
        # On ne teste ici que le comportement de garde-fou sur l'absence de token,
        # sans appeler réellement Apify.
        with patch("recipes.services.recipe_importer.config") as config_mock:
            config_mock.return_value = ""
            with self.assertRaises(InstagramImportError) as ctx:
                extract_instagram_recipe("https://www.instagram.com/p/DTQdIJoDJRI/")
        self.assertEqual(ctx.exception.code, "apify_not_configured")

    @patch("recipes.services.recipe_importer.ApifyClient")
    @patch("recipes.services.recipe_importer.config")
    @patch("recipes.services.ai_service.parse_instagram_caption")
    def test_extract_instagram_recipe_uses_apify_v3_run_model(
        self, parse_caption_mock, config_mock, apify_client_cls
    ):
        """apify-client v3 returns a Pydantic Run with default_dataset_id, not a dict."""
        from types import SimpleNamespace

        config_mock.return_value = "fake-token"
        parse_caption_mock.return_value = {
            "is_recipe": True,
            "title": "Tarte aux pommes",
            "ingredients_text": "- 3 pommes\n- 1 pâte",
            "instructions_text": "1. Préparer.\n2. Cuire.",
            "reason": "",
        }

        run = SimpleNamespace(default_dataset_id="dataset-123")
        dataset_client = SimpleNamespace(
            iterate_items=lambda: iter(
                [
                    {
                        "caption": "Recette tarte aux pommes\nIngrédients: pommes",
                        "firstComment": "",
                    }
                ]
            )
        )
        actor_client = SimpleNamespace(call=lambda **kwargs: run)
        client = SimpleNamespace(
            actor=lambda *_args, **_kwargs: actor_client,
            dataset=lambda *_args, **_kwargs: dataset_client,
        )
        apify_client_cls.return_value = client

        result = extract_instagram_recipe("https://www.instagram.com/reel/C3xKGnUI4zj/")
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "Tarte aux pommes")
        self.assertIn("pommes", result["ingredients_text"])

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

    def test_canonicalize_import_url_normalizes_basic_parts(self):
        url = "HTTPS://Example.com/Recette/Test/?b=2&a=1&utm_source=newsletter#section"
        canonical = canonicalize_import_url(url)
        # Schéma et host en minuscules, fragment supprimé, utm_* supprimé, query triée
        self.assertEqual(
            canonical,
            "https://example.com/Recette/Test?a=1&b=2",
        )

    def test_canonicalize_import_url_keeps_business_params(self):
        url = "https://example.com/recipe?id=123&recipeId=456&utm_medium=email"
        canonical = canonicalize_import_url(url)
        # Les paramètres métier doivent rester présents
        self.assertEqual(
            canonical,
            "https://example.com/recipe?id=123&recipeId=456",
        )

    def test_canonicalize_import_url_strips_tracking_ids(self):
        url = "https://example.com/recipe?gclid=test123&fbclid=abc&utm_campaign=x"
        canonical = canonicalize_import_url(url)
        # Tous les paramètres de tracking doivent être supprimés
        self.assertEqual(
            canonical,
            "https://example.com/recipe",
        )

    def test_canonicalize_instagram_reel_and_post_are_equal(self):
        url_reel = "https://www.instagram.com/reel/DUJNIgZjKQr/?utm_source=ig_web_copy_link&igsh=NTc4MTIwNjQ2YQ=="
        url_post = "https://www.instagram.com/p/DUJNIgZjKQr/"

        canonical_reel = canonicalize_import_url(url_reel)
        canonical_post = canonicalize_import_url(url_post)

        self.assertEqual(canonical_reel, canonical_post)

    def test_import_from_url_sets_canonical_import_source_url(self):
        url = "https://example.com/recipe?id=123&utm_source=news"

        with patch("recipes.services.recipe_importer.extract_with_recipe_scrapers") as scrapers_mock:
            scrapers_mock.return_value = dict(SAMPLE_RECIPE)
            data, used = import_recipe_from_url(url)

        self.assertIsNotNone(data)
        self.assertEqual(used, "recipe-scrapers")
        # import_source_url doit être la version canonique
        self.assertEqual(
            data.get("import_source_url"),
            canonicalize_import_url(url),
        )

    def test_import_from_url_view_returns_existing_recipe_when_already_imported(self):
        """
        Si une recette importée existe déjà pour cette URL (normalisée) et est accessible,
        la vue doit renvoyer directement cette recette sans créer de RecipeImportRequest.
        """
        from recipes.models import RecipeImportRequest

        url = "https://example.com/recipe?id=123&utm_source=news"
        recipe = self._create_imported_recipe(url, is_public=True)

        response = self.client.post(
            "/api/recipes/import_from_url/",
            {"url": url},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("already_imported"))
        self.assertEqual(data.get("recipe_id"), recipe.id)
        self.assertIn("recipe", data)

        # Vérifier qu'aucune nouvelle demande d'import n'a été créée
        self.assertEqual(RecipeImportRequest.objects.count(), 0)

    def test_import_from_url_view_ignores_tracking_params_for_deduplication(self):
        """
        Deux URLs qui ne diffèrent que par des paramètres de tracking
        doivent pointer vers la même recette importée.
        """
        url_original = "https://example.com/recipe?id=123"
        url_with_tracking = "https://example.com/recipe?id=123&utm_source=newsletter&utm_medium=email"

        recipe = self._create_imported_recipe(url_original, is_public=True)

        response = self.client.post(
            "/api/recipes/import_from_url/",
            {"url": url_with_tracking},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("already_imported"))
        self.assertEqual(data.get("recipe_id"), recipe.id)

    def test_import_from_url_view_deduplicates_even_for_instagram_source_type(self):
        """
        La déduplication ne doit pas dépendre de source_type ('instagram', 'marmiton', etc.).
        """
        from recipes.models import RecipeImportRequest

        url = "https://www.instagram.com/p/DUJNIgZjKQr/"
        recipe = self._create_imported_recipe(url, is_public=True, source_type="instagram")

        response = self.client.post(
            "/api/recipes/import_from_url/",
            {"url": url},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data.get("already_imported"))
        self.assertEqual(data.get("recipe_id"), recipe.id)
        self.assertEqual(RecipeImportRequest.objects.count(), 0)

