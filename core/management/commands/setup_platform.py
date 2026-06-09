"""
setup_platform — Ensure the public tenant record and its primary domain exist.

Run once after migrate_schemas so that BASE_DOMAIN serves the landing page,
register form, and Command Center instead of routing to a tenant workspace.
Safe to re-run on every deploy — uses get_or_create throughout.

Usage:
    python manage.py setup_platform
    python manage.py setup_platform --domain guesthouse.circlecore.co.za
"""

from django.conf import settings
from django.core.management.base import BaseCommand

from tenants.models import Domain, GuestHouseTenant


class Command(BaseCommand):
    help = "Ensure the public tenant record and its primary domain exist."

    def add_arguments(self, parser):
        parser.add_argument(
            "--domain",
            default=None,
            help="Public domain to assign (defaults to BASE_DOMAIN from settings).",
        )

    def handle(self, *args, **options):
        base_domain = (options["domain"] or settings.BASE_DOMAIN).strip().lower()

        # ── 1. Public tenant record ───────────────────────────────────────────
        tenant, created = GuestHouseTenant.objects.get_or_create(
            schema_name="public",
            defaults={
                "name": "Circle Core Platform",
                "owner_name": "Circle Core Technologies",
                "owner_email": "platform@circlecore.co.za",
                "is_active": True,
                "is_verified": True,
            },
        )
        verb = "Created" if created else "Found"
        self.stdout.write(f"{verb} public tenant (schema: public, name: {tenant.name})")

        # ── 2. Domain assignment ──────────────────────────────────────────────
        domain_obj, domain_created = Domain.objects.get_or_create(
            domain=base_domain,
            defaults={"tenant": tenant, "is_primary": True},
        )

        if not domain_created and domain_obj.tenant_id != tenant.pk:
            old_schema = domain_obj.tenant.schema_name
            self.stdout.write(self.style.WARNING(
                f"Domain '{base_domain}' was mapped to tenant '{old_schema}' "
                f"— reassigning to public schema."
            ))
            domain_obj.tenant = tenant
            domain_obj.is_primary = True
            domain_obj.save(update_fields=["tenant", "is_primary"])
            self.stdout.write(self.style.SUCCESS(
                f"Reassigned '{base_domain}' → public tenant"
            ))
        else:
            verb = "Created" if domain_created else "Found"
            self.stdout.write(f"{verb} domain '{base_domain}' → public tenant")

        # ── 3. Summary ────────────────────────────────────────────────────────
        self.stdout.write(self.style.SUCCESS(
            f"\nPublic site ready:\n"
            f"  Landing page : https://{base_domain}/\n"
            f"  Register     : https://{base_domain}/register/\n"
            f"  Request demo : https://{base_domain}/request-demo/\n"
            f"  Command Ctr  : https://{base_domain}/command/\n"
        ))
