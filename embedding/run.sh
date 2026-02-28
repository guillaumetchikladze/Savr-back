#!/bin/bash
# Script rapide pour lancer l'API d'embedding sur le port 8001

# Activer l'environnement virtuel si disponible
if [ -d "venv" ]; then
    source venv/bin/activate
fi

# Lancer uvicorn sur le port 8001
uvicorn app:app --host 0.0.0.0 --port 8001

