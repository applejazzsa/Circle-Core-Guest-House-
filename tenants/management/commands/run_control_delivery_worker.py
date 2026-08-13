import signal
import time

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone

from tenants.models import ControlDeliveryWorkerHeartbeat


class Command(BaseCommand):
    help = "Dispatch Guest House control emails/notifications and purge expired replay nonces."

    def add_arguments(self, parser):
        parser.add_argument("--poll-seconds", type=int, default=10)
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        poll_seconds = max(2, min(options["poll_seconds"], 60))
        stopping = False

        def stop(*_args):
            nonlocal stopping
            stopping = True

        for name in ("SIGTERM", "SIGINT"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), stop)

        purge_iteration = 0
        while not stopping:
            now = timezone.now()
            ControlDeliveryWorkerHeartbeat.objects.update_or_create(
                name='control-delivery',
                defaults={'last_seen_at': now, 'last_error_code': ''},
            )
            try:
                call_command("dispatch_control_activations", limit=50, verbosity=0)
                call_command("dispatch_control_operation_notifications", limit=100, verbosity=0)
                if purge_iteration % max(1, 3600 // poll_seconds) == 0:
                    call_command("purge_control_api_nonces", verbosity=0)
            except Exception as exc:
                ControlDeliveryWorkerHeartbeat.objects.filter(name='control-delivery').update(
                    last_seen_at=timezone.now(), last_error_code=exc.__class__.__name__[:80],
                )
                raise
            ControlDeliveryWorkerHeartbeat.objects.filter(name='control-delivery').update(
                last_seen_at=timezone.now(), last_success_at=timezone.now(), last_error_code='',
            )
            purge_iteration += 1
            if options["once"]:
                break
            time.sleep(poll_seconds)
