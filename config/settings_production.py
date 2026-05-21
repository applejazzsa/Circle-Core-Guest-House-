from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from decouple import Csv, config

from .settings import *  # noqa: F403


BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config("SECRET_KEY")
DEBUG = False
ALLOWED_HOSTS = config("ALLOWED_HOSTS", cast=Csv())


def database_from_url(database_url):
    parsed = urlparse(database_url)

    if parsed.scheme in ("sqlite", "sqlite3"):
        name = unquote(parsed.path.lstrip("/")) or "db.sqlite3"
        return {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / name,
        }

    if parsed.scheme in ("postgres", "postgresql"):
        options = parse_qs(parsed.query)
        return {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": unquote(parsed.path.lstrip("/")),
            "USER": unquote(parsed.username or ""),
            "PASSWORD": unquote(parsed.password or ""),
            "HOST": parsed.hostname or "",
            "PORT": str(parsed.port or ""),
            "OPTIONS": {key: values[-1] for key, values in options.items()},
        }

    raise ValueError("Unsupported DATABASE_URL scheme.")


DATABASES = {
    "default": database_from_url(config("DATABASE_URL", default="sqlite:///db.sqlite3")),
}

STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_ROOT = BASE_DIR / "media"

SECURE_BROWSER_XSS_FILTER = True
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
