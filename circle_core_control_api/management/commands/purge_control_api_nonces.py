from django.core.management.base import BaseCommand
from django.utils import timezone

from circle_core_control_api.models import RequestNonce


class Command(BaseCommand):
    help = "Delete expired product control API replay nonces. Schedule this in each product environment."

    def handle(self, *args, **options):
        deleted, _ = RequestNonce.objects.filter(expires_at__lt=timezone.now()).delete()
        self.stdout.write(f"Purged {deleted} expired control API nonce records.")
