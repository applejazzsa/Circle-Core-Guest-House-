import uuid
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import TrialLicense


class Command(BaseCommand):
    help = "Activate a 30-day Circle Core Guest House trial license."

    def handle(self, *args, **options):
        guest_house_name = input("Guest house name: ").strip()
        owner_email = input("Owner email: ").strip()

        license_obj = TrialLicense.objects.create(
            guest_house_name=guest_house_name,
            owner_email=owner_email,
            expires_at=timezone.now() + timedelta(days=30),
            license_key=f"CCG-TRIAL-{uuid.uuid4().hex[:8].upper()}",
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Trial activated. "
                f"License: {license_obj.license_key}. "
                f"Expires: {license_obj.expires_at:%Y-%m-%d}. "
                f"Days remaining: {license_obj.days_remaining}."
            )
        )
