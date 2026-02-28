# Configuration de l'environnement - API d'Embedding

## 📋 Prérequis

- Python 3.8 ou supérieur
- pip (gestionnaire de paquets Python)

## 🚀 Installation rapide

### Option 1: Utiliser le script de démarrage (recommandé)

```bash
cd embeding
chmod +x start.sh
./start.sh
```

Le script va automatiquement:
- Créer l'environnement virtuel Python
- Installer toutes les dépendances
- Créer le fichier `.env` depuis `.env.example` si nécessaire
- Vérifier la configuration
- Démarrer l'API

### Option 2: Installation manuelle

#### 1. Créer l'environnement virtuel

```bash
cd embeding
python3 -m venv venv
source venv/bin/activate  # Sur Windows: venv\Scripts\activate
```

#### 2. Installer les dépendances

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

#### 3. Configurer les variables d'environnement

Créez un fichier `.env` à partir de `.env.example`:

```bash
cp .env.example .env
```

Puis éditez le fichier `.env` et modifiez les valeurs:

```env
# Secret pour l'authentification basique (OBLIGATOIRE)
# Génère un secret fort avec:
# python3 -c "import secrets; print(secrets.token_urlsafe(32))"
EMBEDDING_API_SECRET=votre-secret-fort-ici

# Configuration du serveur
PORT=8001
HOST=0.0.0.0
```

**⚠️ IMPORTANT**: Vous DEVEZ changer `EMBEDDING_API_SECRET` avec un secret fort!

#### 4. Générer un secret fort

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Copiez le résultat et remplacez `votre-secret-fort-ici` dans le fichier `.env`.

## 🔐 Configuration du secret

Le secret `EMBEDDING_API_SECRET` est utilisé pour l'authentification basique de l'API. Tous les endpoints (sauf `/` et `/health`) nécessitent un header:

```
X-API-Key: votre-secret-depuis-.env
```

### Utilisation dans d'autres projets

Si vous utilisez cette API depuis un autre projet (comme `Savr-back`), vous devez ajouter le même secret dans le fichier `.env` de ce projet:

**Dans `Savr-back/.env`:**
```env
EMBEDDING_API_SECRET=votre-secret-fort-ici
EMBEDDING_API_URL=http://localhost:8001
```

**Dans `Savr/.env` (si nécessaire):**
```env
EMBEDDING_API_SECRET=votre-secret-fort-ici
EMBEDDING_API_URL=http://localhost:8001
```

## 🎯 Démarrer l'API

### Avec le script

```bash
./start.sh
```

### Manuellement

```bash
source venv/bin/activate
python app.py
```

### Avec uvicorn directement

```bash
source venv/bin/activate
uvicorn app:app --host 0.0.0.0 --port 8001
```

L'API sera accessible sur `http://localhost:8001`

## 📖 Documentation

Une fois l'API lancée, accédez à la documentation interactive:
- **Swagger UI**: http://localhost:8001/docs
- **ReDoc**: http://localhost:8001/redoc

## ✅ Vérification

Testez que l'API fonctionne:

```bash
# Vérifier la santé de l'API
curl http://localhost:8001/health

# Tester avec authentification (remplacez YOUR_SECRET)
curl -X POST http://localhost:8001/embed \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_SECRET" \
  -d '{"text": "Hello, world!", "normalize": true}'
```

## 🔧 Dépannage

### Le fichier .env n'existe pas

Le script `start.sh` créera automatiquement le fichier `.env` depuis `.env.example`. Si vous installez manuellement, copiez `.env.example` vers `.env`.

### Erreur: "EMBEDDING_API_SECRET ne peut pas être vide"

Assurez-vous que:
1. Le fichier `.env` existe
2. Le fichier `.env` contient la ligne `EMBEDDING_API_SECRET=votre-secret`
3. Le secret n'est pas vide

### Le modèle ne se télécharge pas

Le modèle BGE-small-en-v1.5 (~130 MB) est téléchargé automatiquement au premier lancement. Assurez-vous d'avoir:
- Une connexion Internet active
- Suffisamment d'espace disque (~200 MB)

### Port déjà utilisé

Changez le port dans le fichier `.env`:

```env
PORT=8002
```

Ou utilisez uvicorn directement avec un autre port:

```bash
uvicorn app:app --host 0.0.0.0 --port 8002
```

## 📝 Notes

- Le fichier `.env` est dans `.gitignore` et ne sera pas commité
- Ne partagez jamais votre fichier `.env` avec des informations sensibles
- Utilisez `.env.example` comme template pour les autres développeurs
- Le modèle est chargé une seule fois au démarrage de l'API

