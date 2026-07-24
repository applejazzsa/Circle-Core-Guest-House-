from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.utils import timezone

from tenants.models import ControlOperationNotification


class Command(BaseCommand):
    help = 'Dispatch queued customer-safe Smart Control operation notifications.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=50)

    def handle(self, *args, **options):
        rows = ControlOperationNotification.objects.filter(state__in=('queued', 'failed'), attempts__lt=5).select_related('tenant')[:max(1, min(options['limit'], 100))]
        sent = failed = 0
        for row in rows:
            try:
                row.attempts += 1
                send_mail(
                    subject=f'Guest House account update for {row.tenant.name}',
                    message=f'Your Guest House account has been updated: {row.action.replace("_", " ")}.\n\nContact Circle Core support if you need assistance.',
                    from_email=settings.DEFAULT_FROM_EMAIL, recipient_list=[row.recipient], fail_silently=False,
                )
                row.state, row.sent_at, row.last_error_code = 'sent', timezone.now(), ''
                sent += 1
            except Exception as exc:
                row.state, row.last_error_code = 'failed', exc.__class__.__name__[:80]
                failed += 1
            row.save(update_fields=['attempts', 'state', 'sent_at', 'last_error_code'])
        self.stdout.write(f'sent={sent} failed={failed}')
