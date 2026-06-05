from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Deprecated. Owner accounts are now created via the registration page at circlecore.co.za/register/'

    def handle(self, *args, **options):
        self.stdout.write(self.style.WARNING(
            'create_owner is no longer used.\n'
            'Tenant admin accounts are created automatically when a guest house\n'
            'registers at https://circlecore.co.za/register/\n\n'
            'To create a tenant admin via CLI:\n'
            '  from django_tenants.utils import schema_context\n'
            '  with schema_context("your_schema"):\n'
            '      from django.contrib.auth import get_user_model\n'
            '      get_user_model().objects.create_superuser(...)'
        ))
