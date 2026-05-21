import shutil
import zipfile
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create a local zip backup of the SQLite database and media folder."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            default="backups",
            help="Directory where the backup zip should be written. Defaults to ./backups.",
        )

    def handle(self, *args, **options):
        output_dir = Path(options["output"])
        if not output_dir.is_absolute():
            output_dir = Path(settings.BASE_DIR) / output_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = output_dir / f"circle-core-backup-{timestamp}.zip"

        db_config = settings.DATABASES["default"]
        db_engine = db_config.get("ENGINE", "")
        db_name = db_config.get("NAME")
        if "sqlite" not in db_engine:
            raise CommandError("backup_local only supports SQLite. Use your database provider backup for production DBs.")
        db_path = Path(db_name)
        if not db_path.exists():
            raise CommandError(f"Database file not found: {db_path}")

        temp_db = output_dir / f"db-{timestamp}.sqlite3"
        shutil.copy2(db_path, temp_db)

        media_root = Path(settings.MEDIA_ROOT)
        try:
            with zipfile.ZipFile(backup_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(temp_db, "db.sqlite3")
                if media_root.exists():
                    for path in media_root.rglob("*"):
                        if path.is_file():
                            archive.write(path, Path("media") / path.relative_to(media_root))
        finally:
            if temp_db.exists():
                temp_db.unlink()

        self.stdout.write(self.style.SUCCESS(f"Backup created: {backup_path}"))
