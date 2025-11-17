"""
Django settings for savr_back project.
"""

from pathlib import Path
from decouple import config
from datetime import timedelta

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = config('SECRET_KEY', default='django-insecure-change-me-in-production')

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = config('DEBUG', default=True, cast=bool)

ALLOWED_HOSTS = ['*']


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
    'accounts',
    'recipes',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
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

STATIC_URL = 'static/'

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Custom User Model
AUTH_USER_MODEL = 'accounts.User'

# REST Framework settings
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.AllowAny',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
    # Éviter le coût du Browsable API par défaut
    'DEFAULT_RENDERER_CLASSES': (
        'rest_framework.renderers.JSONRenderer',
    ),
}

# JWT Settings
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(days=1),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
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

# Use S3 for media files if configured, otherwise use local storage
if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY and AWS_BUCKET:
    DEFAULT_FILE_STORAGE = 'storages.backends.s3boto3.S3Boto3Storage'
    STATICFILES_STORAGE = 'storages.backends.s3boto3.S3StaticStorage'
    AWS_S3_FILE_OVERWRITE = False
    AWS_S3_VERIFY = True


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

