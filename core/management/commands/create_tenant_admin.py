"""
Create or update a superuser inside a specific tenant's schema.

Usage:
    python manage.py create_tenant_admin \
        --domain guesthouse.circlecore.co.za \
        --username admin \
        --email admin@circlecore.co.za \
        --password <password>
"""

from django.core.management.base import BaseCommand, CommandError
from django_tenants.utils import schema_context

from tenants.models import Domain


class Command(BaseCommand):
    help = "Create or update a superuser inside a tenant schema, looked up by domain."

    def add_arguments(self, parser):
        parser.add_argument("--domain", required=True, help="Primary domain of the tenant")
        parser.add_argument("--username", required=True)
        parser.add_argument("--email", required=True)
        parser.add_argument("--password", required=True)

    def handle(self, *args, **options):
        domain_str = options["domain"].lower().strip()
        username = options["username"]
        email = options["email"].lower().strip()
        password = options["password"]

        try:
            domain = Domain.objects.select_related("tenant").get(domain=domain_str)
        except Domain.DoesNotExist:
            raise CommandError(
                f"No tenant found for domain '{domain_str}'.\n"
                "Run 'python manage.py list_tenants' to see registered domains."
            )

        tenant = domain.tenant
        schema = tenant.schema_name

        with schema_context(schema):
            from django.contrib.auth import get_user_model
            User = get_user_model()

            user, created = User.objects.get_or_create(username=username)
            user.email = email
            user.is_staff = True
            user.is_superuser = True
            user.is_active = True
            user.set_password(password)
            user.save()

        verb = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} superuser '{username}' in schema '{schema}' ({tenant.name})"
        ))
