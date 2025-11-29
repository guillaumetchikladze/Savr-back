# Savr Backend - Django REST Framework

Backend API pour l'application Savr.

## Installation

1. Créer un environnement virtuel :
```bash
python -m venv venv
source venv/bin/activate  # Sur Windows: venv\Scripts\activate
```

2. Installer les dépendances :
```bash
pip install -r requirements.txt
```

3. Créer un fichier `.env` à la racine :
```
SECRET_KEY=your-secret-key-here
DEBUG=True
```

4. Appliquer les migrations :
```bash
python manage.py makemigrations
python manage.py migrate
```

5. Créer un superutilisateur (optionnel) :
```bash
python manage.py createsuperuser
```

## Démarrage

### Pour développement local (simulateur/émulateur)
```bash
python manage.py runserver
```

### Pour Expo Go sur appareil physique
Pour que l'API soit accessible depuis votre téléphone via Expo Go, lancez le serveur sur toutes les interfaces :

```bash
python manage.py runserver 0.0.0.0:8000
```

L'API sera disponible sur :
- `http://localhost:8000/api/` (depuis votre ordinateur)
- `http://VOTRE_IP_LOCALE:8000/api/` (depuis votre téléphone)

**Important** : Assurez-vous que votre téléphone et votre ordinateur sont sur le même réseau WiFi.

## Endpoints

- `POST /api/auth/register/` - Inscription
- `POST /api/auth/login/` - Connexion
- `GET /api/auth/profile/` - Profil utilisateur (authentifié)
- `POST /api/auth/token/refresh/` - Rafraîchir le token JWT

## Structure

- `accounts/` - Application d'authentification et gestion des utilisateurs
- `savr_back/` - Configuration principale du projet

## Déploiement Docker (production)

Le dossier `docker/` contient tout le nécessaire pour lancer l'API derrière Caddy (reverse proxy + HTTPS), MinIO et Redis. Voici le déroulé complet pour mettre l'API en production.

### 1. Pré-requis côté serveur

- Docker Engine ≥ 24 et Docker Compose plugin installés
- Ports 80/443 ouverts vers le serveur (HTTP/HTTPS)
- Un nom de domaine (ex. `api.mondomaine.com`) pointant en **A (IPv4)** vers l'IP du serveur
- Facultatif mais recommandé : un sous-domaine pour MinIO (ex. `s3.mondomaine.com`) pointant vers la même IP

### 2. Fichiers à créer

1. **Cloner le repo** sur le serveur :
   ```bash
   git clone https://github.com/…/Savr-back.git
   cd Savr-back
   ```
2. **Créer le fichier `.env`** à la racine (copier depuis `.env.example`) et compléter :
   ```
   # Base de données (ex. Neon, RDS…)
   DB_HOST=mydb.example.com
   DB_PORT=5432
   DB_NAME=savr_db
   DB_USER=savr
   DB_PASSWORD=********

   # Django
   SECRET_KEY=change-me-in-prod
   DEBUG=False

   # Stockage S3/MinIO
   AWS_ACCESS_KEY_ID=minioadmin
   AWS_SECRET_ACCESS_KEY=minioadmin
   AWS_BUCKET=savr

   # Reverse proxy Caddy
   DOMAIN=api.mondomaine.com
   MINIO_DOMAIN=s3.mondomaine.com
   CADDY_EMAIL=admin@mondomaine.com   # requis pour que Let's Encrypt émette les certificats
   ```
   > Astuce : si vous utilisez la base distante (Neon, Supabase, etc.), laissez le service PostgreSQL du compose désactivé (il n'est plus présent par défaut) et ajustez `DB_HOST`.

### 3. Démarrage des services

```bash
# Toujours dans Savr-back/
docker compose up -d --build redis minio django celery-worker caddy
```

Compose bricole automati­quement :

- `django` : Gunicorn sur port interne 8000
- `celery-worker` : worker Celery (file redis)
- `redis` : broker/résultat Celery
- `minio` : stockage S3-compatible (ports 9000/9001)
- `caddy` : reverse proxy / certificats auto Let's Encrypt

### 4. Vérifications rapides

```bash
docker compose ps
curl -vk https://api.mondomaine.com/api/   # devrait renvoyer les routes DRF
docker compose logs -f django              # vérifier qu'il n'y a pas d'erreur
docker compose logs -f caddy               # vérifier l'émission des certificats
```

### 5. Mise à jour / redéploiement

```bash
git pull
docker compose up -d --build django celery-worker caddy
```

### 6. Rappels opérationnels

- **Certificats** : Caddy renouvelle automatiquement (Let's Encrypt). Assurez-vous que les enregistrements DNS A/AAAA restent valides.
- **Backups** : si vous utilisez MinIO local, le volume `minio-data` doit être sauvegardé (snapshots Docker volume ou synchronisation S3).
- **Logs** : `docker compose logs -f <service>` pour diagnostiquer rapidement.
- **Rollbacks** : `docker compose down` pour tout arrêter (garde les volumes). Ajouter `-v` pour purger les données.

