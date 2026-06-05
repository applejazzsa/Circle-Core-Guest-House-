# Circle Core Guest House

A multi-tenant SaaS guest house management system built with Django and `django-tenants`. Each guest house gets a fully isolated PostgreSQL schema at `{name}.circlecore.co.za`.

## Architecture

- **Multi-tenancy**: `django-tenants` — schema-per-tenant on PostgreSQL
- **Public schema** (`circlecore.co.za`): landing page, registration, email verification
- **Tenant schemas** (`{name}.circlecore.co.za`): full guest house management app
- **Payments**: PayFast recurring subscriptions
- **Auth**: fully isolated per tenant — no cross-tenant user access

---

## Requirements

- Python 3.10+
- PostgreSQL 15+ (SQLite is not supported)
- Redis 7+ (rate limiting, cache)

---

## Local Development Setup

### 1. Install PostgreSQL and Redis

**macOS:**
```bash
brew install postgresql redis
brew services start postgresql
brew services start redis
```

**Ubuntu/Debian:**
```bash
sudo apt install postgresql redis-server
```

**Windows:** Install [PostgreSQL](https://www.postgresql.org/download/windows/) and [Redis for Windows](https://github.com/microsoftarchive/redis/releases).

### 2. Create the database

```bash
sudo -u postgres psql
```
```sql
CREATE DATABASE circlecore;
CREATE USER circlecore WITH PASSWORD 'devpassword';
GRANT ALL PRIVILEGES ON DATABASE circlecore TO circlecore;
\q
```

### 3. Install Python dependencies

```bash
python -m venv .venv
# Windows:
.\.venv\Scripts\Activate.ps1
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### 4. Configure environment

```bash
cp .env.example .env
```

Edit `.env` — minimum for local dev:
```
SECRET_KEY=any-local-dev-secret
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1,.circlecore.co.za
BASE_DOMAIN=circlecore.co.za
DB_NAME=circlecore
DB_USER=circlecore
DB_PASSWORD=devpassword
DB_HOST=localhost
DB_PORT=5432
PAYFAST_SANDBOX=True
```

### 5. Run migrations

```bash
# Shared (public) schema first — creates tenants + sessions tables
python manage.py migrate_schemas --shared

# This also runs tenant migrations in any existing tenant schemas
python manage.py migrate_schemas
```

### 6. Start the server

```bash
python manage.py runserver
```

Visit `http://localhost:8000/` — you'll see the public landing page.

### 7. Register a guest house

Go to `http://localhost:8000/register/` and create your first tenant. After submitting, you'll be redirected to `http://{name}.circlecore.co.za/login/`.

> **Local subdomain routing**: For local development, add an entry to your hosts file:
> ```
> # Windows: C:\Windows\System32\drivers\etc\hosts
> # macOS/Linux: /etc/hosts
> 127.0.0.1  yourname.circlecore.co.za
> ```

---

## Management Commands

All tenant-specific commands require `--schema <schema_name>`. Use `list_tenants` to find schema names.

| Command | Description |
|---|---|
| `list_tenants` | Show all tenants, domains, subscription status |
| `extend_subscription --schema <name> <days>` | Add days to a tenant's subscription |
| `setup_subscription --schema <name>` | Interactively configure a tenant's subscription |
| `send_trial_reminders` | Send expiry emails to tenants at 7, 3, 1 day remaining |
| `send_trial_reminders --dry-run` | Preview without sending |
| `backup_local` | pg_dump + media zip backup |
| `backup_local --output /path/to/dir` | Write backup to a specific directory |
| `seed_pos_items --schema <name>` | Seed default POS items for a tenant |

### Examples

```bash
# See all tenants
python manage.py list_tenants

# Extend Madlanga B&B trial by 14 days
python manage.py extend_subscription --schema madlanga_bb 14

# Run trial reminder emails
python manage.py send_trial_reminders

# Back up everything
python manage.py backup_local
```

### Applying new migrations

When you add new core migrations, apply them to ALL tenant schemas:

```bash
python manage.py migrate_schemas
```

This automatically applies the migration to every existing tenant schema.

---

## Production Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for the full VPS setup guide covering:
- PostgreSQL, Redis, Gunicorn, Nginx
- Wildcard subdomain routing
- Let's Encrypt wildcard SSL
- DNS configuration
- Cron jobs
- PayFast configuration

---

## Project Structure

```
config/
  settings.py              — Base settings (dev)
  settings_production.py   — Production overrides
  urls.py                  — Tenant schema URL conf
  urls_public.py           — Public schema URL conf (landing, register)

tenants/                   — Tenant management app (public schema)
  models.py                — GuestHouseTenant, Domain
  views.py                 — Registration, verification, PayFast
  payfast.py               — PayFast utilities

core/                      — Guest house app (per-tenant schema)
  models.py                — All guest house models
  views.py                 — All guest house views
  middleware.py            — Subscription + role enforcement
  management/commands/     — CLI tools

templates/
  public/                  — Landing page, registration
  emails/                  — Transactional email templates
  subscription/            — Subscription management pages
  base.html                — In-app base template
  base_public.html         — Public pages base template
  base_auth.html           — Login page base template
```
