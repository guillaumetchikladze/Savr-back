# Guide de Déploiement en Production - Savr Backend

Guide complet pour déployer le backend Savr en production.

## 📋 Pré-requis

### Sur le serveur de production

- **Docker Engine** ≥ 24.0 et **Docker Compose** plugin installés
- **Ports ouverts** : 80 (HTTP) et 443 (HTTPS)
- **Nom de domaine** configuré :
  - Un domaine principal (ex: `api.mondomaine.com`) pointant en **A (IPv4)** vers l'IP du serveur
  - Optionnel : un sous-domaine pour MinIO (ex: `s3.mondomaine.com`) pointant vers la même IP
- **Base de données PostgreSQL** accessible (locale ou distante comme Neon, Supabase, etc.)

## 🚀 Première Installation

### 1. Cloner le repository

```bash
git clone <URL_DU_REPO> Savr-back
cd Savr-back
```

### 2. Configurer les variables d'environnement

Créer un fichier `.env` à la racine du projet :

```bash
cp ENV_EXAMPLE.txt .env
nano .env  # ou votre éditeur préféré
```

**Variables obligatoires à configurer :**

```env
# Base de données PostgreSQL
DB_HOST=your-db-host.com          # IP ou hostname de votre DB
DB_PORT=5432
DB_NAME=savr_db
DB_USER=your_db_user
DB_PASSWORD=your_secure_password

# Django - IMPORTANT pour la production
SECRET_KEY=changez-moi-par-une-cle-secrete-tres-longue-et-aleatoire
DEBUG=False

# Stockage S3/MinIO
AWS_ACCESS_KEY_ID=minioadmin          # Changez en production !
AWS_SECRET_ACCESS_KEY=minioadmin      # Changez en production !
AWS_BUCKET=savr

# Reverse proxy Caddy (HTTPS automatique)
DOMAIN=api.mondomaine.com
MINIO_DOMAIN=s3.mondomaine.com        # Optionnel
CADDY_EMAIL=admin@mondomaine.com      # Requis pour Let's Encrypt
```

> ⚠️ **Important** : Changez `SECRET_KEY`, `AWS_ACCESS_KEY_ID` et `AWS_SECRET_ACCESS_KEY` en production !

### 3. Vérifier la configuration DNS

Avant de lancer, assurez-vous que :
- Le domaine `api.mondomaine.com` pointe vers l'IP du serveur (vérifier avec `dig api.mondomaine.com`)
- Si vous utilisez MinIO avec un sous-domaine, `s3.mondomaine.com` doit aussi pointer vers la même IP

### 4. Lancer les services

```bash
docker compose up -d --build
```

Cette commande va :
- Construire les images Docker pour Django et Celery
- Démarrer tous les services (Redis, MinIO, Django, Celery, Caddy)
- Configurer automatiquement HTTPS via Let's Encrypt

### 5. Appliquer les migrations de base de données

> ⚠️ **Note** : Depuis la version avec le script d'entrypoint, les migrations sont appliquées automatiquement au démarrage du conteneur Django. Cette étape n'est plus nécessaire, mais vous pouvez toujours l'exécuter manuellement si besoin :

```bash
docker compose exec django python manage.py migrate
```

### 6. Créer un superutilisateur (optionnel)

```bash
docker compose exec django python manage.py createsuperuser
```

### 7. Vérifier que tout fonctionne

```bash
# Vérifier l'état des services
docker compose ps

# Vérifier les logs
docker compose logs -f django
docker compose logs -f caddy

# Tester l'API
curl https://api.mondomaine.com/api/
```

## 🔄 Mise à jour / Redéploiement

Pour mettre à jour l'application après des changements de code :

### Méthode simple (recommandée)

```bash
# 1. Récupérer les dernières modifications
git pull

# 2. Reconstruire les images et redémarrer les services
docker compose up -d --build django celery-worker caddy
```

C'est tout ! 🎉

### Explication

- `git pull` : Récupère le nouveau code
- `--build` : Reconstruit les images Docker avec le nouveau code
- `django celery-worker caddy` : Redémarre uniquement ces services (les autres comme Redis et MinIO continuent de tourner)

### Vérification après mise à jour

```bash
# Vérifier que les services sont bien redémarrés
docker compose ps

# Vérifier les logs pour détecter d'éventuelles erreurs
docker compose logs -f django

# Si des migrations ont été ajoutées, les appliquer
docker compose exec django python manage.py migrate
```

## 🛠️ Commandes Utiles

### Voir les logs en temps réel

```bash
docker compose logs -f django          # Logs Django
docker compose logs -f celery-worker    # Logs Celery
docker compose logs -f caddy            # Logs Caddy (HTTPS)
docker compose logs -f                 # Tous les logs
```

### Arrêter les services

```bash
docker compose down                    # Arrête tout (garde les volumes)
docker compose down -v                 # Arrête tout et supprime les volumes (⚠️ supprime les données !)
```

### Redémarrer un service spécifique

```bash
docker compose restart django
docker compose restart celery-worker
```

### Accéder au shell du container Django

```bash
docker compose exec django bash
```

### Exécuter des commandes Django

```bash
docker compose exec django python manage.py <commande>
docker compose exec django python manage.py migrate
docker compose exec django python manage.py createsuperuser
docker compose exec django python manage.py collectstatic  # Si nécessaire
```

## 📦 Services Déployés

| Service | Description | Ports |
|---------|-------------|-------|
| **Django** | API principale avec Gunicorn | 8000 (interne) |
| **Celery Worker** | Traitement des tâches asynchrones | - |
| **Redis** | Broker pour Celery | 6379 |
| **MinIO** | Stockage S3-compatible | 9000 (API), 9001 (Console) |
| **Caddy** | Reverse proxy + HTTPS automatique | 80, 443 |

## 🔒 Sécurité en Production

### Checklist avant la mise en production

- [ ] `SECRET_KEY` changé et sécurisé
- [ ] `DEBUG=False` dans le `.env`
- [ ] `AWS_ACCESS_KEY_ID` et `AWS_SECRET_ACCESS_KEY` changés (pas les valeurs par défaut)
- [ ] Mot de passe de base de données fort
- [ ] Certificats HTTPS fonctionnels (vérifier avec `curl -I https://api.mondomaine.com`)
- [ ] Firewall configuré (seuls les ports 80/443 ouverts)
- [ ] Backups de la base de données configurés
- [ ] Backups des volumes MinIO configurés (si MinIO local)

### Backups

**Base de données :**
```bash
# Exemple de backup PostgreSQL
docker compose exec -T <postgres_container> pg_dump -U $DB_USER $DB_NAME > backup_$(date +%Y%m%d).sql
```

**MinIO (volumes Docker) :**
```bash
# Backup du volume MinIO
docker run --rm -v savr-back_minio-data:/data -v $(pwd):/backup alpine tar czf /backup/minio-backup-$(date +%Y%m%d).tar.gz /data
```

## 🐛 Dépannage

### Les services ne démarrent pas

```bash
# Vérifier les logs
docker compose logs

# Vérifier la configuration
docker compose config
```

### Problème de certificats HTTPS

- Vérifier que le domaine pointe bien vers l'IP du serveur
- Vérifier que les ports 80/443 sont ouverts
- Vérifier les logs Caddy : `docker compose logs caddy`

### Problème de connexion à la base de données

- Vérifier `DB_HOST`, `DB_USER`, `DB_PASSWORD` dans le `.env`
- Vérifier que la base de données est accessible depuis le serveur
- Tester la connexion : `docker compose exec django python manage.py dbshell`

### Erreur "relation does not exist" (tables manquantes)

Si vous voyez une erreur comme `relation "accounts_user" does not exist`, cela signifie que les migrations n'ont pas été appliquées :

**Solution immédiate :**
```bash
# Appliquer les migrations manuellement
docker compose exec django python manage.py migrate
```

**Solution permanente :**
Le script d'entrypoint exécute automatiquement les migrations au démarrage. Si vous avez une ancienne version, reconstruisez l'image :
```bash
docker compose up -d --build django
```

Les migrations seront appliquées automatiquement au prochain démarrage.

### MinIO ne fonctionne pas

- Accéder à la console : `http://votre-serveur:9001`
- Identifiants par défaut : `minioadmin` / `minioadmin` (ou ceux définis dans `.env`)

## 📝 Notes

- **Certificats HTTPS** : Caddy renouvelle automatiquement les certificats Let's Encrypt. Aucune action requise.
- **Volumes Docker** : Les données (Redis, MinIO) sont persistées dans des volumes Docker. Ils ne sont pas supprimés lors d'un `docker compose down` (sauf avec `-v`).
- **Migrations** : Après chaque `git pull`, vérifier s'il y a de nouvelles migrations et les appliquer avec `python manage.py migrate`.

## 🆘 Support

En cas de problème, vérifier dans l'ordre :
1. Les logs : `docker compose logs -f`
2. L'état des services : `docker compose ps`
3. La configuration : `docker compose config`
4. Les variables d'environnement : `cat .env`










