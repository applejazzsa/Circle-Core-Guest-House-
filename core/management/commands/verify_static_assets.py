from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Verify that required production assets and their manifest versions exist."

    required_assets = (
        "css/theme.css",
        "icons/circle-core-icon.svg",
        "icons/circle-core-icon-192.png",
        "icons/circle-core-icon-512.png",
        "js/offline-reception.js",
    )

    def handle(self, *args, **options):
        root = Path(settings.STATIC_ROOT)
        failures = []
        for asset in self.required_assets:
            try:
                stored_name = staticfiles_storage.stored_name(asset)
                public_url = staticfiles_storage.url(asset)
            except Exception as exc:
                failures.append(f"{asset}: manifest lookup failed ({exc})")
                continue
            stored_path = root / stored_name
            if not stored_path.is_file():
                failures.append(f"{asset}: missing collected file {stored_path}")
            else:
                self.stdout.write(self.style.SUCCESS(f"{asset} -> {public_url}"))

        if failures:
            raise CommandError("Static asset verification failed:\n" + "\n".join(failures))
        self.stdout.write(self.style.SUCCESS("Required static assets verified."))
