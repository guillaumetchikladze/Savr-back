# 🔍 API d'Embedding - BGE-small

API FastAPI pour générer des embeddings de texte en utilisant le modèle **BGE-small-en-v1.5**.

## 📋 Prérequis

- Python 3.8+
- pip

## 🚀 Installation

### 1. Créer l'environnement virtuel

```bash
cd embeding
python3 -m venv venv
source venv/bin/activate  # Sur Windows: venv\Scripts\activate
```

### 2. Installer les dépendances

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

**Note**: Le premier lancement téléchargera automatiquement le modèle BGE-small-en-v1.5 (~130 MB). Cela peut prendre quelques minutes.

### 3. Configurer les variables d'environnement

Copiez le fichier `.env.example` vers `.env` et modifiez les valeurs :

```bash
cp .env.example .env
```

Puis éditez `.env` et changez le secret :

```env
EMBEDDING_API_SECRET=votre-secret-personnalise-ici
PORT=8001
HOST=0.0.0.0
```

## 🎯 Utilisation

### Démarrer l'API

```bash
# Activer l'environnement virtuel
source venv/bin/activate

# Lancer l'API
python app.py
```

Ou avec uvicorn directement :

```bash
uvicorn app:app --host 0.0.0.0 --port 8001
```

L'API sera accessible sur `http://localhost:8001`

### Documentation interactive

Une fois l'API lancée, accédez à la documentation interactive :
- Swagger UI: `http://localhost:8001/docs`
- ReDoc: `http://localhost:8001/redoc`

## 📡 Endpoints

### `GET /` ou `GET /health`

Vérifie l'état de l'API et retourne des informations sur le modèle.

**Réponse**:
```json
{
  "status": "ok",
  "model": "BAAI/bge-small-en-v1.5",
  "dimension": 384
}
```

### `POST /embed`

Génère un embedding pour un texte unique.

**Headers requis**:
```
X-API-Key: votre-secret-depuis-.env
```

**Body**:
```json
{
  "text": "Votre texte à transformer en embedding",
  "normalize": true
}
```

**Réponse**:
```json
{
  "embedding": [0.123, -0.456, ...],
  "dimension": 384,
  "text": "Votre texte à transformer en embedding"
}
```

### `POST /embed/batch`

Génère des embeddings pour plusieurs textes en une seule requête.

**Headers requis**:
```
X-API-Key: votre-secret-depuis-.env
```

**Body**:
```json
{
  "texts": [
    "Premier texte",
    "Deuxième texte",
    "Troisième texte"
  ],
  "normalize": true
}
```

**Réponse**:
```json
{
  "embeddings": [
    [0.123, -0.456, ...],
    [0.789, -0.012, ...],
    [0.345, -0.678, ...]
  ],
  "dimension": 384,
  "count": 3
}
```

## 🔐 Authentification

Tous les endpoints (sauf `/` et `/health`) nécessitent une authentification via header :

```
X-API-Key: votre-secret-depuis-.env
```

## 🧪 Exemples d'utilisation

### Avec curl

```bash
# Vérifier la santé de l'API
curl http://localhost:8001/health

# Générer un embedding
curl -X POST http://localhost:8001/embed \
  -H "Content-Type: application/json" \
  -H "X-API-Key: votre-secret" \
  -d '{
    "text": "Hello, world!",
    "normalize": true
  }'

# Générer plusieurs embeddings
curl -X POST http://localhost:8001/embed/batch \
  -H "Content-Type: application/json" \
  -H "X-API-Key: votre-secret" \
  -d '{
    "texts": ["Texte 1", "Texte 2", "Texte 3"],
    "normalize": true
  }'
```

### Avec Python

```python
import requests

API_URL = "http://localhost:8001"
API_KEY = "votre-secret-depuis-.env"

# Générer un embedding
response = requests.post(
    f"{API_URL}/embed",
    headers={"X-API-Key": API_KEY},
    json={
        "text": "Hello, world!",
        "normalize": True
    }
)
embedding = response.json()["embedding"]
print(f"Dimension: {len(embedding)}")

# Générer plusieurs embeddings
response = requests.post(
    f"{API_URL}/embed/batch",
    headers={"X-API-Key": API_KEY},
    json={
        "texts": ["Texte 1", "Texte 2", "Texte 3"],
        "normalize": True
    }
)
embeddings = response.json()["embeddings"]
print(f"Nombre d'embeddings: {len(embeddings)}")
```

## 📊 Spécifications du modèle

- **Modèle**: BAAI/bge-small-en-v1.5
- **Dimension**: 384
- **Langue**: Anglais (mais fonctionne bien avec d'autres langues)
- **Taille**: ~130 MB

## ⚙️ Configuration

Les variables d'environnement dans `.env` :

- `EMBEDDING_API_SECRET`: Secret pour l'authentification (obligatoire)
- `PORT`: Port sur lequel l'API écoute (défaut: 8001)
- `HOST`: Host sur lequel l'API écoute (défaut: 0.0.0.0)

### 🔑 Utilisation du secret dans d'autres projets

Si vous utilisez cette API depuis d'autres projets (comme `Savr-back` ou `Savr`), vous devez ajouter le même secret dans leurs fichiers `.env` :

**Dans `Savr-back/.env`:**
```env
# ... autres variables ...
EMBEDDING_API_SECRET=votre-secret-fort-ici
EMBEDDING_API_URL=http://localhost:8001
```

**Dans `Savr/.env` (si nécessaire):**
```env
# ... autres variables ...
EMBEDDING_API_SECRET=votre-secret-fort-ici
EMBEDDING_API_URL=http://localhost:8001
```

⚠️ **Important**: Utilisez le **même secret** dans tous les fichiers `.env` pour que l'authentification fonctionne correctement.

## 🔧 Dépannage

### Le modèle ne se télécharge pas

Assurez-vous d'avoir une connexion Internet active. Le modèle est téléchargé depuis Hugging Face au premier lancement.

### Erreur de mémoire

Si vous rencontrez des problèmes de mémoire, vous pouvez réduire la taille du batch dans `config.py` (modifiez `DEFAULT_BATCH_SIZE`).

### Port déjà utilisé

Changez le port dans le fichier `.env` ou utilisez un autre port :

```bash
uvicorn app:app --host 0.0.0.0 --port 8002
```

## 📝 Notes

- Le modèle est chargé une seule fois au démarrage de l'API
- Les embeddings sont normalisés par défaut (L2 norm)
- L'API supporte CORS pour être utilisée depuis un frontend
- En production, modifiez `allow_origins` dans `app.py` pour restreindre les origines autorisées

