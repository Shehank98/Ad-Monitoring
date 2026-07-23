import environ
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(DEBUG=(bool, False))
environ.Env.read_env(BASE_DIR / '.env')

SECRET_KEY = env('SECRET_KEY', default='django-insecure-change-me-before-production')
DEBUG = env('DEBUG')
ALLOWED_HOSTS = env.list('ALLOWED_HOSTS', default=['*'])
CSRF_TRUSTED_ORIGINS = env.list('CSRF_TRUSTED_ORIGINS', default=[])

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'accounts',
    'core',
    'verification',
    'competitor',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'ad_monitor.urls'

TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [BASE_DIR / 'templates'],
    'APP_DIRS': True,
    'OPTIONS': {'context_processors': [
        'django.template.context_processors.debug',
        'django.template.context_processors.request',
        'django.contrib.auth.context_processors.auth',
        'django.contrib.messages.context_processors.messages',
        'core.context_processors.branding',
        'core.context_processors.site_notifications',
    ]},
}]

WSGI_APPLICATION = 'ad_monitor.wsgi.application'

DATABASES = {
    'default': env.db('DATABASE_URL', default=f'sqlite:///{BASE_DIR}/db.sqlite3')
}

AUTH_USER_MODEL = 'accounts.User'
LOGIN_URL = '/auth/login/'
LOGIN_REDIRECT_URL = '/dashboard/'
LOGOUT_REDIRECT_URL = '/auth/login/'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
     'OPTIONS': {'min_length': 8}},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Colombo'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']
MEDIA_URL = '/media/'
MEDIA_ROOT = env('MEDIA_ROOT', default=str(BASE_DIR / 'media'))

# ── Firebase Storage ──────────────────────────────────────────────────────────
# When FIREBASE_STORAGE_BUCKET is set, all FileField uploads are routed through
# Firebase Storage so they survive Railway redeploys.
# Set this to your Firebase project's default bucket, e.g.:
#   FIREBASE_STORAGE_BUCKET=your-project-id.appspot.com
FIREBASE_STORAGE_BUCKET = env('FIREBASE_STORAGE_BUCKET', default='')

# Django 5.x STORAGES dict (replaces deprecated DEFAULT_FILE_STORAGE /
# STATICFILES_STORAGE settings). WhiteNoise handles static files; Firebase
# handles uploaded media when the bucket env var is configured.
STORAGES = {
    'default': {
        'BACKEND': (
            'core.firebase_storage.FirebaseStorage'
            if FIREBASE_STORAGE_BUCKET
            else 'django.core.files.storage.FileSystemStorage'
        ),
    },
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# App-specific
ALLOWED_EMAIL_DOMAIN = env('ALLOWED_EMAIL_DOMAIN', default='')
SUPER_ADMIN_EMAILS    = env.list('SUPER_ADMIN_EMAILS', default=[])

# ── Gemini AI TC conversion ───────────────────────────────────────────────────
# When GEMINI_API_KEY is set, the TC PDF Converter parses PDFs with the Gemini
# API (vision) instead of the layout heuristic. Empty = heuristic parsing only.
GEMINI_API_KEY  = env('GEMINI_API_KEY', default='')
GEMINI_TC_MODEL = env('GEMINI_TC_MODEL', default='gemini-2.5-flash')

# 50 MB upload limit
DATA_UPLOAD_MAX_MEMORY_SIZE = 52_428_800
FILE_UPLOAD_MAX_MEMORY_SIZE = 52_428_800

from django.contrib.messages import constants as messages
MESSAGE_TAGS = {
    messages.DEBUG:   'debug',
    messages.INFO:    'info',
    messages.SUCCESS: 'success',
    messages.WARNING: 'warning',
    messages.ERROR:   'error',
}
