#!/bin/sh
set -eu

if [ -n "${DB_HOST:-}" ]; then
  echo "Waiting for PostgreSQL at ${DB_HOST}:${DB_PORT:-5432}..."
  until nc -z "$DB_HOST" "${DB_PORT:-5432}"; do
    sleep 1
  done
fi

if [ "${SKIP_DATABASE_SETUP:-0}" = "1" ]; then
  echo "Skipping database migrations and platform setup."
else
  python manage.py migrate_schemas --noinput --settings="${DJANGO_SETTINGS_MODULE:-config.settings_production}"
fi

python manage.py collectstatic --noinput --settings="${DJANGO_SETTINGS_MODULE:-config.settings_production}"
python manage.py verify_static_assets --settings="${DJANGO_SETTINGS_MODULE:-config.settings_production}"

if [ "${SKIP_DATABASE_SETUP:-0}" != "1" ]; then
  python manage.py setup_platform --settings="${DJANGO_SETTINGS_MODULE:-config.settings_production}"
fi

exec "$@"
