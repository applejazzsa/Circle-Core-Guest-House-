import os
from pathlib import Path

from decouple import Csv, config

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config('SECRET_KEY', default='django-insecure-circle-core-guest-house-dev-key')
DEBUG = config('DEBUG', default=True, cast=bool)
ALLOWED_HOSTS = config(
    'ALLOWED_HOSTS',
    default='localhost,127.0.0.1,.localhost,.circlecore.co.za',
    cast=Csv(),
)

BASE_DOMAIN = config('BASE_DOMAIN', default='circlecore.co.za')

# ── PayFast ──
PAYFAST_MERCHANT_ID = config('PAYFAST_MERCHANT_ID', default='10000100')
PAYFAST_MERCHANT_KEY = config('PAYFAST_MERCHANT_KEY', default='46f0cd694581a')
PAYFAST_PASSPHRASE = config('PAYFAST_PASSPHRASE', default='')
PAYFAST_SANDBOX = config('PAYFAST_SANDBOX', default=True, cast=bool)

# ── Multi-tenancy (django-tenants) ──
# SHARED_APPS → public schema only (no tenant tables)
# TENANT_APPS → each tenant schema (auth, admin, core are fully isolated per tenant)
SHARED_APPS = [
    'django_tenants',
    'tenants',
    'django.contrib.contenttypes',
    'django.contrib.sessions',    # public schema needs sessions for CSRF on landing/register
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

TENANT_APPS = [
    'django.contrib.auth',        # fully isolated per tenant — no cross-tenant user access
    'django.contrib.admin',
    'core',
]

INSTALLED_APPS = list(SHARED_APPS) + [app for app in TENANT_APPS if app not in SHARED_APPS]

TENANT_MODEL = 'tenants.GuestHouseTenant'
TENANT_DOMAIN_MODEL = 'tenants.Domain'

MIDDLEWARE = [
    'django_tenants.middleware.main.TenantMainMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'core.middleware.SubscriptionMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'core.middleware.RoleAccessMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

# Tenant schemas use ROOT_URLCONF; public schema uses PUBLIC_SCHEMA_URLCONF
ROOT_URLCONF = 'config.urls'
PUBLIC_SCHEMA_URLCONF = 'config.urls_public'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'core.context_processors.guest_house_settings',
                'core.context_processors.subscription_context',
                'core.context_processors.inventory_context',
                'core.context_processors.maintenance_context',
                'core.context_processors.staff_role_context',
                'core.context_processors.active_property_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django_tenants.postgresql_backend',
        'NAME': config('DB_NAME', default='circlecore'),
        'USER': config('DB_USER', default='circlecore'),
        'PASSWORD': config('DB_PASSWORD', default=''),
        'HOST': config('DB_HOST', default='localhost'),
        'PORT': config('DB_PORT', default='5432'),
    }
}

DATABASE_ROUTERS = ('django_tenants.routers.TenantSyncRouter',)

# Cache — use Redis in production for rate limiting to work across Gunicorn workers
# Production: BACKEND = 'django.core.cache.backends.redis.RedisCache', LOCATION = redis://...
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Africa/Johannesburg'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = 'login'

AUTHENTICATION_BACKENDS = ['core.backends.CaseInsensitiveModelBackend']
LOGIN_REDIRECT_URL = 'core:home'
LOGOUT_REDIRECT_URL = 'login'

# ── Email ──
EMAIL_BACKEND = config('EMAIL_BACKEND', default='django.core.mail.backends.smtp.EmailBackend')
EMAIL_HOST = config('EMAIL_HOST', default='smtp.xneelo.co.za')
EMAIL_PORT = config('EMAIL_PORT', default=587, cast=int)
EMAIL_USE_TLS = config('EMAIL_USE_TLS', default=True, cast=bool)
EMAIL_USE_SSL = config('EMAIL_USE_SSL', default=False, cast=bool)
EMAIL_HOST_USER = config('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')
DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', default='Circle Core <hello@circlecore.co.za>')
CIRCLE_CORE_SALES_EMAIL = config('CIRCLE_CORE_SALES_EMAIL', default='sales@circlecore.co.za')
