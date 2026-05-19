from pathlib import Path
from dotenv import load_dotenv
import os
import warnings
import os
CHATBASE_SECRET = os.environ.get('CHATBASE_SECRET')
# Load environment variables FIRST
load_dotenv(override=True)

BASE_DIR = Path(__file__).resolve().parent.parent

DEBUG = os.getenv('DEBUG', 'True') == 'True'

_SECRET = os.getenv('SECRET_KEY')
if _SECRET:
    SECRET_KEY = _SECRET
elif DEBUG:
    SECRET_KEY = 'django-insecure-dev-only-change-me'
    warnings.warn(
        'SECRET_KEY is unset; using an insecure development default. Set SECRET_KEY in .env.',
        RuntimeWarning,
        stacklevel=1,
    )
else:
    raise ValueError(
        'SECRET_KEY must be set when DEBUG=False (e.g. in your production environment variables).'
    )

_allowed = [h.strip() for h in os.getenv('ALLOWED_HOSTS', '').split(',') if h.strip()]
if _allowed:
    ALLOWED_HOSTS = _allowed
elif DEBUG:
    ALLOWED_HOSTS = ['*']
else:
    raise ValueError(
        'ALLOWED_HOSTS must list at least one host when DEBUG=False (comma-separated ALLOWED_HOSTS).'
    )

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'accounts',
    'notifications',
    'cloudinary',
    'cloudinary_storage',
]

# Cloudinary config
import cloudinary
cloudinary.config(
    cloud_name=os.environ.get('CLOUDINARY_CLOUD_NAME'),
    api_key=os.environ.get('CLOUDINARY_API_KEY'),
    api_secret=os.environ.get('CLOUDINARY_API_SECRET'),
)

# I-replace ang DEFAULT_FILE_STORAGE
DEFAULT_FILE_STORAGE = 'cloudinary_storage.storage.MediaCloudinaryStorage'

LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/tenant/dashboard/'

ADMIN_LOGIN_ATTEMPT_LIMIT = int(os.getenv('ADMIN_LOGIN_ATTEMPT_LIMIT', '5'))
ADMIN_LOGIN_LOCKOUT_DURATION = int(os.getenv('ADMIN_LOGIN_LOCKOUT_DURATION', '900'))

DJANGO_SITE_ADMIN_PATH_PREFIXES = tuple(
    h.strip()
    for h in os.getenv('DJANGO_SITE_ADMIN_PATH_PREFIXES', '/django-admin').split(',')
    if h.strip()
)

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'accounts.middleware.PortalRBACMiddleware',
    'accounts.middleware.AdminLoginProtectionMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'accounts.middleware.SecurityHeadersMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.middleware.security.SecurityMiddleware',
]

# Configure WhiteNoise to serve media files
WHITENOISE_USE_FINDERS = False
if DEBUG:
    WHITENOISE_AUTOREFRESH = True
    # In development, let Django serve media files
    WHITENOISE_SKIP_REGEXES = [
        r'^/media/',
    ]

ROOT_URLCONF = 'rents_system.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'accounts/templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'accounts.context_processors.admin_profile',  # Legacy - kept for compatibility
                'accounts.context_processors.user_profile',  # Enhanced unified profile
                'accounts.context_processors.system_stats',   # System statistics
                'accounts.context_processors.recent_activity', # Recent activity feed
                'accounts.context_processors.recent_payments', # Recent payments feed
                'accounts.context_processors.notifications',   # Notification system
                'accounts.context_processors.app_settings',   # App metadata
                'accounts.context_processors.active_rules',
            ],
        },
    },
]

WSGI_APPLICATION = 'rents_system.wsgi.application'

import dj_database_url

USE_POSTGRES = os.getenv('USE_POSTGRES', 'false').strip().lower() == 'true'

if USE_POSTGRES and os.getenv('DATABASE_URL'):
    DATABASES = {
        'default': dj_database_url.config(default=os.getenv('DATABASE_URL'))
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql' if USE_POSTGRES else 'django.db.backends.mysql',
            'NAME': os.getenv('PG_DB_NAME', 'rents_db_pg') if USE_POSTGRES else os.getenv('DB_NAME', 'rents_db'),
            'USER': os.getenv('PG_USER', 'postgres') if USE_POSTGRES else os.getenv('DB_USER', 'root'),
            'PASSWORD': os.getenv('PG_PASSWORD', '') if USE_POSTGRES else os.getenv('DB_PASSWORD', ''),
            'HOST': os.getenv('PG_HOST', 'localhost') if USE_POSTGRES else os.getenv('DB_HOST', 'localhost'),
            'PORT': os.getenv('PG_PORT', '5432') if USE_POSTGRES else os.getenv('DB_PORT', '3306'),
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
STATICFILES_DIRS = [os.path.join(BASE_DIR, 'static')]
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Used by SendGrid HTTP API fallback in CustomPasswordResetForm (see accounts.views.auth_views)
SENDGRID_API_KEY = os.getenv('SENDGRID_API_KEY', '')

# Email Configuration for Password Reset
if os.getenv('EMAIL_BACKEND'):
    EMAIL_BACKEND = os.getenv('EMAIL_BACKEND')
    EMAIL_HOST = os.getenv('EMAIL_HOST')
    EMAIL_PORT = int(os.getenv('EMAIL_PORT', '587')) if os.getenv('EMAIL_PORT') else None
    EMAIL_USE_TLS = os.getenv('EMAIL_USE_TLS', 'True') == 'True'
    EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER')
    EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD')
    DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'RENTS System <noreply@rents.com>')
elif os.getenv('EMAIL_HOST_USER') and os.getenv('EMAIL_HOST_PASSWORD'):
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    EMAIL_HOST = os.getenv('EMAIL_HOST', 'smtp.gmail.com')
    EMAIL_PORT = int(os.getenv('EMAIL_PORT', '587'))
    EMAIL_USE_TLS = os.getenv('EMAIL_USE_TLS', 'True') == 'True'
    EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER')
    EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD')
    DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'RENTS System <noreply@rents.com>')
elif os.getenv('SENDGRID_API_KEY'):
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    EMAIL_HOST = 'smtp.sendgrid.net'
    EMAIL_PORT = 587
    EMAIL_USE_TLS = True
    EMAIL_HOST_USER = 'apikey'
    EMAIL_HOST_PASSWORD = os.getenv('SENDGRID_API_KEY')
    DEFAULT_FROM_EMAIL = os.getenv('FROM_EMAIL', 'RENTS System <noreply@rents.com>')
elif DEBUG:
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
    DEFAULT_FROM_EMAIL = 'RENTS System <noreply@rents.com>'
else:
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'
    DEFAULT_FROM_EMAIL = 'RENTS System <noreply@rents.com>'

# Password Reset Settings
PASSWORD_RESET_TIMEOUT = 86400  # 24 hours in seconds

# Site Configuration for Password Reset Links
SITE_ID = 1

# Public URL for outbound links (password reset emails). Prefer SITE_URL in any deployment.
def _canonical_base_url(raw: str, fallback: str) -> str:
    u = (raw or '').strip().rstrip('/')
    return u if u else fallback


_site_url_explicit = _canonical_base_url(os.getenv('SITE_URL', ''), '')
if _site_url_explicit:
    SITE_URL = _site_url_explicit
elif os.getenv('RENDER'):
    SITE_URL = _canonical_base_url(
        os.getenv('RENDER_EXTERNAL_URL', ''),
        'https://rents-system.onrender.com',
    )
else:
    SITE_URL = 'http://127.0.0.1:8000'

MEDIA_URL  = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# Dynamic Notifications System Settings
DYNAMIC_NOTIFICATION_AUTO_CREATE = True  # Create NotificationType on-the-fly if not found
ENABLE_WEBSOCKET_NOTIFICATIONS = False   # Phase 3 feature flag (disabled by default)
NOTIFICATION_EXPIRY_DAYS = 90             # Days before auto-deletion
NOTIFICATION_PAGE_SIZE = 20
NOTIFICATION_EMAIL_FROM = os.getenv('DEFAULT_FROM_EMAIL', 'noreply@rents.com')
