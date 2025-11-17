# Configuration Docker pour MinIO

## Lancer MinIO

Pour démarrer MinIO avec Docker Compose :

```bash
cd docker
docker-compose up -d
```

## Accès à MinIO

Une fois lancé, MinIO sera accessible via :

- **Console Web** : http://localhost:9001
- **API** : http://localhost:9000

**Identifiants par défaut** :
- Username: `minioadmin`
- Password: `minioadmin`

⚠️ **Important** : Changez ces identifiants en production !

## Arrêter MinIO

```bash
cd docker
docker-compose down
```

Pour supprimer également les volumes (données) :

```bash
docker-compose down -v
```

## Personnaliser les identifiants

Créez un fichier `.env` dans le dossier `docker/` :

```env
MINIO_ROOT_USER=votre-utilisateur
MINIO_ROOT_PASSWORD=votre-mot-de-passe
```

Puis modifiez le fichier `docker-compose.yml` pour utiliser ces variables :

```yaml
environment:
  MINIO_ROOT_USER: ${MINIO_ROOT_USER:-minioadmin}
  MINIO_ROOT_PASSWORD: ${MINIO_ROOT_PASSWORD:-minioadmin}
```

## Vérifier le statut

```bash
docker-compose ps
```

## Logs

```bash
docker-compose logs -f minio
```


