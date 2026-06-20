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

## Shared Docker Network (one-time setup)

nginx runs as `circlecore-nginx` in its own container. Both nginx and the app must share a
Docker network so nginx can resolve `guesthouse-web` by name:

```bash
docker network create circlecore_net
```

If the network already exists this command returns an error — that is safe to ignore.

## Nginx Reverse Proxy

The nginx config file is checked into the repo at
`docker/nginx/guesthouse.circlecore.co.za.conf`.

Copy it to your nginx config directory (adjust the path to match your nginx container's
volume mount):

```bash
cp docker/nginx/guesthouse.circlecore.co.za.conf /etc/nginx/conf.d/
```

The config proxies to the `guesthouse-web` container over the shared `circlecore_net`
network. Key headers required for CSRF and HTTPS detection:

```
proxy_set_header Host $host;
proxy_set_header X-Forwarded-Proto https;
proxy_set_header X-Forwarded-Host $host;
```

Static requests are proxied to WhiteNoise in `guesthouse-web`. This deliberately
avoids hard-coded Docker volume paths, which vary with the Compose project name
and can serve a stale static manifest.

## Build And Start

```bash
git pull
docker compose -f docker-compose.production.yml build --no-cache guesthouse-web
docker compose -f docker-compose.production.yml up -d
docker cp docker/nginx/guesthouse.circlecore.co.za.conf circlecore-nginx:/etc/nginx/conf.d/guesthouse.circlecore.co.za.conf
docker exec -it circlecore-nginx nginx -t
docker restart circlecore-nginx
```

Check logs:

```bash
docker compose -f docker-compose.production.yml logs -f guesthouse-web
```

## Create Platform Admin

```bash
docker compose -f docker-compose.production.yml exec guesthouse-web python manage.py createsuperuser --settings=config.settings_production
```

## Verify

```bash
docker compose -f docker-compose.production.yml exec guesthouse-web python manage.py check --deploy --settings=config.settings_production
docker compose -f docker-compose.production.yml exec guesthouse-web python manage.py showmigrations --settings=config.settings_production
docker compose -f docker-compose.production.yml exec guesthouse-web python manage.py verify_static_assets --settings=config.settings_production
docker compose -f docker-compose.production.yml exec guesthouse-web python manage.py test_email support@circlecore.co.za --settings=config.settings_production
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
docker compose -f docker-compose.production.yml exec -T guesthouse-web python manage.py backup_local --output /app/backups --settings=config.settings_production
```

Copy `/app/backups` off-server daily.

## Restore Drill

Test restore monthly on a staging VPS:

```bash
docker compose -f docker-compose.production.yml down
docker volume rm circle-core-guest-house_postgres_data
docker compose -f docker-compose.production.yml up -d db redis
docker compose -f docker-compose.production.yml cp backups/circlecore-db-YYYYMMDD-HHMMSS.dump db:/tmp/restore.dump
docker compose -f docker-compose.production.yml exec db pg_restore --clean --if-exists --no-owner --dbname "$DB_NAME" --username "$DB_USER" /tmp/restore.dump
docker compose -f docker-compose.production.yml up -d guesthouse-web
docker compose -f docker-compose.production.yml exec guesthouse-web python manage.py migrate_schemas --noinput --settings=config.settings_production
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
