"""
Configuration de l'API d'embedding
Centralise toutes les variables de configuration
"""

from decouple import config
import logging

logger = logging.getLogger(__name__)


class Config:
    """Configuration de l'API d'embedding"""
    
    # Modèle d'embedding
    MODEL_NAME = 'BAAI/bge-small-en-v1.5'
    
    # Configuration serveur
    HOST = config('HOST', default='0.0.0.0')
    PORT = config('PORT', default=8001, cast=int)
    
    # Authentification
    EMBEDDING_API_SECRET = config('EMBEDDING_API_SECRET')
    
    # Paramètres du modèle
    DEFAULT_BATCH_SIZE = 32
    DEFAULT_NORMALIZE = True
    
    @classmethod
    def validate(cls):
        """Valide la configuration"""
        if not cls.EMBEDDING_API_SECRET:
            raise ValueError("EMBEDDING_API_SECRET ne peut pas être vide")
        
        if cls.EMBEDDING_API_SECRET == "change-me-to-a-strong-secret-key-here":
            logger.warning("⚠️  ATTENTION: Vous utilisez le secret par défaut! Changez-le dans .env")
        
        return True

