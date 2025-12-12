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

