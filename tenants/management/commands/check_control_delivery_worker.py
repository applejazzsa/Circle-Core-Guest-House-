from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from tenants.models import ControlDeliveryWorkerHeartbeat


class Command(BaseCommand):
    help = 'Fail unless the product-owned control delivery worker heartbeat is current.'

    def add_arguments(self, parser):
        parser.add_argument('--max-age-seconds', type=int, default=90)

    def handle(self, *args, **options):
        max_age = max(10, min(options['max_age_seconds'], 600))
        heartbeat = ControlDeliveryWorkerHeartbeat.objects.filter(name='control-delivery').first()
        if (
            not heartbeat or not heartbeat.last_success_at or heartbeat.last_error_code
            or heartbeat.last_seen_at < timezone.now() - timedelta(seconds=max_age)
        ):
            raise CommandError('The control delivery worker heartbeat is missing, stale, or failed.')
        self.stdout.write(self.style.SUCCESS('Control delivery worker is healthy.'))
