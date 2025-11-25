# Docker Configuration pour Savr Backend

Ce dossier contient la configuration Docker pour l'application Savr.

## Services

Le `docker-compose.yml` configure les services suivants :

### 1. PostgreSQL avec pgvector
- **Image**: `pgvector/pgvector:pg16`
- **Port**: 5432
- **Base de données**: savr_db
- **Extensions installées**:
  - `vector`: Pour les embeddings et la recherche sémantique
  - `unaccent`: Pour la recherche sans accents
  - `pg_trgm`: Pour la recherche fuzzy/approximative

### 2. Redis
- **Image**: `redis:7.4-alpine`
- **Port**: 6379
- **Usage**: Backend pour Celery (tâches asynchrones)

### 3. MinIO
- **Image**: `minio/minio:RELEASE.2024-10-13T13-34-11Z`
- **Ports**: 
  - 9000: API MinIO (compatible S3)
  - 9001: Console Web MinIO
- **Usage**: Stockage des images et fichiers (compatible S3)

### 4. Django avec Gunicorn
- **Build**: Dockerfile.django
- **Port**: 8000 (interne, exposé via Caddy)
- **Usage**: Application Django principale avec Gunicorn comme serveur WSGI

### 5. Celery Worker
- **Build**: Dockerfile.celery
- **Usage**: Traitement des tâches asynchrones (import de recettes, génération d'embeddings, etc.)

### 6. Caddy
- **Image**: `caddy:2.8-alpine`
- **Ports**: 
  - 80: HTTP
  - 443: HTTPS
- **Usage**: Reverse proxy et gestion automatique de HTTPS

## Démarrage

### 1. Configuration initiale

Créer un fichier `.env` à la racine du projet (copier depuis `.env.example`) :

```bash
cp .env.example .env
```

Éditer le fichier `.env` avec vos configurations.

### 2. Configuration des variables d'environnement

Créer un fichier `.env` à la racine avec les variables suivantes :

```bash
# Base de données
DB_NAME=savr_db
DB_USER=postgres
DB_PASSWORD=postgres
DB_PORT=5432

# Django
SECRET_KEY=your-secret-key-here-change-in-production
DEBUG=False

# AWS/MinIO
AWS_ACCESS_KEY_ID=minioadmin
AWS_SECRET_ACCESS_KEY=minioadmin
AWS_BUCKET=savr

# Caddy - Configuration du reverse proxy
DOMAIN=yourdomain.com
MINIO_DOMAIN=minio.yourdomain.com
CADDY_EMAIL=your-email@example.com
```

**Pour la production** : Configurez `DOMAIN` avec votre domaine réel et `CADDY_EMAIL` pour activer HTTPS automatique.

**Pour le développement local** : Utilisez `DOMAIN=localhost` (par défaut).

### 3. Lancer les services

```bash
# Depuis le dossier Savr-back
docker-compose up -d
```

### 4. Initialiser la base de données

```bash
# Appliquer les migrations Django
docker-compose exec django python manage.py migrate

# Créer un superutilisateur
docker-compose exec django python manage.py createsuperuser

# Initialiser les catégories (optionnel)
docker-compose exec django python manage.py init_categories
```

### 5. Configurer MinIO

Accéder à la console MinIO :
- **En développement** : http://localhost:9001
- **En production** : http://minio.yourdomain.com (si MINIO_DOMAIN est configuré)

- **Username**: minioadmin (ou la valeur de AWS_ACCESS_KEY_ID)
- **Password**: minioadmin (ou la valeur de AWS_SECRET_ACCESS_KEY)

Créer un bucket nommé `savr` (ou le nom configuré dans AWS_BUCKET) et le rendre public.

### 6. Accéder à l'application

- **En développement** : http://localhost (via Caddy)
- **En production** : https://yourdomain.com (HTTPS automatique via Caddy)

L'API Django est accessible via Caddy qui fait le reverse proxy vers le conteneur Django.

## Commandes utiles

### Voir les logs

```bash
# Tous les services
docker-compose logs -f

# Un service spécifique
docker-compose logs -f postgres
docker-compose logs -f django
docker-compose logs -f celery-worker
docker-compose logs -f caddy
```

### Arrêter les services

```bash
docker-compose down
```

### Arrêter et supprimer les volumes (⚠️ perte de données)

```bash
docker-compose down -v
```

### Redémarrer un service

```bash
docker-compose restart django
docker-compose restart celery-worker
docker-compose restart caddy
```

### Accéder au shell PostgreSQL

```bash
docker-compose exec postgres psql -U postgres -d savr_db
```

### Accéder au shell Redis

```bash
docker-compose exec redis redis-cli
```

## Configuration pour le développement

Pour utiliser les services Docker avec votre application Django en local :

1. Les services sont accessibles sur `localhost`
2. Configurez votre `.env` avec :
   ```
   DOMAIN=localhost
   DEBUG=True
   ```
3. L'application Django est accessible via Caddy sur http://localhost

## Configuration pour la production

Pour déployer en production :

1. Configurez votre `.env` avec votre domaine réel :
   ```
   DOMAIN=yourdomain.com
   MINIO_DOMAIN=minio.yourdomain.com
   CADDY_EMAIL=your-email@example.com
   DEBUG=False
   SECRET_KEY=your-strong-secret-key-here
   ```

2. Assurez-vous que les DNS pointent vers votre serveur :
   - `yourdomain.com` → IP du serveur
   - `minio.yourdomain.com` → IP du serveur (optionnel)

3. Lancez avec `docker-compose up -d`

4. Caddy configurera automatiquement HTTPS via Let's Encrypt si `CADDY_EMAIL` est défini et que `auto_https` est activé (actuellement désactivé dans le script, à activer pour la production)

## Migration depuis Neon

Si vous migrez depuis Neon :

1. **Exporter les données de Neon** :
   ```bash
   pg_dump -h your-neon-host -U your-user -d your-db > backup.sql
   ```

2. **Importer dans PostgreSQL local** :
   ```bash
   docker-compose exec -T postgres psql -U postgres -d savr_db < backup.sql
   ```

3. **Mettre à jour votre .env** pour pointer vers la base locale

## Troubleshooting

### Le service PostgreSQL ne démarre pas
- Vérifier les logs : `docker-compose logs postgres`
- Vérifier que le port 5432 n'est pas déjà utilisé

### Celery ne traite pas les tâches
- Vérifier les logs : `docker-compose logs celery-worker`
- Vérifier que Redis est accessible
- Vérifier que la base de données est accessible

### MinIO ne démarre pas
- Vérifier que les ports 9000 et 9001 ne sont pas déjà utilisés
- Vérifier les permissions sur le volume minio-data

### Erreur "pgvector extension not found"
- Le service PostgreSQL devrait créer automatiquement l'extension
- Sinon, exécuter manuellement : `docker-compose exec postgres psql -U postgres -d savr_db -c "CREATE EXTENSION vector;"`
