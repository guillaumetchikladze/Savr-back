"""
Settings de test : rapides, déterministes, sans aucun appel réseau.

- Les secrets / clés d'API externes sont forcés à vide AVANT l'import des settings
  de base (python-decouple lit os.environ en priorité sur .env), donc les services
  (embeddings, Gemini, Apify, MS Graph, Expo, S3) prennent leur chemin « non configuré ».
- Le conftest racine ajoute en plus un garde-fou socket : toute connexion sortante
  hors base de test échoue immédiatement.
"""

import os

_FORCED_EMPTY = (
    'SENTRY_DSN',
    'AI_API_KEY',
    'GEMINI_API_KEY',
    'GOOGLE_API_KEY',
    'OPENAI_API_KEY',
    'EMBEDDING_API_SECRET',
    'APIFY_API_TOKEN',
    'JINA_API_TOKEN',
    'AWS_ACCESS_KEY_ID',
    'AWS_SECRET_ACCESS_KEY',
    'AWS_BUCKET',
    'AWS_ENDPOINT',
    'MS_GRAPH_TENANT_ID',
    'MS_GRAPH_CLIENT_ID',
    'MS_GRAPH_CLIENT_SECRET',
    'MS_GRAPH_SENDER',
    'API_URL',
    'PUBLIC_BASE_URL',
    'DOMAIN',
)
for _key in _FORCED_EMPTY:
    os.environ[_key] = ''

os.environ.setdefault('SECRET_KEY', 'test-secret-key-not-for-production-0123456789')
os.environ['DEBUG'] = 'False'
os.environ['AI_PAYWALL_ENABLED'] = 'False'
os.environ['SEARCH_CONTEXT_GEMINI_ENABLED'] = 'False'
os.environ['BATTLE_PLAN_LLM_ENABLED'] = 'False'
os.environ['CHAT_TITLE_LLM_ENABLED'] = 'False'
# Clé Fernet factice (cartes de fidélité chiffrées).
os.environ.setdefault('LOYALTY_CARD_SECRET_KEY', 'dGVzdC1rZXktdGVzdC1rZXktdGVzdC1rZXktMDEyMzQ=')

# Base de test : variables TEST_DB_* (Makefile / CI). Défauts = conteneur `make test-db`.
# Les DB_* du .env sont volontairement ignorées : jamais de tests contre la base de dev.
_TEST_DB_DEFAULTS = {
    'NAME': 'savr_test',
    'USER': 'postgres',
    'PASSWORD': 'postgres',
    'HOST': 'localhost',
    'PORT': '55432',
}
for _name, _default in _TEST_DB_DEFAULTS.items():
    os.environ[f'DB_{_name}'] = os.environ.get(f'TEST_DB_{_name}', _default)

from .settings import *  # noqa: E402,F403

DEBUG = False

PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']

CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'

CHANNEL_LAYERS = {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}}

# Celery : broker en mémoire, jamais exécuté implicitement. Les tests qui veulent
# exécuter une tâche l'appellent explicitement (task.run(...) / task.apply(...)).
CELERY_BROKER_URL = 'memory://'
CELERY_RESULT_BACKEND = 'cache+memory://'
CELERY_TASK_ALWAYS_EAGER = False
CELERY_TASK_STORE_EAGER_RESULT = False

WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = True

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'null': {'class': 'logging.NullHandler'}},
    'root': {'handlers': ['null'], 'level': 'CRITICAL'},
}
