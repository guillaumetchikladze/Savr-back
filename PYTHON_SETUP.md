# Guide de mise à jour Python pour Savr Backend

## Vérifier la version actuelle

```bash
python3 --version
```

## Mettre à jour Python avec pyenv

Vous utilisez `pyenv` pour gérer Python. Voici comment mettre à jour :

### 1. Voir les versions disponibles

```bash
# Voir toutes les versions disponibles
pyenv install --list | grep "  3\."

# Voir les dernières versions Python 3.12
pyenv install --list | grep "  3.12"
```

### 2. Installer une nouvelle version

```bash
# Installer Python 3.12.x (dernière version recommandée)
pyenv install 3.12.7

# Ou Python 3.13.x (dernière version)
pyenv install 3.13.1
```

### 3. Définir la version globale ou locale

```bash
# Pour ce projet uniquement (recommandé)
cd Savr-back
pyenv local 3.12.7

# Ou globalement pour tous les projets
pyenv global 3.12.7
```

### 4. Vérifier la version

```bash
python3 --version
which python3
```

### 5. Recréer l'environnement virtuel

```bash
# Supprimer l'ancien venv
rm -rf venv

# Créer un nouveau venv avec la nouvelle version
python3 -m venv venv

# Activer
source venv/bin/activate

# Installer les dépendances
pip install --upgrade pip
pip install -r requirements.txt
```

## Alternative : Homebrew

Si vous préférez utiliser Homebrew :

```bash
# Installer Python via Homebrew
brew install python@3.12

# Ou la dernière version
brew install python@3.13

# Vérifier
python3.12 --version
```

## Recommandation

Pour Django 5.2.8, Python 3.11+ est requis. Vous avez Python 3.11.9, ce qui est parfait !

Si vous voulez la dernière version :
- Python 3.12.7 (stable et recommandé)
- Python 3.13.1 (dernière version)

## Note importante

Le problème que vous avez rencontré n'était pas lié à la version de Python, mais à la version de `djangorestframework-simplejwt` qui n'existait pas (5.3.2). J'ai corrigé cela en mettant 5.5.1.


