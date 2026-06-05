# Circle Core Guest House — VPS Deployment Guide

## Stack

- **OS**: Ubuntu 22.04 LTS
- **Database**: PostgreSQL 15
- **Cache**: Redis 7 (rate limiting, sessions)
- **App server**: Gunicorn
- **Web server**: Nginx (wildcard subdomain routing)
- **SSL**: Let's Encrypt wildcard certificate (Certbot + DNS challenge)
- **Process manager**: systemd

---

## 1. Initial Server Setup

```bash
# Update and install essentials
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-pip python3-venv git nginx certbot python3-certbot-nginx postgresql postgresql-contrib

# Create app user
sudo adduser --system --group circlecore
```

---

## 2. Redis Setup

```bash
sudo apt install -y redis-server
sudo systemctl enable redis-server
sudo systemctl start redis-server
# Verify
redis-cli ping   # should return PONG
```

---

## 3. PostgreSQL Setup

```bash
sudo -u postgres psql
```

```sql
CREATE DATABASE circlecore;
CREATE USER circlecore WITH PASSWORD 'use-a-strong-password-here';
ALTER ROLE circlecore SET client_encoding TO 'utf8';
ALTER ROLE circlecore SET default_transaction_isolation TO 'read committed';
ALTER ROLE circlecore SET timezone TO 'Africa/Johannesburg';
GRANT ALL PRIVILEGES ON DATABASE circlecore TO circlecore;
\q
```

---

## 3. Deploy the Application

```bash
# Clone the repo
sudo mkdir -p /var/www/circlecore
sudo chown circlecore:circlecore /var/www/circlecore
cd /var/www/circlecore
sudo -u circlecore git clone <your-repo-url> .

# Create virtual environment and install packages
sudo -u circlecore python3 -m venv .venv
sudo -u circlecore .venv/bin/pip install -r requirements.txt

# Set up environment
sudo -u circlecore cp .env.example .env
sudo -u circlecore nano .env   # Fill in all values
```

### `.env` values to set

```
SECRET_KEY=<generate with: python3 -c "import secrets; print(secrets.token_urlsafe(50))">
DEBUG=False
ALLOWED_HOSTS=circlecore.co.za,.circlecore.co.za
BASE_DOMAIN=circlecore.co.za
DB_NAME=circlecore
DB_USER=circlecore
DB_PASSWORD=<your-db-password>
DB_HOST=localhost
DB_PORT=5432
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=smtp.gmail.com
EMAIL_HOST_USER=your@email.com
EMAIL_HOST_PASSWORD=your-app-password
DEFAULT_FROM_EMAIL=Circle Core <noreply@circlecore.co.za>
PAYFAST_MERCHANT_ID=<from payfast.co.za>
PAYFAST_MERCHANT_KEY=<from payfast.co.za>
PAYFAST_PASSPHRASE=<if set in payfast dashboard>
PAYFAST_SANDBOX=False
```

---

## 4. Run Migrations and Collect Static Files

```bash
cd /var/www/circlecore

# Migrate shared (public) schema first — creates tenants and auth tables
sudo -u circlecore .venv/bin/python manage.py migrate_schemas --shared --settings=config.settings_production

# Collect static files
sudo -u circlecore .venv/bin/python manage.py collectstatic --noinput --settings=config.settings_production

# Create the platform superadmin (for circlecore.co.za/admin/)
sudo -u circlecore .venv/bin/python manage.py createsuperuser --settings=config.settings_production
```

---

## 5. Gunicorn systemd Service

```bash
sudo nano /etc/systemd/system/circlecore.service
```

```ini
[Unit]
Description=Circle Core Guest House — Gunicorn
After=network.target postgresql.service

[Service]
User=circlecore
Group=circlecore
WorkingDirectory=/var/www/circlecore
EnvironmentFile=/var/www/circlecore/.env
ExecStart=/var/www/circlecore/.venv/bin/gunicorn \
    --workers 3 \
    --bind unix:/run/circlecore.sock \
    --access-logfile /var/log/circlecore/access.log \
    --error-logfile /var/log/circlecore/error.log \
    config.wsgi:application
ExecReload=/bin/kill -s HUP $MAINPID
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo mkdir -p /var/log/circlecore
sudo chown circlecore:circlecore /var/log/circlecore
sudo systemctl daemon-reload
sudo systemctl enable circlecore
sudo systemctl start circlecore
```

---

## 6. Nginx — Wildcard Subdomain Routing

```bash
sudo nano /etc/nginx/sites-available/circlecore
```

```nginx
# Redirect HTTP → HTTPS for root domain
server {
    listen 80;
    server_name circlecore.co.za www.circlecore.co.za *.circlecore.co.za;
    return 301 https://$host$request_uri;
}

# HTTPS — serves both root domain and all *.circlecore.co.za subdomains
server {
    listen 443 ssl;
    server_name circlecore.co.za www.circlecore.co.za *.circlecore.co.za;

    ssl_certificate     /etc/letsencrypt/live/circlecore.co.za/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/circlecore.co.za/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    client_max_body_size 20M;

    location /static/ {
        alias /var/www/circlecore/staticfiles/;
        expires 30d;
        add_header Cache-Control "public";
    }

    location /media/ {
        alias /var/www/circlecore/media/;
    }

    location / {
        proxy_pass http://unix:/run/circlecore.sock;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/circlecore /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

---

## 7. Wildcard SSL Certificate (Let's Encrypt)

Wildcard certs require a DNS challenge — you cannot use the HTTP challenge.

```bash
sudo certbot certonly \
  --manual \
  --preferred-challenges dns \
  -d circlecore.co.za \
  -d "*.circlecore.co.za"
```

Certbot will ask you to add a `TXT` record to your DNS. Add it in your domain registrar's DNS panel:

```
_acme-challenge.circlecore.co.za  TXT  <value-from-certbot>
```

Wait 60 seconds for DNS to propagate, then press Enter in the terminal.

### Auto-renewal

Wildcard DNS certs need a DNS plugin for auto-renewal. If your DNS provider is Cloudflare:

```bash
sudo apt install python3-certbot-dns-cloudflare
# Configure Cloudflare credentials and use --dns-cloudflare flag
```

Otherwise, set a calendar reminder to renew every 60 days manually:
```bash
sudo certbot renew
sudo systemctl reload nginx
```

---

## 8. DNS Configuration

In your domain registrar (for circlecore.co.za), add:

| Type | Name | Value |
|------|------|-------|
| A | `@` | `<your-VPS-IP>` |
| A | `www` | `<your-VPS-IP>` |
| A | `*` | `<your-VPS-IP>` |

The wildcard `*` record routes all subdomains (e.g. `madlanga.circlecore.co.za`) to your VPS.

---

## 9. Cron Jobs

```bash
sudo -u circlecore crontab -e
```

```cron
# Send trial expiry reminders daily at 9am SAST
0 9 * * * /var/www/circlecore/.venv/bin/python /var/www/circlecore/manage.py send_trial_reminders --settings=config.settings_production

# Certificate renewal check (twice daily)
0 0,12 * * * /usr/bin/certbot renew --quiet && systemctl reload nginx
```

---

## 10. Deploying Updates

```bash
cd /var/www/circlecore
sudo -u circlecore git pull

# Apply any new migrations to ALL tenant schemas
sudo -u circlecore .venv/bin/python manage.py migrate_schemas --settings=config.settings_production

# Collect static if changed
sudo -u circlecore .venv/bin/python manage.py collectstatic --noinput --settings=config.settings_production

# Restart app
sudo systemctl restart circlecore
```

> **Important**: Always use `migrate_schemas` (not `migrate`) when deploying. It applies shared migrations to the public schema and tenant migrations to every tenant schema automatically.

---

## 11. PayFast Setup

1. Register at [payfast.co.za](https://payfast.co.za)
2. Go to **My Account → Settings → Merchant Details**
3. Copy your **Merchant ID** and **Merchant Key** into `.env`
4. Set an optional **Passphrase** (recommended) — must match `PAYFAST_PASSPHRASE` in `.env`
5. Under **ITN Settings**, PayFast will POST to `https://{tenant}.circlecore.co.za/payfast/itn/` automatically — no manual configuration needed per tenant
6. Set `PAYFAST_SANDBOX=False` in production

---

## 12. Useful Commands

```bash
# List all tenants
sudo -u circlecore .venv/bin/python manage.py shell -c "from tenants.models import GuestHouseTenant; [print(t.schema_name, t.name) for t in GuestHouseTenant.objects.all()]" --settings=config.settings_production

# Run command in a specific tenant schema
sudo -u circlecore .venv/bin/python manage.py shell --settings=config.settings_production
>>> from django_tenants.utils import schema_context
>>> with schema_context('madlanga_bb'):
...     from core.models import Subscription
...     print(Subscription.objects.first())

# Check Gunicorn status
sudo systemctl status circlecore

# Tail logs
sudo tail -f /var/log/circlecore/error.log
```
