from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django_tenants.utils import schema_context

from tenants.models import GuestHouseTenant


class Command(BaseCommand):
    help = 'Extend a tenant subscription by N days.'

    def add_arguments(self, parser):
        parser.add_argument('days', type=int, help='Number of days to add.')
        parser.add_argument('--schema', required=True, help='Tenant schema name (e.g. madlanga_bb).')

    def handle(self, *args, **options):
        days = options['days']
        schema_name = options['schema']

        if days <= 0:
            raise CommandError('Days must be greater than zero.')

        if not GuestHouseTenant.objects.filter(schema_name=schema_name).exists():
            raise CommandError(f'No tenant found with schema "{schema_name}". '
                               f'Run: python manage.py list_tenants')

        with schema_context(schema_name):
            from core.models import Subscription
            subscription = Subscription.objects.select_related('plan').first()
            if not subscription:
                raise CommandError(f'No subscription found in schema "{schema_name}".')

            base = subscription.expires_at if subscription.expires_at > timezone.now() else timezone.now()
            subscription.expires_at = base + timezone.timedelta(days=days)
            if subscription.status == 'expired':
                subscription.status = 'active'
                subscription.save(update_fields=['expires_at', 'status'])
            else:
                subscription.save(update_fields=['expires_at'])

            self.stdout.write(self.style.SUCCESS(
                f'[{schema_name}] Subscription extended by {days} days. '
                f'New expiry: {subscription.expires_at:%Y-%m-%d}. '
                f'Days remaining: {subscription.days_remaining}.'
            ))
