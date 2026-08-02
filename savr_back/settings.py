"""
Django settings for savr_back project.
"""

from pathlib import Path
from decouple import config
from datetime import timedelta
import sys
import boto3

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = config('SECRET_KEY', default='django-insecure-change-me-in-production')

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = config('DEBUG', default=True, cast=bool)

# Sentry Configuration
import sentry_sdk
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.celery import CeleryIntegration

SENTRY_DSN = config('SENTRY_DSN', default='')

if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[
            DjangoIntegration(
                transaction_style='url',
                middleware_spans=True,
                signals_spans=True,
                cache_spans=True,
            ),
            CeleryIntegration(),
        ],
        # Set traces_sample_rate to 1.0 to capture 100%
        # of the transactions for performance monitoring.
        # We recommend adjusting this value in production.
        traces_sample_rate=0.1 if DEBUG else 0.05,
        # If you wish to associate users to errors (assuming you are using
        # django.contrib.auth) you may enable sending PII data.
        send_default_pii=True,
        # Set profiles_sample_rate to 1.0 to profile 100%
        # of sampled transactions.
        # We recommend adjusting this value in production.
        profiles_sample_rate=0.1 if DEBUG else 0.05,
        environment='development' if DEBUG else 'production',
    )

ALLOWED_HOSTS = ['*']

# Derrière Caddy (HTTPS terminé au proxy) : sans ça Django voit du HTTP et le CSRF admin échoue.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True

def _build_csrf_trusted_origins():
    """Origines autorisées pour POST admin / formulaires (scheme obligatoire)."""
    raw = config('CSRF_TRUSTED_ORIGINS', default='').strip()
    origins = [o.strip().rstrip('/') for o in raw.split(',') if o.strip()] if raw else []

    domain = config('DOMAIN', default='').strip().rstrip('/')
    if domain:
        if '://' in domain:
            origins.insert(0, domain)
        else:
            origins.insert(0, f'https://{domain}')
            # Caddy local sans TLS
            origins.append(f'http://{domain}')

    # Déduplique en gardant l'ordre
    seen = set()
    deduped = []
    for origin in origins:
        if origin not in seen:
            seen.add(origin)
            deduped.append(origin)

    if not deduped:
        deduped = [
            'https://api.cookoo.tchikladze.fr',
            'https://tchikook.fr',
            'https://www.tchikook.fr',
        ]
    return deduped


# Requis dès Django 4+ pour le POST admin derrière HTTPS (l'API JWT n'est pas impactée).
CSRF_TRUSTED_ORIGINS = _build_csrf_trusted_origins()

if not DEBUG:
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_SECURE = True

# Filtrage recettes / préférences alimentaires (recipes.dietary_filters)
# Distance cosinus pgvector : 0 = identique, 2 = opposé. Plus la valeur est basse, plus c’est strict.
DIETARY_SEMANTIC_MATCHING = config('DIETARY_SEMANTIC_MATCHING', default=True, cast=bool)
DIETARY_SEMANTIC_MAX_DISTANCE = config('DIETARY_SEMANTIC_MAX_DISTANCE', default=0.42, cast=float)
DIETARY_SEMANTIC_MAX_INGREDIENTS_PER_LABEL = config(
    'DIETARY_SEMANTIC_MAX_INGREDIENTS_PER_LABEL', default=120, cast=int
)

# Recherche recettes sémantique (nomic 512d + hybride pg_trgm/pgvector)
EMBEDDING_DIMENSION = config('EMBEDDING_DIMENSION', default=512, cast=int)
SEARCH_SEMANTIC_MAX_DISTANCE = config('SEARCH_SEMANTIC_MAX_DISTANCE', default=0.45, cast=float)
SEARCH_HYBRID_WEIGHT_SEMANTIC = config('SEARCH_HYBRID_WEIGHT_SEMANTIC', default=0.65, cast=float)
SEARCH_HYBRID_WEIGHT_TRIGRAM = config('SEARCH_HYBRID_WEIGHT_TRIGRAM', default=0.35, cast=float)
# word_similarity : typo « healty » → « healthy » dans les tags (~0.5+)
SEARCH_TRIGRAM_MIN_SCORE = config('SEARCH_TRIGRAM_MIN_SCORE', default=0.08, cast=float)
SEARCH_HYBRID_MIN_SCORE = config('SEARCH_HYBRID_MIN_SCORE', default=0.18, cast=float)
SEARCH_CONTEXT_GEMINI_ENABLED = config('SEARCH_CONTEXT_GEMINI_ENABLED', default=True, cast=bool)


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'rest_framework_simplejwt',
    'corsheaders',
    'channels',
    'accounts',
    'recipes',
    'emails',
    'chat',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'savr_back.middleware.TimingMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'savr_back.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'savr_back.wsgi.application'
ASGI_APPLICATION = 'savr_back.asgi.application'


# Database
# https://docs.djangoproject.com/en/5.2/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': config('DB_NAME', default='savr_db'),
        'USER': config('DB_USER', default='postgres'),
        'PASSWORD': config('DB_PASSWORD', default=''),
        'HOST': config('DB_HOST', default='localhost'),
        'PORT': config('DB_PORT', default='5432'),
    }
}


# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = 'fr-fr'

TIME_ZONE = 'Europe/Paris'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
# En DEBUG, WhiteNoise peut servir depuis les apps sans collectstatic.
WHITENOISE_USE_FINDERS = DEBUG

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Custom User Model
AUTH_USER_MODEL = 'accounts.User'

# Paywall / entitlements — when True, AI features require plan=premium
AI_PAYWALL_ENABLED = config('AI_PAYWALL_ENABLED', default=False, cast=bool)

# Billing architecture:
# - contact_email : mailto support (défaut — safe App Store / en attendant IAP ou web)
# - web_handoff   : token app → tchikook.fr (Stancer plus tard)
# - iap           : App Store / Play Billing (rebuild + lib native)
BILLING_CHECKOUT_MODE = config('BILLING_CHECKOUT_MODE', default='contact_email')
BILLING_SUPPORT_EMAIL = config('BILLING_SUPPORT_EMAIL', default='contact@tchikook.fr')
BILLING_WEB_CHECKOUT_BASE_URL = config(
    'BILLING_WEB_CHECKOUT_BASE_URL',
    default='https://tchikook.fr/licence',
)
BILLING_HANDOFF_TTL_SECONDS = config('BILLING_HANDOFF_TTL_SECONDS', default=600, cast=int)
# Prix placeholders (à affiner) — mensuel / annuel
BILLING_MONTHLY_PRICE_CENTS = config('BILLING_MONTHLY_PRICE_CENTS', default=499, cast=int)
BILLING_MONTHLY_PRICE_LABEL = config('BILLING_MONTHLY_PRICE_LABEL', default='4,99 €')
BILLING_YEARLY_PRICE_CENTS = config('BILLING_YEARLY_PRICE_CENTS', default=3999, cast=int)
BILLING_YEARLY_PRICE_LABEL = config('BILLING_YEARLY_PRICE_LABEL', default='39,99 €')
BILLING_YEARLY_MONTHLY_EQ_LABEL = config('BILLING_YEARLY_MONTHLY_EQ_LABEL', default='3,33 €')
BILLING_YEARLY_SAVINGS_LABEL = config('BILLING_YEARLY_SAVINGS_LABEL', default='~4 mois offerts')
BILLING_YEARLY_SAVINGS_AMOUNT_LABEL = config('BILLING_YEARLY_SAVINGS_AMOUNT_LABEL', default='~20 €')
BILLING_YEARLY_WEEKLY_EQ_LABEL = config('BILLING_YEARLY_WEEKLY_EQ_LABEL', default='0,77 €')
BILLING_YEARLY_LIST_PRICE_LABEL = config('BILLING_YEARLY_LIST_PRICE_LABEL', default='59,99 €')
BILLING_LICENSE_CURRENCY = config('BILLING_LICENSE_CURRENCY', default='eur')
# Compat anciens settings one-shot (fallback)
BILLING_LICENSE_PRICE_CENTS = config('BILLING_LICENSE_PRICE_CENTS', default=3999, cast=int)
BILLING_LICENSE_PRICE_LABEL = config('BILLING_LICENSE_PRICE_LABEL', default='39,99 €')

# REST Framework settings
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.AllowAny',
    ],
    'DEFAULT_PAGINATION_CLASS': 'recipes.pagination.CustomPageNumberPagination',
    'PAGE_SIZE': 20,
    # Éviter le coût du Browsable API par défaut
    'DEFAULT_RENDERER_CLASSES': (
        'rest_framework.renderers.JSONRenderer',
    ),
}

# JWT Settings
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(days=1),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=365),  # Refresh token valide 1 an (quasi-infini)
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'ALGORITHM': 'HS256',
    'SIGNING_KEY': SECRET_KEY,
    'AUTH_HEADER_TYPES': ('Bearer',),
}

# CORS Settings
# En développement, autoriser toutes les origines pour Expo Go
if DEBUG:
    CORS_ALLOW_ALL_ORIGINS = True
else:
    CORS_ALLOWED_ORIGINS = [
        "http://localhost:19006",
        "http://127.0.0.1:19006",
        "http://localhost:8081",
        "http://127.0.0.1:8081",
    ]

CORS_ALLOW_CREDENTIALS = True

# AWS S3 Configuration for file storage
AWS_ACCESS_KEY_ID = config('AWS_ACCESS_KEY_ID', default='')
AWS_SECRET_ACCESS_KEY = config('AWS_SECRET_ACCESS_KEY', default='')
AWS_BUCKET = config('AWS_BUCKET', default='')
AWS_STORAGE_BUCKET_NAME = AWS_BUCKET  # Alias pour compatibilité
AWS_S3_REGION_NAME = config('AWS_S3_REGION_NAME', default='eu-west-3')
AWS_ENDPOINT = config('AWS_ENDPOINT', default='')
AWS_USE_PATH_STYLE_ENDPOINT = config('AWS_USE_PATH_STYLE_ENDPOINT', default='false', cast=bool)

# Construire le custom domain
if AWS_ENDPOINT:
    # Pour MinIO ou endpoint personnalisé
    if AWS_USE_PATH_STYLE_ENDPOINT:
        AWS_S3_CUSTOM_DOMAIN = AWS_ENDPOINT.replace('http://', '').replace('https://', '')
    else:
        AWS_S3_CUSTOM_DOMAIN = f'{AWS_BUCKET}.{AWS_ENDPOINT.replace("http://", "").replace("https://", "")}'
else:
    AWS_S3_CUSTOM_DOMAIN = f'{AWS_BUCKET}.s3.{AWS_S3_REGION_NAME}.amazonaws.com'

AWS_S3_OBJECT_PARAMETERS = {
    'CacheControl': 'max-age=86400',
}
AWS_DEFAULT_ACL = 'public-read'
AWS_QUERYSTRING_AUTH = False

# Media files → S3/MinIO. Static admin → WhiteNoise (le navigateur ne peut pas
# charger le CSS depuis minio:9000 / une IP LAN privée).
if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY and AWS_BUCKET:
    DEFAULT_FILE_STORAGE = 'storages.backends.s3boto3.S3Boto3Storage'
    AWS_S3_FILE_OVERWRITE = False
    AWS_S3_VERIFY = True

STATICFILES_STORAGE = 'whitenoise.storage.CompressedStaticFilesStorage'

# Celery configuration
CELERY_BROKER_URL = config('CELERY_BROKER_URL', default='redis://localhost:6379/0')
CELERY_RESULT_BACKEND = config('CELERY_RESULT_BACKEND', default='redis://localhost:6379/0')
CELERY_TASK_DEFAULT_QUEUE = 'savr_default'
CELERY_TASK_DEFAULT_EXCHANGE = 'savr'
CELERY_TASK_DEFAULT_ROUTING_KEY = 'savr.default'
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 60 * 10  # 10 minutes
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
# macOS: prefork + libs Objective-C (Sentry, httpx, SDK OpenAI…) → SIGABRT au fork().
# En local sur Darwin, solo par défaut ; prefork reste le défaut ailleurs (Docker/Linux).
_default_celery_pool = 'solo' if sys.platform == 'darwin' else 'prefork'
CELERY_WORKER_POOL = config('CELERY_WORKER_POOL', default=_default_celery_pool)
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

# Email queues (priority routing)
# Rappel push « photo à table » après complete_cooking (secondes). Ex. 10800 = 3 h ; 90 pour tests.
MEAL_TIME_PHOTO_REMINDER_DELAY_SECONDS = config(
    'MEAL_TIME_PHOTO_REMINDER_DELAY_SECONDS',
    default=10800,
    cast=int,
)

CELERY_TASK_QUEUES = (
    # default
    __import__("kombu").Queue("savr_default", routing_key="savr.default"),
    # emails
    __import__("kombu").Queue("emails_urgent", routing_key="emails.urgent"),
    __import__("kombu").Queue("emails_high", routing_key="emails.high"),
    __import__("kombu").Queue("emails_normal", routing_key="emails.normal"),
    __import__("kombu").Queue("emails_low", routing_key="emails.low"),
)

# Microsoft Graph (app-only)
MS_GRAPH_TENANT_ID = config('MS_GRAPH_TENANT_ID', default='')
MS_GRAPH_CLIENT_ID = config('MS_GRAPH_CLIENT_ID', default='')
MS_GRAPH_CLIENT_SECRET = config('MS_GRAPH_CLIENT_SECRET', default='')
MS_GRAPH_SENDER = config('MS_GRAPH_SENDER', default='')
EMAIL_FROM_ADDRESS = config('EMAIL_FROM_ADDRESS', default='noreply@tchikook.fr')

# Public URL (used to build password reset links).
# In local dev you may have API_URL=http://host:8000/api -> public base is http://host:8000
API_URL = config('API_URL', default='')
if API_URL and API_URL.rstrip('/').endswith('/api'):
    PUBLIC_BASE_URL = API_URL.rstrip('/')[:-4]
else:
    PUBLIC_BASE_URL = config('PUBLIC_BASE_URL', default='')

# Channels configuration (WebSocket)
CHANNEL_REDIS_URL = config('CHANNEL_REDIS_URL', default='redis://localhost:6379/1')
CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels_redis.core.RedisChannelLayer',
        'CONFIG': {
            'hosts': [CHANNEL_REDIS_URL],
        },
    },
}


def build_s3_url(image_path):
    """
    Construire l'URL complète d'une image à partir de son chemin relatif dans S3.
    
    Args:
        image_path: Chemin relatif (ex: 'meal_plans/70/6096a520a71247229f1cae315fc2bd84.jpg')
    
    Returns:
        URL complète de l'image
    """
    if not image_path:
        return None
    
    # Nettoyer le chemin (enlever le préfixe s3:/ si présent)
    clean_path = image_path.replace('s3:/', '').lstrip('/')
    
    if AWS_ENDPOINT and AWS_USE_PATH_STYLE_ENDPOINT:
        # Format path-style: http://localhost:9000/bucket/key
        protocol = 'https' if AWS_ENDPOINT.startswith('https://') else 'http'
        endpoint_host = AWS_ENDPOINT.replace('http://', '').replace('https://', '')
        return f"{protocol}://{endpoint_host}/{AWS_BUCKET}/{clean_path}"
    elif AWS_ENDPOINT:
        # Format virtual-hosted avec endpoint personnalisé
        return f"{AWS_ENDPOINT}/{AWS_BUCKET}/{clean_path}"
    elif AWS_S3_CUSTOM_DOMAIN:
        # Format avec custom domain
        protocol = 'https' if not AWS_ENDPOINT or AWS_ENDPOINT.startswith('https://') else 'http'
        return f"{protocol}://{AWS_S3_CUSTOM_DOMAIN}/{clean_path}"
    else:
        # Format AWS standard
        return f"https://{AWS_BUCKET}.s3.{AWS_S3_REGION_NAME}.amazonaws.com/{clean_path}"


_S3_CLIENT = None


def build_s3_client():
    """Créer un client S3/MinIO partagé."""
    global _S3_CLIENT
    if _S3_CLIENT is not None:
        return _S3_CLIENT

    config_kwargs = {
        'aws_access_key_id': AWS_ACCESS_KEY_ID,
        'aws_secret_access_key': AWS_SECRET_ACCESS_KEY,
        'region_name': AWS_S3_REGION_NAME,
    }
    if AWS_ENDPOINT:
        config_kwargs['endpoint_url'] = AWS_ENDPOINT
        if AWS_ENDPOINT.startswith('http://'):
            config_kwargs['use_ssl'] = False
    _S3_CLIENT = boto3.client('s3', **config_kwargs)
    return _S3_CLIENT


_PRESIGNED_URL_CACHE = {}


def build_presigned_get_url(image_path, expires_in=3600):
    """Générer une URL pré-signée pour télécharger une image (cache en mémoire)."""
    if not image_path:
        return None

    clean_path = image_path.replace('s3:/', '').lstrip('/')

    if not AWS_BUCKET or not AWS_ACCESS_KEY_ID or not AWS_SECRET_ACCESS_KEY:
        return build_s3_url(clean_path)

    import time
    cache_key = (clean_path, expires_in)
    now = time.time()
    cached = _PRESIGNED_URL_CACHE.get(cache_key)
    if cached and cached[1] > now:
        return cached[0]

    try:
        client = build_s3_client()
        url = client.generate_presigned_url(
            'get_object',
            Params={'Bucket': AWS_BUCKET, 'Key': clean_path},
            ExpiresIn=expires_in,
        )
        # Rafraîchir 5 min avant expiration
        _PRESIGNED_URL_CACHE[cache_key] = (url, now + max(expires_in - 300, 60))
        return url
    except Exception:
        return build_s3_url(clean_path)

