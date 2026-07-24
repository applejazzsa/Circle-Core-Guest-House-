import uuid
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.db import transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from django_tenants.utils import schema_context

from circle_core_control_api.errors import ControlAPIError

from core.models import ControlUserSecurity, Property, StaffProfile, Subscription
from tenants.models import ControlActivationOutbox, ControlOperationNotification, GuestHouseTenant
from tenants.product_control_backend import GuestHouseProductControlBackend


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class GuestHouseProductControlTests(TransactionTestCase):
    reset_sequences = True

    def tearDown(self):
        ControlActivationOutbox.objects.all().delete()
        ControlOperationNotification.objects.all().delete()
        for tenant in GuestHouseTenant.objects.exclude(schema_name='public'):
            tenant.delete(force_drop=True)
        super().tearDown()

    def payload(self, reference=None):
        return {
            'legal_or_trading_name': 'Example Lodge (Pty) Ltd',
            'tenant_display_name': 'Example Lodge',
            'product': 'guest-house', 'plan': 'starter', 'billing_cycle': 'monthly',
            'subscription_kind': 'trial',
            'trial_start': timezone.now().isoformat(),
            'trial_expiry': (timezone.now() + timedelta(days=14)).isoformat(),
            'next_billing_date': (timezone.localdate() + timedelta(days=14)).isoformat(),
            'primary_administrator': {
                'name': 'Primary Administrator', 'email': 'admin@example.invalid',
                'phone': '+27000000000', 'send_activation_invitation': True,
                'force_secure_password_creation': True,
            },
            'primary_contact': {'email': 'contact@example.invalid', 'phone': '+27000000001'},
            'timezone': 'Africa/Johannesburg', 'currency': 'ZAR', 'country': 'ZA',
            'primary_location': {
                'property_name': 'Example Lodge', 'property_type': 'lodge',
                'number_of_rooms': 8, 'physical_location': 'Johannesburg',
            },
            'initial_feature_flags': {},
            'external_smart_control_tenant_reference': str(reference or uuid.uuid4()),
        }

    def context(self):
        return SimpleNamespace(operation_id=str(uuid.uuid4()))

    def action_context(self, expected=None):
        return SimpleNamespace(
            operation_id=str(uuid.uuid4()), expected_before_state=expected or {},
            requested_by='owner@circlecore.co.za', reason='Approved tenant lifecycle test', approval_reference='approval-1',
        )

    def test_product_creates_schema_admin_subscription_property_and_outbox(self):
        result = GuestHouseProductControlBackend().execute('create_tenant', {}, self.payload(), self.context())
        self.assertEqual(result['http_status'], 201)
        tenant = GuestHouseTenant.objects.get(pk=result['data']['tenant_id'])
        with schema_context(tenant.schema_name):
            user = get_user_model().objects.get(pk=result['data']['administrator_id'])
            self.assertFalse(user.has_usable_password())
            self.assertTrue(user.is_superuser)
            self.assertTrue(StaffProfile.objects.filter(user=user, role='Owner').exists())
            self.assertEqual(str(Subscription.objects.get().pk), result['data']['subscription_id'])
            self.assertEqual(Property.objects.get().name, 'Example Lodge')
        self.assertEqual(ControlActivationOutbox.objects.get().state, 'pending')

    def test_logical_duplicate_is_rejected_without_second_schema(self):
        payload = self.payload()
        backend = GuestHouseProductControlBackend()
        backend.execute('create_tenant', {}, payload, self.context())
        with self.assertRaises(ControlAPIError) as caught:
            backend.execute('create_tenant', {}, payload, self.context())
        self.assertEqual(caught.exception.error_code, 'tenant_already_exists')
        self.assertEqual(GuestHouseTenant.objects.exclude(schema_name='public').count(), 1)

    def test_activation_outbox_dispatches_tenant_secure_password_link(self):
        GuestHouseProductControlBackend().execute('create_tenant', {}, self.payload(), self.context())
        call_command('dispatch_control_activations', stdout=StringIO())
        self.assertEqual(ControlActivationOutbox.objects.get().state, 'sent')
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('/password-reset/', mail.outbox[0].body)
        self.assertNotIn('password=', mail.outbox[0].body)

    def test_tenant_refresh_is_product_owned_and_mutations_fail_closed(self):
        backend = GuestHouseProductControlBackend()
        created = backend.execute('create_tenant', {}, self.payload(), self.context())
        current = backend.read('tenant', {'tenant_id': created['data']['tenant_id']}, None)
        self.assertEqual(current['tenant_id'], created['data']['tenant_id'])
        self.assertEqual(current['status'], 'trialing')
        self.assertTrue(backend.capabilities(None)['capabilities']['tenant_read'])
        self.assertTrue(backend.capabilities(None)['capabilities']['suspension'])

    def test_product_owned_trial_and_suspension_lifecycle(self):
        backend = GuestHouseProductControlBackend()
        created = backend.execute('create_tenant', {}, self.payload(), self.context())
        tenant_id = created['data']['tenant_id']
        with transaction.atomic():
            extended = backend.execute('extend_trial', {'tenant_id': tenant_id}, {'trial_days': 7}, self.action_context({'status': 'trialing'}))
            self.assertGreater(extended['after_state']['trial_expiry'], extended['before_state']['trial_expiry'])
            self.assertEqual(extended['data']['notification']['state'], 'queued')
            self.assertTrue(ControlOperationNotification.objects.filter(tenant_id=tenant_id, state='queued').exists())
            suspended = backend.execute('suspend_tenant', {'tenant_id': tenant_id}, {}, self.action_context({'status': 'trialing'}))
            self.assertEqual(suspended['after_state']['status'], 'suspended')
            restored = backend.execute('reactivate_tenant', {'tenant_id': tenant_id}, {}, self.action_context({'status': 'suspended'}))
        self.assertEqual(restored['after_state']['status'], 'trialing')

    def test_manual_payment_is_not_treated_as_verified(self):
        backend = GuestHouseProductControlBackend()
        created = backend.execute('create_tenant', {}, self.payload(), self.context())
        with transaction.atomic():
            result = backend.execute('manual_payment', {'tenant_id': created['data']['tenant_id']}, {
                'amount': '250.00', 'currency': 'ZAR', 'payment_date': timezone.localdate().isoformat(),
                'payment_method_category': 'eft', 'internal_reference': 'MANUAL-001', 'activate_after_payment': True,
            }, self.action_context({'status': 'trialing'}))
        self.assertEqual(result['data']['verification_status'], 'pending_verification')
        with schema_context(GuestHouseTenant.objects.get(pk=created['data']['tenant_id']).schema_name):
            self.assertEqual(Subscription.objects.get().status, 'trial')

    def test_identity_controls_and_product_entitlement_are_product_owned(self):
        backend = GuestHouseProductControlBackend()
        created = backend.execute('create_tenant', {}, self.payload(), self.context())
        tenant = GuestHouseTenant.objects.get(pk=created['data']['tenant_id'])
        identifiers = {'tenant_id': str(tenant.pk), 'user_id': created['data']['administrator_id']}
        with transaction.atomic():
            forced = backend.execute('force_password_reset', identifiers, {}, self.action_context({'status': 'trialing'}))
            self.assertTrue(forced['data']['force_password_reset'])
            disabled = backend.execute('disable_product', {'tenant_id': str(tenant.pk)}, {}, self.action_context({'status': 'trialing'}))
            self.assertFalse(disabled['after_state']['product_enabled'])
            enabled = backend.execute('enable_product', {'tenant_id': str(tenant.pk)}, {}, self.action_context({'status': 'trialing'}))
            self.assertTrue(enabled['after_state']['product_enabled'])
        with schema_context(tenant.schema_name):
            user = get_user_model().objects.get(pk=created['data']['administrator_id'])
            self.assertTrue(ControlUserSecurity.objects.get(user=user).force_password_reset)
            user.is_active = False
            user.save(update_fields=['is_active'])
        with transaction.atomic():
            unlocked = backend.execute('unlock_user', identifiers, {}, self.action_context({'status': 'trialing'}))
            self.assertTrue(unlocked['data']['account_unlocked'])
        with schema_context(tenant.schema_name):
            self.assertTrue(get_user_model().objects.get(pk=created['data']['administrator_id']).is_active)

    def test_safe_user_directory_and_support_actions_never_expose_credentials(self):
        backend = GuestHouseProductControlBackend()
        created = backend.execute('create_tenant', {}, self.payload(), self.context())
        tenant_id = created['data']['tenant_id']
        context = self.action_context({'status': 'trialing'})
        with transaction.atomic():
            invited = backend.execute('invite_user', {'tenant_id': tenant_id}, {
                'email': 'manager@example.invalid', 'full_name': 'Support Manager', 'role': 'Manager',
            }, context)
        user_id = invited['data']['product_user_id']
        with transaction.atomic(), self.assertRaises(ControlAPIError) as duplicate:
            backend.execute('invite_user', {'tenant_id': tenant_id}, {
                'email': 'manager@example.invalid', 'full_name': 'Duplicate Manager', 'role': 'Manager',
            }, self.action_context({'status': 'trialing'}))
        self.assertEqual(duplicate.exception.error_code, 'user_already_exists')
        directory = backend.read('tenant_users', {'tenant_id': tenant_id}, None)
        encoded = str(directory).lower()
        self.assertIn('m***@example.invalid', encoded)
        self.assertNotIn('password', {key for row in directory['results'] for key in row})
        self.assertNotIn('token', encoded)
        with transaction.atomic():
            self.assertEqual(backend.execute('disable_user', {'tenant_id': tenant_id, 'user_id': user_id}, {}, context)['data']['status'], 'disabled')
            self.assertEqual(backend.execute('reactivate_user', {'tenant_id': tenant_id, 'user_id': user_id}, {}, context)['data']['status'], 'active')
            self.assertEqual(backend.execute('change_user_role', {'tenant_id': tenant_id, 'user_id': user_id}, {'role': 'Reception'}, context)['data']['role'], 'Reception')
            reset = backend.execute('send_password_reset', {'tenant_id': tenant_id, 'user_id': user_id}, {}, context)
        self.assertNotIn('token', reset['data'])
        self.assertNotIn('url', reset['data'])
        with transaction.atomic(), self.assertRaises(ControlAPIError) as limited:
            backend.execute('send_password_reset', {'tenant_id': tenant_id, 'user_id': user_id}, {}, context)
        self.assertEqual(limited.exception.error_code, 'rate_limited')
