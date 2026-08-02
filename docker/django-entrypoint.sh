#!/bin/bash
set -e

echo "🚀 Démarrage du conteneur Django..."

# Fonction pour attendre que la base de données soit prête
wait_for_db() {
    echo "⏳ Attente de la base de données..."
    max_attempts=30
    attempt=0
    
    until python manage.py check --database default 2>/dev/null; do
        attempt=$((attempt + 1))
        if [ $attempt -ge $max_attempts ]; then
            echo "❌ Erreur : Impossible de se connecter à la base de données après $max_attempts tentatives"
            exit 1
        fi
        echo "⏳ Base de données non disponible, attente de 2 secondes... (tentative $attempt/$max_attempts)"
        sleep 2
    done
    echo "✅ Base de données prête !"
}

# Attendre que la base de données soit prête
wait_for_db

# Appliquer les migrations
echo "📦 Application des migrations..."
python manage.py migrate --noinput

echo "✅ Migrations appliquées avec succès !"

# CSS/JS admin (et autres statics) servis par WhiteNoise — pas via MinIO.
echo "📦 Collecte des fichiers static..."
python manage.py collectstatic --noinput
echo "✅ Static files prêts !"

# Exécuter la commande passée en argument (daphne par défaut pour HTTP + WebSocket)
echo "🎯 Démarrage du serveur ASGI (Daphne)..."
exec "$@"

