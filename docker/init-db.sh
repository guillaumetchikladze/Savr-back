#!/bin/bash
set -e

# Script d'initialisation de la base de données PostgreSQL
# Active l'extension pgvector nécessaire pour les embeddings

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    -- Activer l'extension pgvector pour les recherches vectorielles
    CREATE EXTENSION IF NOT EXISTS vector;
    
    -- Activer l'extension unaccent pour la recherche sans accents
    CREATE EXTENSION IF NOT EXISTS unaccent;
    
    -- Activer l'extension pg_trgm pour la recherche fuzzy
    CREATE EXTENSION IF NOT EXISTS pg_trgm;
    
    -- Afficher les extensions installées
    SELECT extname, extversion FROM pg_extension;
EOSQL

echo "Extensions PostgreSQL installées avec succès!"


