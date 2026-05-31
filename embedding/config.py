"""
Configuration de l'API d'embedding
Centralise toutes les variables de configuration
"""

from decouple import config
import logging

logger = logging.getLogger(__name__)


class Config:
    """Configuration de l'API d'embedding"""

    MODEL_NAME = config('EMBEDDING_MODEL_NAME', default='nomic-ai/nomic-embed-text-v1.5')
    EMBEDDING_DIMENSION = config('EMBEDDING_DIMENSION', default=512, cast=int)
    HOST = config('HOST', default='0.0.0.0')
    PORT = config('PORT', default=8001, cast=int)
    EMBEDDING_API_SECRET = config('EMBEDDING_API_SECRET')

    DEFAULT_BATCH_SIZE = config('EMBEDDING_DEFAULT_BATCH_SIZE', default=8, cast=int)
    DEFAULT_NORMALIZE = True
    TORCH_NUM_THREADS = config('EMBEDDING_TORCH_NUM_THREADS', default=2, cast=int)

    PREFIX_QUERY = 'search_query: '
    PREFIX_DOCUMENT = 'search_document: '

    @classmethod
    def validate(cls):
        if not cls.EMBEDDING_API_SECRET:
            raise ValueError('EMBEDDING_API_SECRET ne peut pas être vide')

        if cls.EMBEDDING_API_SECRET == 'change-me-to-a-strong-secret-key-here':
            logger.warning(
                'ATTENTION: secret embedding par défaut — changez EMBEDDING_API_SECRET dans .env'
            )

        if cls.EMBEDDING_DIMENSION < 1 or cls.EMBEDDING_DIMENSION > 768:
            raise ValueError('EMBEDDING_DIMENSION doit être entre 1 et 768 pour nomic-embed-text-v1.5')

        return True
