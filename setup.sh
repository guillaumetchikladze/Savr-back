#!/bin/bash

# Script de configuration pour Savr Backend

echo "🚀 Configuration de Savr Backend..."

# Créer un environnement virtuel
if [ ! -d "venv" ]; then
    echo "📦 Création de l'environnement virtuel..."
    python3 -m venv venv
fi

# Activer l'environnement virtuel
echo "🔌 Activation de l'environnement virtuel..."
source venv/bin/activate

# Installer les dépendances
echo "📥 Installation des dépendances..."
pip install -r requirements.txt

# Créer le fichier .env s'il n'existe pas
if [ ! -f ".env" ]; then
    echo "📝 Création du fichier .env..."
    cat > .env << EOF
SECRET_KEY=$(python3 -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())')
DEBUG=True
EOF
    echo "✅ Fichier .env créé avec une clé secrète générée"
fi

# Appliquer les migrations
echo "🗄️  Application des migrations..."
python manage.py makemigrations
python manage.py migrate

echo "✅ Configuration terminée !"
echo ""
echo "Pour démarrer le serveur :"
echo "  source venv/bin/activate"
echo "  python manage.py runserver"

