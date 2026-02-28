"""
API d'embedding utilisant BGE-small-en-v1.5
Fournit des endpoints pour générer des embeddings de texte
"""

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List
from sentence_transformers import SentenceTransformer
import logging

from config import Config

# Configuration du logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Validation de la configuration
try:
    Config.validate()
except Exception as e:
    logger.error(f"Erreur de configuration: {str(e)}")
    logger.error("Assurez-vous que le fichier .env existe et contient EMBEDDING_API_SECRET")
    raise

# Initialisation de FastAPI
app = FastAPI(
    title="Embedding API",
    description="API pour générer des embeddings de texte avec BGE-small",
    version="1.0.0"
)

# Configuration CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # En production, spécifier les origines autorisées
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Chargement du modèle BGE-small (chargé une seule fois au démarrage)
logger.info(f"Chargement du modèle {Config.MODEL_NAME}...")
try:
    model = SentenceTransformer(Config.MODEL_NAME)
    logger.info(f"✅ Modèle {Config.MODEL_NAME} chargé avec succès!")
    logger.info(f"📊 Dimension des embeddings: {model.get_sentence_embedding_dimension()}")
except Exception as e:
    logger.error(f"❌ Erreur lors du chargement du modèle: {str(e)}")
    logger.error("Assurez-vous d'avoir une connexion Internet pour télécharger le modèle")
    logger.error("Le modèle sera téléchargé depuis Hugging Face (~130 MB)")
    raise


# Modèles Pydantic pour les requêtes/réponses
class EmbeddingRequest(BaseModel):
    """Requête pour générer un embedding"""
    text: str = Field(..., description="Texte à transformer en embedding", min_length=1)
    normalize: bool = Field(default=True, description="Normaliser l'embedding (L2 norm)")


class BatchEmbeddingRequest(BaseModel):
    """Requête pour générer plusieurs embeddings"""
    texts: List[str] = Field(..., description="Liste de textes à transformer", min_items=1)
    normalize: bool = Field(default=True, description="Normaliser les embeddings (L2 norm)")


class EmbeddingResponse(BaseModel):
    """Réponse avec l'embedding généré"""
    embedding: List[float] = Field(..., description="Vecteur d'embedding")
    dimension: int = Field(..., description="Dimension du vecteur")
    text: str = Field(..., description="Texte original")


class BatchEmbeddingResponse(BaseModel):
    """Réponse avec plusieurs embeddings"""
    embeddings: List[List[float]] = Field(..., description="Liste de vecteurs d'embedding")
    dimension: int = Field(..., description="Dimension des vecteurs")
    count: int = Field(..., description="Nombre d'embeddings générés")


class HealthResponse(BaseModel):
    """Réponse de santé de l'API"""
    status: str
    model: str
    dimension: int


# Authentification basique via header
async def verify_api_key(x_api_key: str = Header(..., alias="X-API-Key")):
    """Vérifie que la clé API fournie est correcte"""
    if x_api_key != Config.EMBEDDING_API_SECRET:
        raise HTTPException(
            status_code=401,
            detail="Clé API invalide. Fournissez un header 'X-API-Key' valide."
        )
    return x_api_key


# Endpoints
@app.get("/", response_model=HealthResponse)
async def root():
    """Endpoint de base - informations sur l'API"""
    return {
        "status": "ok",
        "model": Config.MODEL_NAME,
        "dimension": model.get_sentence_embedding_dimension()
    }


@app.get("/health", response_model=HealthResponse)
async def health():
    """Endpoint de santé de l'API"""
    return {
        "status": "healthy",
        "model": Config.MODEL_NAME,
        "dimension": model.get_sentence_embedding_dimension()
    }


@app.post("/embed", response_model=EmbeddingResponse, dependencies=[Depends(verify_api_key)])
async def create_embedding(request: EmbeddingRequest):
    """
    Génère un embedding pour un texte unique
    
    - **text**: Texte à transformer en embedding
    - **normalize**: Si True, normalise l'embedding (L2 norm)
    """
    try:
        # Génération de l'embedding
        embedding = model.encode(
            request.text,
            normalize_embeddings=request.normalize,
            show_progress_bar=False
        )
        
        # Conversion en liste Python
        embedding_list = embedding.tolist()
        
        return {
            "embedding": embedding_list,
            "dimension": len(embedding_list),
            "text": request.text
        }
    except Exception as e:
        logger.error(f"Erreur lors de la génération de l'embedding: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Erreur lors de la génération de l'embedding: {str(e)}"
        )


@app.post("/embed/batch", response_model=BatchEmbeddingResponse, dependencies=[Depends(verify_api_key)])
async def create_batch_embeddings(request: BatchEmbeddingRequest):
    """
    Génère des embeddings pour plusieurs textes en une seule requête
    
    - **texts**: Liste de textes à transformer en embeddings
    - **normalize**: Si True, normalise les embeddings (L2 norm)
    """
    try:
        if len(request.texts) == 0:
            raise HTTPException(
                status_code=400,
                detail="La liste de textes ne peut pas être vide"
            )
        
        # Génération des embeddings en batch
        embeddings = model.encode(
            request.texts,
            normalize_embeddings=request.normalize,
            show_progress_bar=False,
            batch_size=Config.DEFAULT_BATCH_SIZE,
            convert_to_numpy=True
        )
        
        # Conversion en liste de listes Python
        if len(embeddings.shape) == 1:
            embeddings_list = [embeddings.tolist()]
        else:
            embeddings_list = embeddings.tolist()
        
        return {
            "embeddings": embeddings_list,
            "dimension": len(embeddings_list[0]) if len(embeddings_list) > 0 else 0,
            "count": len(embeddings_list)
        }
    except Exception as e:
        logger.error(f"Erreur lors de la génération des embeddings: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Erreur lors de la génération des embeddings: {str(e)}"
        )


if __name__ == "__main__":
    import uvicorn
    logger.info(f"Démarrage de l'API d'embedding sur {Config.HOST}:{Config.PORT}")
    uvicorn.run(app, host=Config.HOST, port=Config.PORT)

