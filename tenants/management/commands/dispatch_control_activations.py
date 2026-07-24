from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.db import models, transaction
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django_tenants.utils import schema_context

from tenants.models import ControlActivationOutbox


class Command(BaseCommand):
    help = 'Dispatch product-owned Smart Control Guest House activation invitations.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=25)
        parser.add_argument('--max-attempts', type=int, default=5)

    def handle(self, *args, **options):
        limit = max(1, min(options['limit'], 100))
        max_attempts = max(1, min(options['max_attempts'], 10))
        ids = list(ControlActivationOutbox.objects.filter(
            state__in=('pending', 'failed'), attempts__lt=max_attempts,
        ).values_list('pk', flat=True)[:limit])
        sent = failed = 0
        for message_id in ids:
            try:
                with transaction.atomic():
                    message = ControlActivationOutbox.objects.select_for_update().select_related('tenant').get(pk=message_id)
                    if message.state == 'sent' or message.attempts >= max_attempts:
                        continue
                    message.attempts += 1
                    with schema_context(message.tenant.schema_name):
                        user = get_user_model().objects.get(pk=message.user_id)
                        uid = urlsafe_base64_encode(force_bytes(user.pk))
                        token = PasswordResetTokenGenerator().make_token(user)
                    domain = message.tenant.domains.filter(is_primary=True).values_list('domain', flat=True).first()
                    if not domain:
                        raise RuntimeError('Tenant primary domain is unavailable')
                    invite_url = f'https://{domain}/password-reset/{uid}/{token}/'
                    reset = message.kind == 'password_reset'
                    send_mail(
                        subject=(f'Reset your Circle Core Guest House password for {message.tenant.name}' if reset else f'Activate your Circle Core Guest House account for {message.tenant.name}'),
                        message=(
                            f'Hello {user.first_name or "Administrator"},\n\n'
                            f'{"Use this secure link to reset your password" if reset else "Your Guest House workspace is ready. Create your password securely here"}:\n'
                            f'{invite_url}\n\nIf you did not expect this invitation, contact Circle Core support.'
                        ),
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        recipient_list=[message.recipient], fail_silently=False,
                    )
                    message.state = 'sent'
                    message.last_error_code = ''
                    message.sent_at = timezone.now()
                    message.save(update_fields=['attempts', 'state', 'last_error_code', 'sent_at'])
                    sent += 1
            except Exception as exc:
                ControlActivationOutbox.objects.filter(pk=message_id).update(
                    attempts=models.F('attempts') + 1, state='failed',
                    last_error_code=exc.__class__.__name__[:80],
                )
                failed += 1
        self.stdout.write(f'processed={len(ids)} sent={sent} failed={failed}')
