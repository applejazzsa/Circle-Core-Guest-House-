from django.core.management.base import BaseCommand
from django_tenants.utils import schema_context

from tenants.models import GuestHouseTenant


class Command(BaseCommand):
    help = 'List all registered guest house tenants and their subscription status.'

    def handle(self, *args, **options):
        tenants = GuestHouseTenant.objects.prefetch_related('domains').order_by('created_at')

        if not tenants.exists():
            self.stdout.write('No tenants registered yet.')
            return

        self.stdout.write(f'\n{"SCHEMA":<25} {"NAME":<30} {"DOMAIN":<40} {"VERIFIED":<10} {"STATUS":<12} {"DAYS LEFT"}')
        self.stdout.write('─' * 125)

        for tenant in tenants:
            domain = tenant.domains.filter(is_primary=True).first()
            domain_str = domain.domain if domain else '—'
            verified = 'Yes' if tenant.is_verified else 'No'

            sub_status = '—'
            days_left = '—'
            try:
                with schema_context(tenant.schema_name):
                    from core.models import Subscription
                    sub = Subscription.objects.select_related('plan').first()
                    if sub:
                        sub_status = sub.status
                        days_left = str(sub.days_remaining)
            except Exception:
                sub_status = 'error'

            self.stdout.write(
                f'{tenant.schema_name:<25} {tenant.name:<30} {domain_str:<40} '
                f'{verified:<10} {sub_status:<12} {days_left}'
            )

        self.stdout.write(f'\nTotal: {tenants.count()} tenant(s)')
