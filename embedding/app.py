"""
API d'embedding nomic-embed-text-v1.5 (Matryoshka 512d, optimisé CPU / Raspberry Pi).
"""

from __future__ import annotations

import logging
from typing import List, Literal

import numpy as np
import torch
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer

from config import Config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

try:
    Config.validate()
except Exception as e:
    logger.error('Erreur de configuration: %s', e)
    raise

torch.set_num_threads(Config.TORCH_NUM_THREADS)

app = FastAPI(
    title='Embedding API',
    description='Embeddings nomic-embed-text-v1.5 (Matryoshka)',
    version='2.0.0',
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)

logger.info('Chargement du modèle %s...', Config.MODEL_NAME)
model = SentenceTransformer(Config.MODEL_NAME, trust_remote_code=True)
logger.info('Modèle chargé (dim native=%s, sortie cible=%sd)', model.get_sentence_embedding_dimension(), Config.EMBEDDING_DIMENSION)


def prefix_texts(texts: list[str], input_type: str) -> list[str]:
    prefix = Config.PREFIX_QUERY if input_type == 'query' else Config.PREFIX_DOCUMENT
    return [prefix + t for t in texts]


def matryoshka_truncate(vectors: np.ndarray, dimensions: int, normalize: bool) -> np.ndarray:
    truncated = vectors[:, :dimensions].astype(np.float32)
    if normalize:
        norms = np.linalg.norm(truncated, axis=1, keepdims=True)
        truncated = truncated / np.clip(norms, 1e-12, None)
    return truncated


def encode_texts(
    texts: list[str],
    *,
    input_type: str,
    normalize: bool,
    dimensions: int,
    batch_size: int,
) -> list[list[float]]:
    if not texts:
        return []
    prefixed = prefix_texts(texts, input_type)
    raw = model.encode(
        prefixed,
        normalize_embeddings=False,
        show_progress_bar=False,
        batch_size=batch_size,
        convert_to_numpy=True,
    )
    if raw.ndim == 1:
        raw = raw.reshape(1, -1)
    out = matryoshka_truncate(raw, dimensions, normalize)
    return out.tolist()


class EmbeddingRequest(BaseModel):
    text: str = Field(..., min_length=1)
    normalize: bool = True
    input_type: Literal['query', 'passage'] = 'passage'
    dimensions: int = Field(default=512, ge=1, le=768)


class BatchEmbeddingRequest(BaseModel):
    texts: List[str] = Field(..., min_items=1)
    normalize: bool = True
    input_type: Literal['query', 'passage'] = 'passage'
    dimensions: int = Field(default=512, ge=1, le=768)


class EmbeddingResponse(BaseModel):
    embedding: List[float]
    dimension: int
    text: str


class BatchEmbeddingResponse(BaseModel):
    embeddings: List[List[float]]
    dimension: int
    count: int


class HealthResponse(BaseModel):
    status: str
    model: str
    dimension: int


async def verify_api_key(x_api_key: str = Header(..., alias='X-API-Key')):
    if x_api_key != Config.EMBEDDING_API_SECRET:
        raise HTTPException(status_code=401, detail="Clé API invalide.")
    return x_api_key


def _resolve_dimensions(requested: int) -> int:
    if requested != Config.EMBEDDING_DIMENSION:
        raise HTTPException(
            status_code=400,
            detail=f'dimensions doit être {Config.EMBEDDING_DIMENSION}',
        )
    return requested


@app.get('/', response_model=HealthResponse)
@app.get('/health', response_model=HealthResponse)
async def health():
    return {
        'status': 'healthy',
        'model': Config.MODEL_NAME,
        'dimension': Config.EMBEDDING_DIMENSION,
    }


@app.post('/embed', response_model=EmbeddingResponse, dependencies=[Depends(verify_api_key)])
async def create_embedding(request: EmbeddingRequest):
    dims = _resolve_dimensions(request.dimensions)
    batch_size = 1
    vectors = encode_texts(
        [request.text],
        input_type=request.input_type,
        normalize=request.normalize,
        dimensions=dims,
        batch_size=batch_size,
    )
    embedding = vectors[0]
    return {'embedding': embedding, 'dimension': len(embedding), 'text': request.text}


@app.post('/embed/batch', response_model=BatchEmbeddingResponse, dependencies=[Depends(verify_api_key)])
async def create_batch_embeddings(request: BatchEmbeddingRequest):
    if not request.texts:
        raise HTTPException(status_code=400, detail='La liste de textes ne peut pas être vide.')
    dims = _resolve_dimensions(request.dimensions)
    batch_size = 1 if request.input_type == 'query' else Config.DEFAULT_BATCH_SIZE
    embeddings = encode_texts(
        request.texts,
        input_type=request.input_type,
        normalize=request.normalize,
        dimensions=dims,
        batch_size=batch_size,
    )
    return {
        'embeddings': embeddings,
        'dimension': len(embeddings[0]) if embeddings else 0,
        'count': len(embeddings),
    }


if __name__ == '__main__':
    import uvicorn

    uvicorn.run(app, host=Config.HOST, port=Config.PORT)
