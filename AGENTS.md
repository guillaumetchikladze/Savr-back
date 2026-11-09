# Savr-back — Mémo rapide (pour humains & Cursor)

## Backend (Django / DRF)

### Venv obligatoire

```bash
cd "/path/to/Savr-back"
source venv/bin/activate
```

### Installation

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
```

### Démarrage

```bash
python manage.py runserver
```

### Chat agent (WebSocket obligatoire)

Le chat mobile utilise `ws/chat/`. Avec Channels installé, `runserver` suffit en dev.
Pour un serveur dédié ASGI :

```bash
daphne -b 0.0.0.0 -p 8000 savr_back.asgi:application
```

Redis doit tourner (channel layer + locks chat). Celery optionnel pour les titres auto.

### Accès depuis un téléphone (Expo Go)

```bash
python manage.py runserver 0.0.0.0:8000
```

Le front doit utiliser `API_BASE_URL=http://<IP_LOCALE>:8000/api` (même IP pour REST et WS).

## Lint & tests

Toute installation de dépendances passe par `sfw` (Socket Firewall) :

```bash
uv venv .venv --python 3.12 && sfw uv pip install --python .venv/bin/python -r requirements-dev.txt
```

Lint / format (ruff, config dans `pyproject.toml`, migrations exclues) :

```bash
make lint        # ruff check + ruff format --check
make fmt         # autofix + format
```

Tests (pytest + pytest-django + xdist, settings `savr_back.settings_test`) — nécessitent un Postgres avec pgvector :

```bash
make test-db                  # Postgres+pgvector éphémère (docker-compose.test.yml, port 55432, tmpfs)
make test                     # suite rapide (exclut @slow), --reuse-db ; N=auto → parallèle (xdist)
make test-all                 # tout, y compris @slow
make test-domain d=shopping   # auth | mealplan | posts | shopping | dietary | chat | imports | recipes
make test-changed             # tests liés aux fichiers modifiés (vs BASE=origin/main)
make test-fresh               # recrée les bases de test (si une migration existante a été modifiée)
```

Règles de la suite :
- aucun appel réseau : `conftest.py` bloque toute connexion hors base de test (le test échoue s'il tente un accès) ; les clés externes (Gemini, embeddings, Apify, S3, MS Graph) sont vidées par `settings_test`. Mocker le LLM / scraper au niveau du module appelant (`recipes.tasks.formalize_recipe`, …).
- Celery n'exécute rien implicitement : appeler la tâche directement (`process_recipe_import_from_url(str(id))`) ou capturer les `.delay()` avec la fixture `celery_delay`.
- fixtures partagées (`conftest.py`) : `make_user` (validé par défaut), `user`, `api.as_user(u)`, `make_recipe`, `make_meal_plan`, `make_shopping_list`, `follow`.
- un fichier par domaine (`test_<domaine>_*.py`) → marqueur de domaine appliqué automatiquement.
- tests API orientés règles métier et permissions, pas de CRUD trivial.
