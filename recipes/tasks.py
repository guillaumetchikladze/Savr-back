import logging
import asyncio
import nest_asyncio

# Permettre les boucles asyncio imbriquées (nécessaire pour Celery)
nest_asyncio.apply()

from celery import shared_task

from .models import RecipeImportRequest
from .services.ai_service import formalize_recipe
from .services.formalization_pipeline import create_recipe_from_formalized
from .services.recipe_importer import import_recipe_from_url
from .services.image_uploader import download_and_upload_image

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def process_recipe_import(self, request_id: str):
    try:
        import_request = RecipeImportRequest.objects.select_related('user').get(id=request_id)
    except RecipeImportRequest.DoesNotExist:
        logger.error("[RecipeImportTask] Request %s not found", request_id)
        return

    if import_request.status not in [RecipeImportRequest.STATUS_PENDING, RecipeImportRequest.STATUS_PROCESSING]:
        logger.info("[RecipeImportTask] Request %s already processed (%s)", request_id, import_request.status)
        return

    logger.info("[RecipeImportTask] Processing request %s", request_id)
    import_request.status = RecipeImportRequest.STATUS_PROCESSING
    import_request.error_message = ''
    import_request.save(update_fields=['status', 'error_message', 'updated_at'])

    data = import_request.payload

    try:
        formalized_recipe = asyncio.run(
            formalize_recipe(
                data['title'],
                data.get('description', ''),
                data['ingredients_text'],
                data['instructions_text'],
                data.get('servings'),
                data.get('prep_time'),
                data.get('cook_time'),
            )
        )

        recipe = create_recipe_from_formalized(formalized_recipe, data, import_request.user)

        import_request.status = RecipeImportRequest.STATUS_SUCCESS
        import_request.recipe = recipe
        import_request.save(update_fields=['status', 'recipe', 'updated_at'])
        logger.info("[RecipeImportTask] Request %s completed", request_id)
    except Exception as exc:  # pragma: no cover
        logger.exception("[RecipeImportTask] Request %s failed: %s", request_id, exc)
        import_request.status = RecipeImportRequest.STATUS_ERROR
        import_request.error_message = str(exc)
        import_request.save(update_fields=['status', 'error_message', 'updated_at'])
        raise


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def process_recipe_import_from_url(self, request_id: str):
    """
    Tâche Celery pour importer une recette depuis une URL externe.
    Fait l'extraction, puis la formalisation IA, puis la création en DB.
    """
    try:
        import_request = RecipeImportRequest.objects.select_related('user').get(id=request_id)
    except RecipeImportRequest.DoesNotExist:
        logger.error("[RecipeImportURLTask] Request %s not found", request_id)
        return

    if import_request.status not in [RecipeImportRequest.STATUS_PENDING, RecipeImportRequest.STATUS_PROCESSING]:
        logger.info("[RecipeImportURLTask] Request %s already processed (%s)", request_id, import_request.status)
        return

    logger.info("[RecipeImportURLTask] Processing request %s", request_id)
    import_request.status = RecipeImportRequest.STATUS_PROCESSING
    import_request.error_message = ''
    import_request.save(update_fields=['status', 'error_message', 'updated_at'])

    payload = import_request.payload
    url = payload.get('url', '')

    if not url:
        import_request.status = RecipeImportRequest.STATUS_ERROR
        import_request.error_message = "URL manquante dans le payload"
        import_request.save(update_fields=['status', 'error_message', 'updated_at'])
        return

    try:
        # Étape 1 : Extraire la recette depuis l'URL
        logger.info("[RecipeImportURLTask] Step 1/5: Extracting recipe from URL: %s", url)
        raw_recipe_data, source_type = import_recipe_from_url(url)
        
        logger.info(
            "[RecipeImportURLTask] Extraction result - source_type=%s, has_data=%s, title=%s",
            source_type,
            bool(raw_recipe_data),
            raw_recipe_data.get('title', 'N/A') if raw_recipe_data else 'N/A'
        )
        
        if not raw_recipe_data:
            error_msg = f"Impossible d'extraire la recette depuis cette URL (source: {source_type or 'unknown'}). Vérifiez que l'URL est valide et accessible."
            logger.warning("[RecipeImportURLTask] Extraction failed: %s", error_msg)
            import_request.status = RecipeImportRequest.STATUS_ERROR
            import_request.error_message = error_msg
            import_request.save(update_fields=['status', 'error_message', 'updated_at'])
            return

        # Étape 2 : Les données d'import sont déjà structurées, pas besoin de prétraitement
        # On met juste à jour le payload avec les données extraites
        logger.info("[RecipeImportURLTask] Step 2/5: Using extracted data directly (no preprocessing needed): %s", raw_recipe_data.get('title', ''))
        
        # Sauvegarder l'URL externe de l'image temporairement
        external_image_url = raw_recipe_data.get('image_path', '')
        
        payload.update({
            **raw_recipe_data,
            'import_source_url': url,
        })
        import_request.payload = payload
        import_request.save(update_fields=['payload'])

        # Étape 3 : Formaliser avec l'IA (données déjà structurées)
        logger.info("[RecipeImportURLTask] Step 3/5: Formalizing recipe with AI: %s", raw_recipe_data.get('title', ''))
        formalized_recipe = asyncio.run(
            formalize_recipe(
                raw_recipe_data['title'],
                raw_recipe_data.get('description', ''),
                raw_recipe_data['ingredients_text'],
                raw_recipe_data['instructions_text'],
                raw_recipe_data.get('servings'),
                raw_recipe_data.get('prep_time'),
                raw_recipe_data.get('cook_time'),
            )
        )

        # Étape 4 : Créer la recette en DB
        logger.info("[RecipeImportURLTask] Step 4/5: Creating recipe in database")
        recipe = create_recipe_from_formalized(formalized_recipe, payload, import_request.user)
        
        # Étape 5 : Télécharger et uploader l'image vers S3 si elle existe
        if external_image_url and external_image_url.startswith('http'):
            logger.info("[RecipeImportURLTask] Step 5/5: Downloading and uploading image to S3: %s", external_image_url)
            s3_image_path = download_and_upload_image(
                external_image_url,
                import_request.user.id,
                recipe.id
            )
            if s3_image_path:
                recipe.image_path = s3_image_path
                recipe.save(update_fields=['image_path'])
                logger.info("[RecipeImportURLTask] Image successfully uploaded to S3: %s", s3_image_path)
            else:
                logger.warning("[RecipeImportURLTask] Failed to upload image, keeping original URL")
                # Si l'upload échoue, on garde l'URL originale
                recipe.image_path = external_image_url
                recipe.save(update_fields=['image_path'])
        else:
            logger.info("[RecipeImportURLTask] Step 5/5: No external image URL to download")

        import_request.status = RecipeImportRequest.STATUS_SUCCESS
        import_request.recipe = recipe
        import_request.save(update_fields=['status', 'recipe', 'updated_at'])
        logger.info(
            "[RecipeImportURLTask] Request %s completed successfully - recipe_id=%s, title='%s'",
            request_id,
            recipe.id,
            recipe.title
        )
        
    except Exception as exc:
        logger.exception("[RecipeImportURLTask] Request %s failed: %s", request_id, exc)
        import_request.status = RecipeImportRequest.STATUS_ERROR
        import_request.error_message = str(exc)
        import_request.save(update_fields=['status', 'error_message', 'updated_at'])
        raise

