#!/bin/bash
# Script pour rattraper les embeddings manquants (ingrédients et recettes importés sans embedding)
# Usage:
#   Local:  ./scripts/regenerate_embeddings.sh
#   Docker: ./scripts/regenerate_embeddings.sh --docker

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

USE_DOCKER=false
EXTRA_ARGS=()

for arg in "$@"; do
    case $arg in
        --docker)
            USE_DOCKER=true
            ;;
        *)
            EXTRA_ARGS+=("$arg")
            ;;
    esac
done

echo "🔄 Génération des embeddings manquants..."
echo "   (ingrédients et recettes importés sans embedding)"
echo ""

if [ "$USE_DOCKER" = true ]; then
    # Vérifier que le service embedding est up
    if ! docker compose ps embedding 2>/dev/null | grep -q "Up"; then
        echo "⚠️  Le service 'embedding' n'est pas démarré."
        echo "   Lancez d'abord: docker compose up -d embedding"
        exit 1
    fi
    docker compose exec django python manage.py generate_missing_embeddings "${EXTRA_ARGS[@]}"
else
    python manage.py generate_missing_embeddings "${EXTRA_ARGS[@]}"
fi

echo ""
echo "✅ Terminé !"
