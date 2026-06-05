from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django_tenants.utils import schema_context

from core.subscriptions import next_renewal_datetime, trial_end
from tenants.models import GuestHouseTenant


class Command(BaseCommand):
    help = 'Create or replace the subscription for a specific tenant schema.'

    def add_arguments(self, parser):
        parser.add_argument('--schema', required=True, help='Tenant schema name (e.g. madlanga_bb).')

    def handle(self, *args, **options):
        schema_name = options['schema']

        if not GuestHouseTenant.objects.filter(schema_name=schema_name).exists():
            raise CommandError(f'No tenant found with schema "{schema_name}". '
                               f'Run: python manage.py list_tenants')

        with schema_context(schema_name):
            from core.models import Subscription, SubscriptionPlan

            plans = list(SubscriptionPlan.objects.order_by('monthly_price'))
            if not plans:
                raise CommandError('No subscription plans in this schema. Run migrate_schemas first.')

            owner_name = input('Owner name: ').strip()
            owner_email = input('Owner email: ').strip()
            owner_phone = input('Owner phone: ').strip()

            self.stdout.write('Available plans:')
            for i, plan in enumerate(plans, start=1):
                self.stdout.write(f'  {i}. {plan.display_name} — R {plan.monthly_price}/mo')
            plan_idx = int(input('Choose plan number: ').strip() or '1') - 1
            plan = plans[plan_idx]

            billing = input('Billing cycle (1=Monthly, 2=Annual) [1]: ').strip() or '1'
            billing_cycle = 'annual' if billing == '2' else 'monthly'

            status_choice = input('Status (1=Trial, 2=Active) [1]: ').strip() or '1'
            status = 'active' if status_choice == '2' else 'trial'

            now = timezone.now()
            if status == 'trial':
                expires_at = trial_end(now)
                trial_ends_at = expires_at
            else:
                expires_at = next_renewal_datetime(billing_cycle, now)
                trial_ends_at = None

            Subscription.objects.all().delete()
            subscription = Subscription.objects.create(
                plan=plan,
                billing_cycle=billing_cycle,
                status=status,
                expires_at=expires_at,
                trial_ends_at=trial_ends_at,
                owner_name=owner_name,
                owner_email=owner_email,
                owner_phone=owner_phone,
                next_billing_date=expires_at.date(),
            )

            self.stdout.write(self.style.SUCCESS(
                f'[{schema_name}] Subscription configured. '
                f'Plan: {subscription.plan.display_name}. '
                f'Status: {subscription.get_status_display()}. '
                f'Expires: {subscription.expires_at:%Y-%m-%d}.'
            ))
