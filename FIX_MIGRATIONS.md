# 🔧 Fix : Erreur "relation does not exist"

## Problème

Erreur en production :
```
relation "accounts_user" does not exist
LINE 1: SELECT 1 AS "a" FROM "accounts_user" WHERE "accounts_user"."...
```

**Cause** : Les migrations Django n'ont pas été appliquées à la base de données de production.

## ✅ Solution Immédiate (à faire maintenant)

Connectez-vous à votre serveur de production et exécutez :

```bash
cd Savr-back
docker compose exec django python manage.py migrate
```

Cela va créer toutes les tables manquantes dans la base de données.

## 🚀 Solution Permanente (pour éviter que ça se reproduise)

Un script d'entrypoint a été ajouté pour exécuter automatiquement les migrations au démarrage du conteneur Django.

**Pour l'activer :**

1. Récupérez les dernières modifications :
```bash
git pull
```

2. Reconstruisez l'image Django :
```bash
docker compose up -d --build django
```

Désormais, les migrations seront appliquées automatiquement à chaque démarrage du conteneur.

## 📝 Vérification

Pour vérifier que tout fonctionne :

```bash
# Vérifier que les migrations ont été appliquées
docker compose exec django python manage.py showmigrations

# Vérifier les logs du conteneur (vous devriez voir "✅ Migrations appliquées avec succès !")
docker compose logs django | grep -i migration
```

## 🔍 Diagnostic

Si le problème persiste :

1. **Vérifier la connexion à la base de données** :
```bash
docker compose exec django python manage.py dbshell
```

2. **Vérifier les variables d'environnement** :
```bash
docker compose exec django env | grep DB_
```

3. **Vérifier les logs complets** :
```bash
docker compose logs django
```

## 🔧 Erreur : "operator class gin_trgm_ops does not exist"

Si vous voyez cette erreur :
```
psycopg2.errors.UndefinedObject: operator class "gin_trgm_ops" does not exist for access method "gin"
```

**Cause** : L'extension PostgreSQL `pg_trgm` n'est pas installée dans votre base de données.

### Solution automatique (recommandée)

La migration a été modifiée pour installer automatiquement l'extension. Reconstruisez et relancez :

```bash
git pull
docker compose up -d --build django
```

### Solution manuelle (si la base de données ne permet pas l'installation via migrations)

Si vous utilisez une base de données externe (Neon, Supabase, etc.) qui nécessite des permissions spéciales :

1. **Connectez-vous à votre base de données** :
```bash
docker compose exec django python manage.py dbshell
```

2. **Installez l'extension manuellement** :
```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

Ou via psql directement :
```bash
psql -h $DB_HOST -U $DB_USER -d $DB_NAME -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"
```

3. **Vérifiez que l'extension est installée** :
```sql
SELECT * FROM pg_extension WHERE extname = 'pg_trgm';
```

4. **Relancez les migrations** :
```bash
docker compose exec django python manage.py migrate
```

### Extensions PostgreSQL requises

Pour que l'application fonctionne complètement, ces extensions doivent être installées :
- `pg_trgm` : Pour la recherche fuzzy (trigram similarity)
- `vector` : Pour les embeddings vectoriels (si utilisé)
- `unaccent` : Pour la recherche sans accents (optionnel)

La migration `0003_add_search_indexes.py` installe automatiquement `pg_trgm`.
La migration `0024_enable_pgvector.py` installe automatiquement `vector`.

