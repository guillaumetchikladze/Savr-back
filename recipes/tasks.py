import logging
import asyncio
import nest_asyncio

# Permettre les boucles asyncio imbriquées (nécessaire pour Celery)
nest_asyncio.apply()

from celery import shared_task

from .models import RecipeImportRequest
from .services.ai_service import formalize_recipe
from .services.formalization_pipeline import create_recipe_from_formalized
from .services.recipe_importer import import_recipe_from_url, is_ingredients_suspicious, InstagramImportError
from .services.image_uploader import download_and_upload_image

logger = logging.getLogger(__name__)

def _update_import_progress(import_request: RecipeImportRequest, step: str, percent: int, *, used_source: str | None = None):
    payload = import_request.payload or {}
    payload['import_progress'] = {
        'step': step,
        'percent': max(0, min(int(percent), 100)),
    }
    if used_source:
        payload['import_extractor'] = used_source
    import_request.payload = payload
    import_request.save(update_fields=['payload', 'updated_at'])


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
    _update_import_progress(import_request, step='EXTRACTING', percent=10)

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
        try:
            raw_recipe_data, used_source = import_recipe_from_url(url)
        except InstagramImportError as ie:
            used_source = 'instagram'
            logger.warning(
                "[RecipeImportURLTask] Instagram import failed early for url=%s (code=%s, message=%s)",
                url,
                ie.code,
                ie.message,
            )
            if ie.code in {'apify_not_configured', 'apify_failed', 'post_not_found'}:
                error_msg = (
                    "Nous n’arrivons pas à accéder à ce post Instagram pour le moment. "
                    "Le profil est peut-être privé, le contenu n’est plus disponible "
                    "ou notre service d’import est temporairement indisponible."
                )
            elif ie.code in {'no_recipe', 'no_recipe_text'}:
                error_msg = (
                    "Ce post Instagram ne semble pas contenir de recette détaillée "
                    "(ingrédients et étapes) exploitable automatiquement. "
                    "Essayez avec un autre lien."
                )
            elif ie.code == 'no_ingredients':
                error_msg = (
                    "Nous n’avons pas réussi à identifier une liste d’ingrédients suffisamment claire "
                    "à partir de ce post Instagram. Les informations sont trop vagues."
                )
            else:
                error_msg = (
                    "Une erreur est survenue lors de l’import depuis Instagram. "
                    "Réessayez plus tard ou avec un autre lien."
                )

            import_request.status = RecipeImportRequest.STATUS_ERROR
            import_request.error_message = error_msg
            import_request.save(update_fields=['status', 'error_message', 'updated_at'])
            _update_import_progress(import_request, step='ERROR', percent=100, used_source=used_source)
            return

        _update_import_progress(import_request, step='EXTRACTED', percent=25, used_source=used_source)
        
        logger.info(
            "[RecipeImportURLTask] Extraction result - source=%s, has_data=%s, title=%s",
            used_source,
            bool(raw_recipe_data),
            raw_recipe_data.get('title', 'N/A') if raw_recipe_data else 'N/A'
        )
        
        if not raw_recipe_data:
            if used_source == 'instagram':
                error_msg = (
                    "Nous n’avons pas pu extraire de recette exploitable depuis ce lien Instagram. "
                    "Le contenu ne semble pas contenir suffisamment d’informations pour reconstruire une recette."
                )
            else:
                error_msg = (
                    f"Impossible d'extraire la recette depuis cette URL (source: {used_source or 'unknown'}). "
                    "Vérifiez que l'URL est valide et accessible."
                )

            logger.warning("[RecipeImportURLTask] Extraction failed: %s", error_msg)
            import_request.status = RecipeImportRequest.STATUS_ERROR
            import_request.error_message = error_msg
            import_request.save(update_fields=['status', 'error_message', 'updated_at'])
            _update_import_progress(import_request, step='ERROR', percent=100, used_source=used_source)
            return

        # Étape 2 : Vérifier la complétude minimale des ingrédients (strict_block)
        suspicious, reason = is_ingredients_suspicious(raw_recipe_data.get('ingredients_text', ''))
        if suspicious:
            if used_source == 'instagram':
                error_msg = (
                    "Nous n’avons pas pu reconstruire une liste d’ingrédients suffisamment complète "
                    "à partir de ce post Instagram. Les quantités ou les ingrédients sont trop vagues."
                )
            else:
                error_msg = (
                    "Import bloqué: impossible de garantir la complétude des ingrédients. "
                    f"Raison: {reason}"
                )
            logger.warning(
                "[RecipeImportURLTask] Ingredient completeness check failed (source=%s): %s",
                used_source,
                error_msg,
            )
            import_request.status = RecipeImportRequest.STATUS_ERROR
            import_request.error_message = error_msg
            import_request.save(update_fields=['status', 'error_message', 'updated_at'])
            _update_import_progress(import_request, step='ERROR', percent=100, used_source=used_source)
            return

        # Étape 3 : Les données d'import sont déjà structurées, pas besoin de prétraitement
        # On met juste à jour le payload avec les données extraites
        logger.info("[RecipeImportURLTask] Step 3/5: Using extracted data directly (no preprocessing needed): %s", raw_recipe_data.get('title', ''))
        
        # Sauvegarder l'URL externe de l'image temporairement
        external_image_url = raw_recipe_data.get('image_path', '')
        
        payload.update({
            **raw_recipe_data,
            'import_source_url': url,
            'source_type': used_source or payload.get('source_type') or 'generic',
        })
        import_request.payload = payload
        import_request.save(update_fields=['payload'])
        _update_import_progress(import_request, step='READY_FOR_AI', percent=35, used_source=used_source)

        # Étape 4 : Formaliser avec l'IA (données déjà structurées)
        logger.info("[RecipeImportURLTask] Step 4/5: Formalizing recipe with AI: %s", raw_recipe_data.get('title', ''))
        _update_import_progress(import_request, step='FORMALIZING', percent=55, used_source=used_source)
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

        # Étape 5 : Créer la recette en DB
        logger.info("[RecipeImportURLTask] Step 5/5: Creating recipe in database")
        _update_import_progress(import_request, step='SAVING', percent=80, used_source=used_source)
        recipe = create_recipe_from_formalized(formalized_recipe, payload, import_request.user)
        
        # Étape 6 : Télécharger et uploader l'image vers S3 si elle existe
        if external_image_url and external_image_url.startswith('http'):
            logger.info("[RecipeImportURLTask] Step 6/6: Downloading and uploading image to S3: %s", external_image_url)
            _update_import_progress(import_request, step='UPLOADING_IMAGE', percent=90, used_source=used_source)
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
            logger.info("[RecipeImportURLTask] Step 6/6: No external image URL to download")

        import_request.status = RecipeImportRequest.STATUS_SUCCESS
        import_request.recipe = recipe
        import_request.save(update_fields=['status', 'recipe', 'updated_at'])
        _update_import_progress(import_request, step='DONE', percent=100, used_source=used_source)
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
        _update_import_progress(import_request, step='ERROR', percent=100)
        raise


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def reindex_recipe_search(self, recipe_id: int, force: bool = False):
    """Indexation async Gemini + embedding (zero-friction : conserve l'ancien vecteur jusqu'au succès)."""
    from .services.recipe_search_index import index_recipe

    try:
        ok = index_recipe(recipe_id, force=force)
        if not ok:
            raise RuntimeError(f'index_recipe failed for recipe {recipe_id}')
    except Exception as exc:
        logger.exception('[ReindexRecipeSearch] recipe %s: %s', recipe_id, exc)
        raise self.retry(exc=exc)

