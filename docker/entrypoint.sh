#!/bin/sh
set -eu

if [ -n "${DB_HOST:-}" ]; then
  echo "Waiting for PostgreSQL at ${DB_HOST}:${DB_PORT:-5432}..."
  until nc -z "$DB_HOST" "${DB_PORT:-5432}"; do
    sleep 1
  done
fi

python manage.py migrate_schemas --noinput --settings="${DJANGO_SETTINGS_MODULE:-config.settings_production}"
python manage.py collectstatic --noinput --settings="${DJANGO_SETTINGS_MODULE:-config.settings_production}"
python manage.py verify_static_assets --settings="${DJANGO_SETTINGS_MODULE:-config.settings_production}"
python manage.py setup_platform --settings="${DJANGO_SETTINGS_MODULE:-config.settings_production}"

exec "$@"
