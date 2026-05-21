# Circle Core Guest House

A Django guest house management app using Django templates, SQLite for local development, static/media configuration, Tailwind CSS via CDN, authentication, PDF support, and production-aware settings.

## Local Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python manage.py migrate
python manage.py create_owner
python manage.py runserver
```

Open http://127.0.0.1:8000/ and log in with the owner account.

## Owner Account

Create the first administrator account with:

```powershell
python manage.py create_owner
```

The command prompts for username, email, and password, then creates a Django superuser.

## Production Settings

Copy `.env.example` to `.env` and set real values:

```powershell
SECRET_KEY=your-production-secret
DEBUG=False
ALLOWED_HOSTS=yourdomain.com,www.yourdomain.com
DATABASE_URL=sqlite:///db.sqlite3
```

Run with production settings:

```powershell
$env:DJANGO_SETTINGS_MODULE="config.settings_production"
python manage.py collectstatic
python manage.py migrate
```

## Notes

- Local development uses SQLite by default.
- `config/settings_production.py` can read a future PostgreSQL `DATABASE_URL`.
- A PostgreSQL migration and Docker deployment setup can be added in a future phase.
- Tailwind CSS is loaded from the CDN; no npm or frontend build step is required.
- Media uploads are stored in `media/`; collected static files go to `staticfiles/`.

## Local Backups

For a no-cost local backup of the SQLite database and uploaded media:

```powershell
python manage.py backup_local
```

Backups are written to `backups/` as zip files. Move these files off the computer regularly.
