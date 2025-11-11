# Savr Backend - Django REST Framework

Backend API pour l'application Savr.

## Installation

1. Créer un environnement virtuel :
```bash
python -m venv venv
source venv/bin/activate  # Sur Windows: venv\Scripts\activate
```

2. Installer les dépendances :
```bash
pip install -r requirements.txt
```

3. Créer un fichier `.env` à la racine :
```
SECRET_KEY=your-secret-key-here
DEBUG=True
```

4. Appliquer les migrations :
```bash
python manage.py makemigrations
python manage.py migrate
```

5. Créer un superutilisateur (optionnel) :
```bash
python manage.py createsuperuser
```

## Démarrage

```bash
python manage.py runserver
```

L'API sera disponible sur `http://localhost:8000/api/`

## Endpoints

- `POST /api/auth/register/` - Inscription
- `POST /api/auth/login/` - Connexion
- `GET /api/auth/profile/` - Profil utilisateur (authentifié)
- `POST /api/auth/token/refresh/` - Rafraîchir le token JWT

## Structure

- `accounts/` - Application d'authentification et gestion des utilisateurs
- `savr_back/` - Configuration principale du projet

