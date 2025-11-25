"""
Service pour télécharger et uploader des images vers S3/MinIO
"""
import logging
import uuid
import requests
from io import BytesIO
from typing import Optional
from django.conf import settings

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30


def download_and_upload_image(image_url: str, user_id: int, recipe_id: Optional[int] = None) -> Optional[str]:
    """
    Télécharge une image depuis une URL externe et l'upload vers S3/MinIO.
    
    Args:
        image_url: URL de l'image externe à télécharger
        user_id: ID de l'utilisateur (pour le chemin)
        recipe_id: ID optionnel de la recette (pour le chemin)
    
    Returns:
        Chemin relatif de l'image dans S3 (ex: 'recipes/123/abc123.jpg') ou None en cas d'erreur
    """
    if not image_url:
        return None
    
    try:
        # Télécharger l'image depuis l'URL externe
        logger.info("[ImageUploader] Downloading image from: %s", image_url)
        response = requests.get(image_url, timeout=REQUEST_TIMEOUT, stream=True, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        response.raise_for_status()
        
        # Vérifier que c'est bien une image
        content_type = response.headers.get('Content-Type', '').lower()
        if not content_type.startswith('image/'):
            logger.warning("[ImageUploader] URL does not point to an image: %s", content_type)
            # On continue quand même, on détectera l'extension plus tard
        
        # Déterminer l'extension du fichier
        file_extension = 'jpg'  # Par défaut
        if content_type:
            if 'jpeg' in content_type or 'jpg' in content_type:
                file_extension = 'jpg'
            elif 'png' in content_type:
                file_extension = 'png'
            elif 'gif' in content_type:
                file_extension = 'gif'
            elif 'webp' in content_type:
                file_extension = 'webp'
        
        # Essayer aussi de détecter depuis l'URL
        if '.' in image_url:
            url_ext = image_url.split('.')[-1].lower().split('?')[0]  # Enlever les query params
            if url_ext in ['jpg', 'jpeg', 'png', 'gif', 'webp']:
                file_extension = url_ext if url_ext != 'jpeg' else 'jpg'
        
        # Lire le contenu de l'image
        image_data = BytesIO(response.content)
        image_size = len(response.content)
        logger.info("[ImageUploader] Downloaded image: %d bytes, extension: %s", image_size, file_extension)
        
        # Vérifier la taille (max 10MB)
        max_size = 10 * 1024 * 1024  # 10MB
        if image_size > max_size:
            logger.warning("[ImageUploader] Image too large: %d bytes (max: %d)", image_size, max_size)
            return None
        
        # Générer un nom de fichier unique
        unique_id = str(uuid.uuid4()).replace('-', '')
        if recipe_id:
            file_name = f"recipes/{user_id}/{recipe_id}/{unique_id}.{file_extension}"
        else:
            file_name = f"recipes/{user_id}/{unique_id}.{file_extension}"
        
        # Upload vers S3
        from savr_back.settings import build_s3_client
        
        s3_client = build_s3_client()
        bucket_name = settings.AWS_BUCKET
        
        if not bucket_name:
            logger.error("[ImageUploader] S3 bucket not configured")
            return None
        
        # Déterminer le content type
        content_type_map = {
            'jpg': 'image/jpeg',
            'jpeg': 'image/jpeg',
            'png': 'image/png',
            'gif': 'image/gif',
            'webp': 'image/webp',
        }
        upload_content_type = content_type_map.get(file_extension, 'image/jpeg')
        
        # Réinitialiser le pointeur du fichier
        image_data.seek(0)
        
        # Upload vers S3
        logger.info("[ImageUploader] Uploading to S3: %s", file_name)
        s3_client.upload_fileobj(
            image_data,
            bucket_name,
            file_name,
            ExtraArgs={
                'ACL': 'public-read',
                'ContentType': upload_content_type
            }
        )
        
        logger.info("[ImageUploader] Successfully uploaded image to S3: %s", file_name)
        return file_name
        
    except requests.exceptions.RequestException as e:
        logger.error("[ImageUploader] Error downloading image from %s: %s", image_url, e)
        return None
    except Exception as e:
        logger.error("[ImageUploader] Error uploading image to S3: %s", e, exc_info=True)
        return None


