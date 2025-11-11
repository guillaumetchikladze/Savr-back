# Activer la recherche fuzzy pour les recettes

Pour activer la recherche fuzzy avancée avec trigram similarity, vous devez activer l'extension PostgreSQL `pg_trgm`.

## Activer l'extension pg_trgm

Connectez-vous à votre base de données PostgreSQL et exécutez :

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

Ou via psql :

```bash
psql -U votre_utilisateur -d votre_base_de_donnees -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"
```

## Vérifier que l'extension est activée

```bash
psql -U votre_utilisateur -d votre_base_de_donnees -c "SELECT * FROM pg_extension WHERE extname = 'pg_trgm';"
```

## Note

La recherche fonctionne déjà avec PostgreSQL Full-Text Search même sans l'extension `pg_trgm`. L'extension `pg_trgm` améliore encore la recherche en permettant des correspondances partielles plus flexibles (fuzzy matching).

