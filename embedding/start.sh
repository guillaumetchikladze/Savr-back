#!/bin/bash

# Script de démarrage de l'API d'embedding

set -e  # Arrêter en cas d'erreur

# Couleurs pour les messages
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}🚀 Démarrage de l'API d'Embedding (nomic 512d)${NC}"

# Vérifier si Python 3 est installé
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}❌ Python 3 n'est pas installé!${NC}"
    exit 1
fi

# Vérifier si l'environnement virtuel existe
if [ ! -d "venv" ]; then
    echo -e "${YELLOW}📦 Création de l'environnement virtuel...${NC}"
    python3 -m venv venv
    echo -e "${GREEN}✅ Environnement virtuel créé${NC}"
fi

# Activer l'environnement virtuel
echo -e "${YELLOW}🔧 Activation de l'environnement virtuel...${NC}"
source venv/bin/activate

# Vérifier si les dépendances sont installées
if [ ! -f "venv/.installed" ] || [ "requirements.txt" -nt "venv/.installed" ]; then
    echo -e "${YELLOW}📥 Installation des dépendances...${NC}"
    pip install --upgrade pip --quiet
    pip install -r requirements.txt
    touch venv/.installed
    echo -e "${GREEN}✅ Dépendances installées${NC}"
fi

# Vérifier si le fichier .env existe
if [ ! -f ".env" ]; then
    echo -e "${YELLOW}⚠️  Le fichier .env n'existe pas!${NC}"
    if [ -f ".env.example" ]; then
        echo -e "${YELLOW}📝 Création depuis .env.example...${NC}"
        cp .env.example .env
        echo -e "${GREEN}✅ Fichier .env créé${NC}"
        echo -e "${RED}⚠️  IMPORTANT: Modifiez le fichier .env et changez EMBEDDING_API_SECRET!${NC}"
        echo -e "${YELLOW}   Vous pouvez générer un secret avec:${NC}"
        echo -e "${YELLOW}   python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"${NC}"
        exit 1
    else
        echo -e "${RED}❌ Le fichier .env.example n'existe pas!${NC}"
        echo -e "${YELLOW}📝 Création d'un fichier .env.example...${NC}"
        cat > .env.example << 'EOF'
# Configuration de l'API d'Embedding
# Copiez ce fichier vers .env et modifiez les valeurs selon vos besoins

# Secret pour l'authentification basique (obligatoire)
# Génère un secret fort avec: python3 -c "import secrets; print(secrets.token_urlsafe(32))"
EMBEDDING_API_SECRET=change-me-to-a-strong-secret-key-here

# Configuration du serveur
PORT=8001
HOST=0.0.0.0
EOF
        cp .env.example .env
        echo -e "${GREEN}✅ Fichier .env.example créé et .env copié${NC}"
        echo -e "${RED}⚠️  IMPORTANT: Modifiez le fichier .env et changez EMBEDDING_API_SECRET!${NC}"
        exit 1
    fi
fi

# Vérifier que le secret n'est pas la valeur par défaut
if grep -q "change-me-to-a-strong-secret-key-here" .env 2>/dev/null; then
    echo -e "${RED}❌ Le secret EMBEDDING_API_SECRET n'a pas été modifié!${NC}"
    echo -e "${YELLOW}   Modifiez le fichier .env et changez EMBEDDING_API_SECRET${NC}"
    echo -e "${YELLOW}   Vous pouvez générer un secret avec:${NC}"
    echo -e "${YELLOW}   python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"${NC}"
    exit 1
fi

# Lancer l'API
echo -e "${GREEN}🚀 Démarrage de l'API d'embedding...${NC}"
echo -e "${GREEN}📖 Documentation disponible sur: http://localhost:8001/docs${NC}"
python app.py

