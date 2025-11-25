#!/bin/sh
set -e

# Générer le Caddyfile à partir des variables d'environnement
DOMAIN=${DOMAIN:-localhost}
MINIO_DOMAIN=${MINIO_DOMAIN:-}
CADDY_EMAIL=${CADDY_EMAIL:-}

# Si MINIO_DOMAIN n'est pas défini, utiliser un sous-domaine par défaut
if [ -z "$MINIO_DOMAIN" ]; then
    MINIO_DOMAIN="minio.${DOMAIN}"
fi

# Créer le Caddyfile
cat > /etc/caddy/Caddyfile <<EOF
{
    # Configuration globale Caddy
EOF

# Activer HTTPS automatique si un email est fourni (production)
if [ -n "$CADDY_EMAIL" ]; then
    echo "    email $CADDY_EMAIL" >> /etc/caddy/Caddyfile
    # HTTPS automatique activé par défaut quand email est défini
else
    # Désactiver HTTPS automatique en développement
    echo "    auto_https off" >> /etc/caddy/Caddyfile
fi

# Continuer le Caddyfile
cat >> /etc/caddy/Caddyfile <<EOF
}

# Reverse proxy pour Django
${DOMAIN} {
    reverse_proxy django:8000
}

# Reverse proxy pour MinIO
${MINIO_DOMAIN} {
    reverse_proxy minio:9000
}
EOF

# Exécuter Caddy
exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile

