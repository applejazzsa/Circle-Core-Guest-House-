"""
backup_local — PostgreSQL dump + media folder backup.

Creates a compressed pg_dump of the entire database (all schemas) plus
a zip of the media directory. Requires pg_dump to be in PATH.

Usage:
    python manage.py backup_local
    python manage.py backup_local --output /mnt/backups
"""

import subprocess
import zipfile
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Create a pg_dump + media backup of the Circle Core installation.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--output',
            default='backups',
            help='Directory to write backups to. Defaults to ./backups.',
        )

    def handle(self, *args, **options):
        output_dir = Path(options['output'])
        if not output_dir.is_absolute():
            output_dir = Path(settings.BASE_DIR) / output_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        db = settings.DATABASES['default']

        if 'postgresql' not in db.get('ENGINE', '') and 'django_tenants' not in db.get('ENGINE', ''):
            raise CommandError('backup_local requires a PostgreSQL database.')

        # ── pg_dump ──────────────────────────────────────────────────────────
        dump_path = output_dir / f'circlecore-db-{timestamp}.dump'
        pg_cmd = [
            'pg_dump',
            '--format=custom',
            f'--dbname={db["NAME"]}',
            f'--host={db.get("HOST", "localhost")}',
            f'--port={db.get("PORT", "5432")}',
            f'--username={db["USER"]}',
            f'--file={dump_path}',
        ]
        env = {'PGPASSWORD': db.get('PASSWORD', '')}

        self.stdout.write(f'Running pg_dump -> {dump_path} ...')
        try:
            result = subprocess.run(pg_cmd, env={**__import__("os").environ, **env},
                                    capture_output=True, text=True)
            if result.returncode != 0:
                raise CommandError(f'pg_dump failed:\n{result.stderr}')
        except FileNotFoundError:
            raise CommandError('pg_dump not found. Install postgresql-client on the server.')

        self.stdout.write(self.style.SUCCESS(f'Database dump saved: {dump_path}'))

        # ── Media zip ────────────────────────────────────────────────────────
        media_root = Path(settings.MEDIA_ROOT)
        if media_root.exists() and any(media_root.rglob('*')):
            media_zip = output_dir / f'circlecore-media-{timestamp}.zip'
            self.stdout.write(f'Zipping media -> {media_zip} ...')
            with zipfile.ZipFile(media_zip, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                for path in media_root.rglob('*'):
                    if path.is_file():
                        archive.write(path, Path('media') / path.relative_to(media_root))
            self.stdout.write(self.style.SUCCESS(f'Media backup saved: {media_zip}'))
        else:
            self.stdout.write('No media files found - skipping media zip.')

        self.stdout.write(self.style.SUCCESS(
            f'\nBackup complete. Files are in: {output_dir}\n'
            'Move these off the server regularly (S3, external drive, etc.).'
        ))
