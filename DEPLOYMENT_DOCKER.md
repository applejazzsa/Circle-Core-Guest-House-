# Circle Core Guest House - Docker Production Deployment

Target domain: `guesthouse.circlecore.co.za`

This guide assumes:

- Ubuntu 24.04 VPS
- Docker and Docker Compose installed
- Existing Nginx reverse proxy
- Let's Encrypt SSL already configured
- Xneelo DNS points `guesthouse.circlecore.co.za` and tenant subdomains to the VPS

## Required `.env`

Create `.env` on the server from `.env.example` and set production values:

```env
SECRET_KEY=<strong-random-secret>
DEBUG=False
ALLOWED_HOSTS=guesthouse.circlecore.co.za,.guesthouse.circlecore.co.za
BASE_DOMAIN=guesthouse.circlecore.co.za

DB_NAME=circlecore
DB_USER=circlecore
DB_PASSWORD=<strong-db-password>
DB_HOST=db
DB_PORT=5432

EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=<xneelo-smtp-host>
EMAIL_PORT=465
EMAIL_USE_TLS=False
EMAIL_USE_SSL=True
EMAIL_HOST_USER=<mailbox>
EMAIL_HOST_PASSWORD=<mailbox-password>
DEFAULT_FROM_EMAIL=Circle Core <support@circlecore.co.za>
CIRCLE_CORE_SALES_EMAIL=support@circlecore.co.za

PAYFAST_MERCHANT_ID=<live-merchant-id>
PAYFAST_MERCHANT_KEY=<live-merchant-key>
PAYFAST_PASSPHRASE=<payfast-passphrase>
PAYFAST_SANDBOX=False

REDIS_URL=redis://redis:6379/1
LOG_LEVEL=INFO
```

If tenants should be `tenant.circlecore.co.za` instead of `tenant.guesthouse.circlecore.co.za`, set:

```env
ALLOWED_HOSTS=circlecore.co.za,.circlecore.co.za
BASE_DOMAIN=circlecore.co.za
```

## Build And Start

```bash
docker compose build
docker compose up -d
docker compose logs -f web
```

The web container runs:

- `migrate_schemas --noinput`
- `collectstatic --noinput`
- Gunicorn on `127.0.0.1:8000`

## Nginx Reverse Proxy

Point Nginx to the local Docker-bound port:

```nginx
server {
    listen 80;
    server_name guesthouse.circlecore.co.za *.guesthouse.circlecore.co.za;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name guesthouse.circlecore.co.za *.guesthouse.circlecore.co.za;

    ssl_certificate /etc/letsencrypt/live/guesthouse.circlecore.co.za/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/guesthouse.circlecore.co.za/privkey.pem;

    client_max_body_size 10M;

    location /media/cleaning_proofs/ {
        return 403;
    }

    location /media/ {
        alias /var/lib/docker/volumes/circle-core-guest-house_media/_data/;
        expires 7d;
        add_header Cache-Control "private";
    }

    location /static/ {
        alias /var/lib/docker/volumes/circle-core-guest-house_staticfiles/_data/;
        expires 30d;
        add_header Cache-Control "public";
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 90s;
    }
}
```

Adjust Docker volume paths if your Compose project name differs.

## Create Platform Admin

```bash
docker compose exec web python manage.py createsuperuser --settings=config.settings_production
```

## Verify

```bash
docker compose exec web python manage.py check --deploy --settings=config.settings_production
docker compose exec web python manage.py showmigrations --settings=config.settings_production
docker compose exec web python manage.py test_email support@circlecore.co.za --settings=config.settings_production
```

Then verify in the browser:

- `https://guesthouse.circlecore.co.za/`
- `https://guesthouse.circlecore.co.za/healthz/`
- Tenant registration
- Tenant login
- Password reset email
- Booking creation
- Payment capture
- Invoice/receipt PDF
- Cleaning proof upload and protected proof review

## Backups

Run daily from cron or host scheduler:

```bash
docker compose exec -T web python manage.py backup_local --output /app/backups --settings=config.settings_production
```

Copy `/app/backups` off-server daily.

## Restore Drill

Test restore monthly on a staging VPS:

```bash
docker compose down
docker volume rm circle-core-guest-house_postgres_data
docker compose up -d db redis
docker compose cp backups/circlecore-db-YYYYMMDD-HHMMSS.dump db:/tmp/restore.dump
docker compose exec db pg_restore --clean --if-exists --no-owner --dbname "$DB_NAME" --username "$DB_USER" /tmp/restore.dump
docker compose up -d web
docker compose exec web python manage.py migrate_schemas --noinput --settings=config.settings_production
```

Restore media by extracting the matching media zip into the `media` Docker volume.

## Release Checklist

- `DEBUG=False`
- `PAYFAST_SANDBOX=False`
- SMTP sends successfully
- Password reset works on a tenant domain
- `docker compose logs web` has no migration/startup errors
- Direct `/media/cleaning_proofs/...` returns `403`
- Authenticated `/cleaning/proofs/<id>/photo/` works for the right property only
- Backups copied off-server
- Restore tested on staging
