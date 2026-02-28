# 🔑 Configuration du Secret d'Authentification

Ce guide explique comment configurer le secret `EMBEDDING_API_SECRET` pour utiliser l'API d'embedding depuis les autres projets.

## 📝 Étapes de Configuration

### 1. Générer un Secret Fort

Générez un secret fort avec Python :

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Copiez le secret généré (ex: `abc123xyz...`).

### 2. Configurer le Secret dans l'API d'Embedding

Dans le fichier `embeding/.env` :

```env
EMBEDDING_API_SECRET=votre-secret-fort-ici
PORT=8001
HOST=0.0.0.0
```

### 3. Configurer le Secret dans Savr-back

Dans le fichier `Savr-back/.env`, ajoutez :

```env
# ... vos autres variables existantes ...

# Configuration de l'API d'Embedding
EMBEDDING_API_SECRET=votre-secret-fort-ici
EMBEDDING_API_URL=http://localhost:8001
```

⚠️ **Important**: Utilisez le **même secret** que dans `embeding/.env` !

### 4. Configurer le Secret dans Savr (si nécessaire)

Dans le fichier `Savr/.env`, ajoutez :

```env
# ... vos autres variables existantes ...

# Configuration de l'API d'Embedding
EMBEDDING_API_SECRET=votre-secret-fort-ici
EMBEDDING_API_URL=http://localhost:8001
```

⚠️ **Important**: Utilisez le **même secret** que dans les autres fichiers `.env` !

## 🔍 Vérification

### Tester l'API d'Embedding

```bash
cd embeding
./start.sh
```

Puis testez avec curl :

```bash
curl -X POST http://localhost:8001/embed \
  -H "Content-Type: application/json" \
  -H "X-API-Key: votre-secret-fort-ici" \
  -d '{"text": "Hello, world!", "normalize": true}'
```

### Utiliser l'API depuis Python (exemple pour Savr-back)

```python
import os
import requests
from decouple import config

# Récupérer le secret depuis .env
EMBEDDING_API_SECRET = config('EMBEDDING_API_SECRET')
EMBEDDING_API_URL = config('EMBEDDING_API_URL', default='http://localhost:8001')

# Faire une requête
response = requests.post(
    f"{EMBEDDING_API_URL}/embed",
    headers={"X-API-Key": EMBEDDING_API_SECRET},
    json={
        "text": "Votre texte à transformer",
        "normalize": True
    }
)

if response.status_code == 200:
    embedding = response.json()["embedding"]
    print(f"Embedding généré: {len(embedding)} dimensions")
else:
    print(f"Erreur: {response.status_code} - {response.text}")
```

## 📋 Checklist

- [ ] Secret généré avec `secrets.token_urlsafe(32)`
- [ ] Secret ajouté dans `embeding/.env`
- [ ] Secret ajouté dans `Savr-back/.env` (même valeur)
- [ ] Secret ajouté dans `Savr/.env` (même valeur, si nécessaire)
- [ ] API d'embedding démarrée et testée
- [ ] Test depuis Savr-back réussi

## 🚨 Sécurité

- ⚠️ Ne commitez **jamais** les fichiers `.env` dans Git
- ⚠️ Utilisez un secret différent pour chaque environnement (dev, staging, prod)
- ⚠️ Ne partagez jamais votre secret publiquement
- ⚠️ Changez le secret si vous pensez qu'il a été compromis

## 📚 Documentation

Pour plus d'informations, consultez :
- `embeding/README.md` - Documentation complète de l'API
- `embeding/ENV_SETUP.md` - Guide d'installation et configuration

