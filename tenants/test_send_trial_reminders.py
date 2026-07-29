"""
Regression coverage for the daily cron error:

    relation "core_subscription" does not exist

send_trial_reminders() previously iterated every active GuestHouseTenant row,
including the public-schema pseudo-tenant django-tenants uses to host
SHARED_APPS. `core` (which owns Subscription) is a TENANT_APP — its tables
only ever exist inside real tenant schemas, never in public — so querying
Subscription while schema_context()'d into public always raised.

The fix is the same one-line `.exclude(schema_name="public")` filter every
other tenant-iterating command in this codebase already uses.
"""

import datetime
from io import StringIO

from django.core.management import call_command
from django.utils import timezone
from django_tenants.utils import schema_context

from core.models import Subscription, SubscriptionPlan
from core.tests import CircleCoreTenantTestCase
from tenants.models import GuestHouseTenant


class SendTrialRemindersPublicSchemaExclusionTest(CircleCoreTenantTestCase):
    def _ensure_public_pseudo_tenant(self):
        """Mirrors production exactly: a GuestHouseTenant row with
        schema_name='public' representing the platform's own shared schema,
        not a real billable guest house."""
        with schema_context("public"):
            tenant, _ = GuestHouseTenant.objects.get_or_create(
                schema_name="public",
                defaults=dict(
                    name="Circle Core Platform",
                    owner_name="Platform",
                    owner_email="platform-public-schema@example.com",
                    owner_phone="0800000000",
                    is_active=True,
                    is_verified=True,
                ),
            )
            return tenant

    def _create_subscription_due_for_reminder(self, owner_email):
        # 5 days + 1 hour lands squarely in TRIAL_REMINDER_SCHEDULE[5],
        # comfortably clear of test-execution timing jitter.
        plan = SubscriptionPlan.objects.get(name="professional")
        return Subscription.objects.create(
            plan=plan,
            status="trial",
            expires_at=timezone.now() + datetime.timedelta(days=5, hours=1),
            owner_name="Real Tenant Owner",
            owner_email=owner_email,
        )

    def test_public_schema_is_excluded_and_produces_no_error(self):
        self._ensure_public_pseudo_tenant()

        stdout, stderr = StringIO(), StringIO()
        call_command("send_trial_reminders", "--dry-run", stdout=stdout, stderr=stderr)

        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("public", stderr.getvalue())
        self.assertNotIn("core_subscription", stdout.getvalue() + stderr.getvalue())

    def test_real_tenant_schema_is_still_processed(self):
        self._ensure_public_pseudo_tenant()
        self._create_subscription_due_for_reminder("real-tenant-owner@example.com")

        stdout = StringIO()
        call_command("send_trial_reminders", "--dry-run", stdout=stdout)

        output = stdout.getvalue()
        self.assertIn(self.tenant.schema_name, output)
        self.assertIn("real-tenant-owner@example.com", output)

    def test_excluded_public_schema_does_not_block_real_tenant_processing(self):
        # The public pseudo-tenant is now excluded outright rather than
        # merely "caught after failing" — prove a real tenant is still
        # processed cleanly, with zero error output, in the same run.
        self._ensure_public_pseudo_tenant()
        self._create_subscription_due_for_reminder("real-tenant-owner-2@example.com")

        stdout, stderr = StringIO(), StringIO()
        call_command("send_trial_reminders", "--dry-run", stdout=stdout, stderr=stderr)

        self.assertEqual(stderr.getvalue(), "")
        self.assertIn("real-tenant-owner-2@example.com", stdout.getvalue())

    def test_filter_does_not_modify_any_tenant_data(self):
        public_tenant = self._ensure_public_pseudo_tenant()
        with schema_context("public"):
            before_count = GuestHouseTenant.objects.count()
            before_active = public_tenant.is_active

        call_command("send_trial_reminders", "--dry-run", stdout=StringIO())

        with schema_context("public"):
            public_tenant.refresh_from_db()
            self.assertEqual(GuestHouseTenant.objects.count(), before_count)
            self.assertEqual(public_tenant.is_active, before_active)
            self.assertEqual(public_tenant.schema_name, "public")

    def test_running_twice_is_idempotent(self):
        self._ensure_public_pseudo_tenant()
        self._create_subscription_due_for_reminder("idempotent-owner@example.com")

        first_out, second_out = StringIO(), StringIO()
        call_command("send_trial_reminders", "--dry-run", stdout=first_out)
        call_command("send_trial_reminders", "--dry-run", stdout=second_out)

        self.assertIn("idempotent-owner@example.com", first_out.getvalue())
        self.assertIn("idempotent-owner@example.com", second_out.getvalue())
        self.assertEqual(Subscription.objects.count(), 1)
