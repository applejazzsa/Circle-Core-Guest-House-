import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import connection
from django.utils import timezone
from django_tenants.utils import schema_context

from circle_core_control_api.backends import BaseProductControlBackend
from circle_core_control_api.errors import ControlAPIError

from .models import ControlActivationOutbox, ControlOperationNotification, Domain, GuestHouseTenant


def _mask_email(value):
    local, separator, domain = str(value or '').partition('@')
    return f'{local[:1]}***@{domain}' if separator else ''


def _datetime(value, field):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        raise ControlAPIError('validation_failed', f'{field} is invalid.', status=400)


def _date(value, field):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).date()
    except (TypeError, ValueError):
        raise ControlAPIError('validation_failed', f'{field} is invalid.', status=400)


def _positive_int(payload, field, maximum):
    try:
        value = int(payload.get(field))
    except (TypeError, ValueError):
        raise ControlAPIError('validation_failed', f'{field} is invalid.', status=400)
    if value < 1 or value > maximum:
        raise ControlAPIError('validation_failed', f'{field} must be between 1 and {maximum}.', status=422)
    return value


class GuestHouseProductControlBackend(BaseProductControlBackend):
    PLAN_METADATA = {
        'starter': {'name': 'Starter', 'monthly': '399.00', 'annual': '3830.00', 'rooms': 8, 'users': 1, 'trial': 14},
        'professional': {'name': 'Professional', 'monthly': '799.00', 'annual': '7670.00', 'rooms': 20, 'users': 5, 'trial': 30},
        'enterprise': {'name': 'Enterprise', 'monthly': '1499.00', 'annual': '14390.00', 'rooms': 1000, 'users': 1000, 'trial': 30},
    }

    def health(self, context):
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
        return {'status': 'healthy', 'product': 'guest-house'}

    def capabilities(self, context):
        return {
            'contract_version': '1.0',
            'product': 'guest-house',
            'capabilities': {
                'tenant_creation': True,
                'trials': True,
                'subscriptions': True,
                'manual_payments': True,
                'suspension': True,
                'account_unlocking': True,
                'session_revocation': True,
                'product_enablement': True,
                'branches': False,
                'properties': True,
                'multiple_locations': True,
                'background_job_retry': False,
                'maintenance_mode': False,
                'compensating_operations': True,
                'tenant_read': True,
                'tenant_activation': True,
                'trial_extension': True,
                'subscription_plan_change': True,
                'subscription_grace_period': True,
                'subscription_cancellation': True,
                'archiving': True,
                'administrator_invitations': True,
                'password_reset': True,
                'force_password_reset': True,
                'user_management': True,
                'user_role_management': True,
            },
            'max_trial_days': 30,
            'features': [
                'expenses', 'full_reports', 'export', 'hourly_bookings', 'weekly_bookings',
                'inventory', 'staff_roles', 'multi_property', 'custom_pdf_branding',
                'maintenance_requests', 'priority_support', 'spa',
            ],
            'setup_fields': [
                {'name': 'property_name', 'label': 'Property name', 'type': 'text', 'max_length': 200, 'required': True},
                {
                    'name': 'property_type', 'label': 'Property type', 'type': 'choice',
                    'choices': ['guest_house', 'hotel', 'lodge', 'bnb', 'self_catering'], 'required': True,
                },
                {'name': 'number_of_rooms', 'label': 'Number of rooms', 'type': 'integer', 'min': 1, 'max': 1000, 'required': True},
                {'name': 'physical_location', 'label': 'Physical location', 'type': 'text', 'max_length': 500, 'required': True},
            ],
            'plans': [
                {
                    'code': code, 'name': item['name'], 'standard': True,
                    'price': item['monthly'], 'max_trial_days': item['trial'],
                    'billing_cycles': ['monthly', 'annual'],
                }
                for code, item in self.PLAN_METADATA.items()
            ],
        }

    def read(self, resource, identifiers, context):
        if resource == 'plans':
            return {
                'currency': 'ZAR',
                'plans': [
                    {
                        'code': code, 'name': item['name'],
                        'prices': {'monthly': item['monthly'], 'annual': item['annual']},
                        'limits': {'rooms': item['rooms'], 'users': item['users']},
                        'max_trial_days': item['trial'], 'billing_cycles': ['monthly', 'annual'],
                    }
                    for code, item in self.PLAN_METADATA.items()
                ],
            }
        if resource == 'tenants':
            query = (context or {}).get('query', {})
            page_size = int(query.get('page_size', 50))
            queryset = GuestHouseTenant.objects.exclude(schema_name='public').order_by('pk')
            cursor = query.get('cursor')
            if cursor:
                try:
                    queryset = queryset.filter(pk__gt=uuid.UUID(str(cursor)))
                except (TypeError, ValueError, AttributeError):
                    raise ControlAPIError('validation_failed', 'cursor is invalid.', status=400)
            tenants = list(queryset[:page_size + 1])
            has_more = len(tenants) > page_size
            tenants = tenants[:page_size]
            results = [self._snapshot(tenant) for tenant in tenants]
            return {
                'results': results, 'count': len(results),
                'next_cursor': str(tenants[-1].pk) if has_more and tenants else None,
            }
        if resource == 'tenant_users':
            try:
                tenant = GuestHouseTenant.objects.get(pk=identifiers.get('tenant_id'))
            except (GuestHouseTenant.DoesNotExist, ValueError, TypeError):
                raise ControlAPIError('tenant_not_found', 'Tenant was not found.', status=404)
            with schema_context(tenant.schema_name):
                from core.models import ControlUserSecurity, StaffProfile
                sessions = {}
                for session in Session.objects.filter(expire_date__gte=timezone.now()):
                    try:
                        decoded = session.get_decoded()
                        if decoded.get('_circle_core_tenant_schema') == tenant.schema_name:
                            user_id = str(decoded.get('_auth_user_id') or '')
                            sessions[user_id] = sessions.get(user_id, 0) + 1
                    except Exception:
                        pass
                results = []
                for user in get_user_model().objects.select_related('staff_profile').order_by('first_name', 'last_name'):
                    profile = StaffProfile.objects.filter(user=user).first()
                    security = ControlUserSecurity.objects.filter(user=user).first()
                    invitation = ControlActivationOutbox.objects.filter(tenant=tenant, user_id=user.pk, kind__in=('activation', 'administrator_invitation')).order_by('-requested_at').first()
                    reset = ControlActivationOutbox.objects.filter(tenant=tenant, user_id=user.pk, kind='password_reset').order_by('-requested_at').first()
                    pin_locked = bool(profile and profile.pin_locked_until and profile.pin_locked_until > timezone.now())
                    successful = (security.last_successful_login if security else None) or user.last_login
                    results.append({
                        'product_user_id': str(user.pk), 'full_name': user.get_full_name() or user.username,
                        'masked_email': _mask_email(user.email), 'role': profile.role if profile else 'Viewer',
                        'status': 'active' if user.is_active else 'disabled',
                        'invitation_status': invitation.state if invitation else 'not_invited',
                        'last_successful_login': successful.isoformat() if successful else None,
                        'last_failed_login': security.last_failed_login.isoformat() if security and security.last_failed_login else None,
                        'failed_login_count': max(security.failed_login_count if security else 0, profile.pin_failed_attempts if profile else 0),
                        'lockout_state': 'locked' if (not user.is_active or pin_locked or (security and security.locked_at)) else 'unlocked',
                        'lockout_reason': (security.lock_reason if security else '') or ('PIN attempts exceeded' if pin_locked else ('Account disabled' if not user.is_active else '')),
                        'mfa_state': 'not_supported', 'active_session_count': sessions.get(str(user.pk), 0),
                        'password_reset_required': bool(security and security.force_password_reset),
                        'account_created_at': user.date_joined.isoformat(),
                        'last_password_reset_request': reset.requested_at.isoformat() if reset else None,
                        'product': 'guest-house',
                    })
                return {'results': results, 'count': len(results),
                        'available_roles': ['Owner', 'Manager', 'Reception', 'Cleaner', 'Viewer']}
        if resource != 'tenant':
            return super().read(resource, identifiers, context)
        tenant_id = identifiers.get('tenant_id')
        try:
            tenant = GuestHouseTenant.objects.get(pk=tenant_id)
        except (GuestHouseTenant.DoesNotExist, ValueError, TypeError):
            raise ControlAPIError('tenant_not_found', 'Tenant was not found.', status=404)
        return self._snapshot(tenant)

    def execute(self, action, identifiers, payload, context):
        if action != 'create_tenant':
            return self._execute_tenant_action(action, identifiers, payload, context)
        if payload.get('product') not in {'guest-house', 'guest_house'}:
            raise ControlAPIError('validation_failed', 'The requested product does not match Guest House.', status=400)
        try:
            external_reference = uuid.UUID(str(payload['external_smart_control_tenant_reference']))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ControlAPIError('validation_failed', 'Smart Control tenant reference is invalid.', status=400)
        existing = GuestHouseTenant.objects.filter(smart_control_reference=external_reference).first()
        if existing:
            current = {'tenant_id': str(existing.pk)}
            with schema_context(existing.schema_name):
                from core.models import Subscription
                subscription = Subscription.objects.first()
                if subscription:
                    current['subscription_id'] = str(subscription.pk)
            raise ControlAPIError(
                'tenant_already_exists', 'This Smart Control tenant is already provisioned.',
                status=409, current_state=current,
            )
        plan_code = str(payload.get('plan', '')).lower()
        plan_metadata = self.PLAN_METADATA.get(plan_code)
        if not plan_metadata:
            raise ControlAPIError('invalid_plan', 'The selected Guest House plan is not supported.', status=422)
        billing_cycle = str(payload.get('billing_cycle', '')).lower()
        if billing_cycle not in {'monthly', 'annual'}:
            raise ControlAPIError('validation_failed', 'The selected billing cycle is not supported.', status=422)
        setup = payload.get('primary_location', {})
        if not isinstance(setup, dict):
            raise ControlAPIError('validation_failed', 'Property setup is invalid.', status=400)
        property_name = str(setup.get('property_name', '')).strip()
        property_type = str(setup.get('property_type', '')).strip()
        location = str(setup.get('physical_location', '')).strip()
        try:
            room_count = int(setup.get('number_of_rooms'))
        except (TypeError, ValueError):
            room_count = 0
        if not property_name or not location or property_type not in {'guest_house', 'hotel', 'lodge', 'bnb', 'self_catering'}:
            raise ControlAPIError('validation_failed', 'Complete valid property setup is required.', status=422)
        if room_count < 1 or room_count > plan_metadata['rooms']:
            raise ControlAPIError('plan_limit_exceeded', 'Room count exceeds the selected plan limit.', status=422)
        administrator = payload.get('primary_administrator', {})
        owner_name = str(administrator.get('name', '')).strip()
        owner_email = str(administrator.get('email', '')).strip().casefold()
        owner_phone = str(administrator.get('phone', '')).strip()
        if not owner_name or not owner_email:
            raise ControlAPIError('validation_failed', 'Administrator name and email are required.', status=400)
        if GuestHouseTenant.objects.filter(owner_email__iexact=owner_email).exists():
            raise ControlAPIError('administrator_already_exists', 'The administrator email already exists.', status=409)
        subscription_kind = payload.get('subscription_kind')
        trial_expiry = _datetime(payload.get('trial_expiry'), 'trial_expiry') if subscription_kind == 'trial' else None
        if subscription_kind == 'trial' and (not trial_expiry or trial_expiry <= timezone.now()):
            raise ControlAPIError('validation_failed', 'Trial expiry must be in the future.', status=422)
        next_billing_date = payload.get('next_billing_date')
        if subscription_kind == 'paid':
            if next_billing_date:
                try:
                    billing_date = datetime.fromisoformat(str(next_billing_date)).date()
                    expires_at = timezone.make_aware(datetime.combine(billing_date, time.max))
                except (TypeError, ValueError):
                    raise ControlAPIError('validation_failed', 'Next billing date is invalid.', status=422)
            else:
                expires_at = timezone.now() + timedelta(days=365 if billing_cycle == 'annual' else 30)
        else:
            expires_at = trial_expiry
        schema_name = f'scc_{external_reference.hex[:20]}'
        domain_name = f'scc-{external_reference.hex[:20]}.{settings.BASE_DOMAIN}'
        tenant = GuestHouseTenant.objects.create(
            schema_name=schema_name,
            smart_control_reference=external_reference,
            name=str(payload.get('tenant_display_name', ''))[:200],
            owner_name=owner_name[:200], owner_email=owner_email,
            owner_phone=owner_phone[:20], is_active=True, is_verified=False,
        )
        Domain.objects.create(domain=domain_name, tenant=tenant, is_primary=True)
        with schema_context(schema_name):
            from core.models import GuestHouseSettings, Property, StaffProfile, Subscription, SubscriptionPlan
            User = get_user_model()
            first_name, _, last_name = owner_name.partition(' ')
            user = User.objects.create_user(
                username=owner_email[:150], email=owner_email,
                first_name=first_name[:150], last_name=last_name[:150], password=None,
                is_staff=True, is_superuser=True,
            )
            user.set_unusable_password()
            user.save(update_fields=['password'])
            owner_group, _ = Group.objects.get_or_create(name='Owner')
            user.groups.add(owner_group)
            StaffProfile.objects.update_or_create(
                user=user,
                defaults={'phone_number': owner_phone or None, 'role': 'Owner'},
            )
            plan = SubscriptionPlan.objects.get(name=plan_code)
            subscription = Subscription.objects.create(
                plan=plan, billing_cycle=billing_cycle,
                status='trial' if subscription_kind == 'trial' else 'active',
                trial_ends_at=trial_expiry, expires_at=expires_at,
                next_billing_date=expires_at.date(), owner_name=owner_name,
                owner_email=owner_email, owner_phone=owner_phone,
            )
            property_row = Property.objects.order_by('pk').first()
            property_values = {
                'name': property_name[:200], 'address': location[:500],
                'description': f'{property_type.replace("_", " ").title()} · {room_count} planned rooms',
                'email': owner_email, 'phone': owner_phone,
            }
            if property_row:
                for field, value in property_values.items():
                    setattr(property_row, field, value)
                property_row.save(update_fields=list(property_values))
            else:
                property_row = Property.objects.create(**property_values)
            GuestHouseSettings.objects.update_or_create(
                pk=1,
                defaults={
                    'guest_house_name': tenant.name, 'phone': owner_phone,
                    'email': owner_email, 'address': location[:500],
                    'currency': payload.get('currency', 'ZAR'),
                },
            )
        outbox = None
        if administrator.get('send_activation_invitation', True):
            outbox = ControlActivationOutbox.objects.create(
                tenant=tenant, user_id=user.pk, recipient=owner_email,
            )
        return {
            'result': 'success', 'http_status': 201,
            'before_state': {},
            'after_state': {
                'status': 'trialing' if subscription_kind == 'trial' else 'active',
                'subscription_state': 'trial' if subscription_kind == 'trial' else 'active',
                'product_enabled': True,
            },
            'data': {
                'tenant_id': str(tenant.pk), 'administrator_id': str(user.pk),
                'subscription_id': str(subscription.pk),
                'activation_email_status': 'queued' if outbox else 'not_requested',
                'notification_id': str(outbox.pk) if outbox else '',
                'created_resources': [
                    {'step': 'tenant', 'type': 'tenant', 'id': str(tenant.pk), 'label': tenant.name},
                    {'step': 'property', 'type': 'property', 'id': str(property_row.pk), 'label': property_name},
                    {'step': 'administrator', 'type': 'administrator', 'id': str(user.pk), 'label': 'Initial owner'},
                    {'step': 'subscription', 'type': 'subscription', 'id': str(subscription.pk), 'label': plan.display_name},
                ],
            },
            'reversible': True, 'compensating_action': 'archive_tenant',
            'audit_metadata': {'schema_created': True, 'plan': plan_code, 'billing_cycle': billing_cycle},
        }

    def _snapshot(self, tenant):
        with schema_context(tenant.schema_name):
            from core.models import Property, Subscription
            subscription = Subscription.objects.select_related('plan').order_by('-pk').first()
            status = subscription.status if subscription else ('active' if tenant.is_active else 'suspended')
            if tenant.archived_at:
                status = 'archived'
            elif subscription and subscription.control_grace_ends_at and subscription.control_grace_ends_at >= timezone.now() and subscription.status == 'expired':
                status = 'grace_period'
            elif status == 'trial':
                status = 'trialing'
            return {
                'tenant_id': str(tenant.pk), 'name': tenant.name, 'status': status,
                'subscription_state': status, 'plan': subscription.plan.name if subscription and subscription.plan else '',
                'trial_expiry': subscription.trial_ends_at.isoformat() if subscription and subscription.trial_ends_at else None,
                'next_billing_date': subscription.next_billing_date.isoformat() if subscription and subscription.next_billing_date else None,
                'grace_ends_at': subscription.control_grace_ends_at.isoformat() if subscription and subscription.control_grace_ends_at else None,
                'product_enabled': bool(tenant.is_active and tenant.product_access_enabled and not tenant.archived_at),
                'user_count': get_user_model().objects.count(), 'location_count': Property.objects.count(),
            }

    def _expected(self, tenant, expected):
        current = self._snapshot(tenant)
        aliases = {'status': 'status', 'subscription_state': 'subscription_state', 'plan': 'plan'}
        stale = {key: {'expected': value, 'actual': current[aliases[key]]} for key, value in (expected or {}).items()
                 if key in aliases and value not in (None, '') and str(value) != str(current[aliases[key]])}
        if stale:
            raise ControlAPIError('stale_before_state', 'Tenant state changed after confirmation.', status=409, current_state=current)
        return current

    def _execute_tenant_action(self, action, identifiers, payload, context):
        try:
            tenant = GuestHouseTenant.objects.select_for_update().get(pk=identifiers.get('tenant_id'))
        except (GuestHouseTenant.DoesNotExist, ValueError, TypeError):
            raise ControlAPIError('tenant_not_found', 'Tenant was not found.', status=404)
        before = self._expected(tenant, context.expected_before_state)
        supported = {
            'extend_trial', 'activate_tenant', 'suspend_tenant', 'reactivate_tenant',
            'apply_grace_period', 'change_plan', 'manual_payment', 'cancel_subscription',
            'archive_tenant', 'enable_product', 'disable_product', 'invite_admin', 'send_password_reset',
            'force_password_reset', 'unlock_user', 'revoke_sessions', 'invite_user', 'disable_user',
            'reactivate_user', 'change_user_role',
        }
        if action not in supported:
            return super().execute(action, identifiers, payload, context)
        data, reversible, compensation = {}, False, None
        with schema_context(tenant.schema_name):
            from core.models import ControlManualPayment, ControlUserSecurity, OfflineDevice, Property, Room, StaffProfile, Subscription, SubscriptionPlan
            subscription = Subscription.objects.select_for_update().select_related('plan').order_by('-pk').first()
            if not subscription:
                raise ControlAPIError('subscription_not_found', 'Tenant subscription was not found.', status=409)

            if action == 'extend_trial':
                if subscription.status != 'trial' or tenant.archived_at:
                    raise ControlAPIError('invalid_state', 'Only a current trial can be extended.', status=409, current_state=before)
                days = _positive_int(payload, 'trial_days', 90)
                anchor = max(subscription.trial_ends_at or timezone.now(), timezone.now())
                subscription.trial_ends_at = anchor + timedelta(days=days)
                subscription.expires_at = max(subscription.expires_at, subscription.trial_ends_at)
                subscription.save(update_fields=['trial_ends_at', 'expires_at'])
                data = {'trial_expiry': subscription.trial_ends_at.isoformat()}
            elif action == 'activate_tenant':
                if subscription.status not in {'trial', 'expired', 'cancelled'} and not tenant.archived_at:
                    raise ControlAPIError('invalid_state', 'Tenant cannot be activated from its current state.', status=409, current_state=before)
                tenant.is_active, tenant.archived_at = True, None
                tenant.product_access_enabled = True
                tenant.save(update_fields=['is_active', 'archived_at', 'product_access_enabled'])
                subscription.status = 'active'
                if subscription.expires_at <= timezone.now():
                    subscription.expires_at = timezone.now() + timedelta(days=30)
                subscription.control_grace_ends_at = None
                subscription.save(update_fields=['status', 'expires_at', 'control_grace_ends_at'])
                reversible, compensation = True, 'suspend_tenant'
            elif action == 'suspend_tenant':
                if tenant.archived_at or subscription.status in {'suspended', 'cancelled'}:
                    raise ControlAPIError('invalid_state', 'Tenant cannot be suspended from its current state.', status=409, current_state=before)
                tenant.control_previous_subscription_status = subscription.status
                tenant.save(update_fields=['control_previous_subscription_status'])
                subscription.status, subscription.control_grace_ends_at = 'suspended', None
                subscription.save(update_fields=['status', 'control_grace_ends_at'])
                reversible, compensation = True, 'reactivate_tenant'
            elif action == 'reactivate_tenant':
                if subscription.status != 'suspended' or tenant.archived_at:
                    raise ControlAPIError('invalid_state', 'Only a suspended tenant can be reactivated.', status=409, current_state=before)
                restored = tenant.control_previous_subscription_status
                if restored not in {'trial', 'active', 'expired'}:
                    restored = 'active'
                if restored == 'trial' and (not subscription.trial_ends_at or subscription.trial_ends_at < timezone.now()):
                    restored = 'active'
                subscription.status = restored
                subscription.save(update_fields=['status'])
                tenant.control_previous_subscription_status = ''
                tenant.save(update_fields=['control_previous_subscription_status'])
                reversible, compensation = True, 'suspend_tenant'
            elif action == 'apply_grace_period':
                days = _positive_int(payload, 'grace_days', 30)
                if tenant.archived_at or subscription.status in {'suspended', 'cancelled'}:
                    raise ControlAPIError('invalid_state', 'Grace period cannot be applied in the current state.', status=409, current_state=before)
                subscription.status = 'expired'
                subscription.control_grace_ends_at = timezone.now() + timedelta(days=days)
                subscription.save(update_fields=['status', 'control_grace_ends_at'])
                data = {'grace_ends_at': subscription.control_grace_ends_at.isoformat()}
            elif action == 'change_plan':
                effective = _datetime(payload.get('effective_at'), 'effective_at')
                if effective > timezone.now():
                    raise ControlAPIError('scheduling_not_supported', 'Future product-side plan changes are not supported.', status=422)
                plan_code = str(payload.get('plan_code', '')).lower()
                try:
                    plan = SubscriptionPlan.objects.get(name=plan_code)
                except SubscriptionPlan.DoesNotExist:
                    raise ControlAPIError('invalid_plan', 'The selected Guest House plan is not supported.', status=422)
                usage = {'rooms': Room.objects.count(), 'users': get_user_model().objects.count(), 'properties': Property.objects.count()}
                conflicts = {}
                if usage['rooms'] > plan.max_rooms:
                    conflicts['rooms'] = {'used': usage['rooms'], 'limit': plan.max_rooms}
                if usage['users'] > plan.max_users:
                    conflicts['users'] = {'used': usage['users'], 'limit': plan.max_users}
                if not plan.feature_multi_property and usage['properties'] > 1:
                    conflicts['properties'] = {'used': usage['properties'], 'limit': 1}
                if conflicts:
                    raise ControlAPIError('plan_limits_exceeded', 'Current tenant usage exceeds the requested plan limits.', status=409, current_state={**before, 'limit_conflicts': conflicts})
                subscription.plan = plan
                subscription.save(update_fields=['plan'])
            elif action == 'manual_payment':
                try:
                    amount = Decimal(str(payload.get('amount')))
                except (InvalidOperation, TypeError):
                    raise ControlAPIError('validation_failed', 'amount is invalid.', status=400)
                currency, reference = str(payload.get('currency', '')).upper(), str(payload.get('internal_reference', '')).strip()
                method = str(payload.get('payment_method_category', '')).strip()
                coverage_start, coverage_end = _date(payload.get('coverage_start'), 'coverage_start'), _date(payload.get('coverage_end'), 'coverage_end')
                evidence = payload.get('evidence_metadata') or {}
                if amount <= 0 or len(currency) != 3 or not currency.isalpha() or not reference or not method or not _date(payload.get('payment_date'), 'payment_date'):
                    raise ControlAPIError('validation_failed', 'Payment amount, currency, and internal reference are required.', status=422)
                if coverage_start and coverage_end and coverage_end < coverage_start:
                    raise ControlAPIError('validation_failed', 'Coverage end cannot precede coverage start.', status=422)
                if not isinstance(evidence, dict):
                    raise ControlAPIError('validation_failed', 'evidence_metadata must be an object.', status=400)
                if ControlManualPayment.objects.filter(internal_reference=reference[:100]).exists():
                    raise ControlAPIError('payment_reference_exists', 'The manual payment reference already exists.', status=409)
                payment = ControlManualPayment.objects.create(
                    subscription=subscription, amount=amount, currency=currency,
                    payment_date=_date(payload.get('payment_date'), 'payment_date'), payment_method_category=method[:40],
                    internal_reference=reference[:100], invoice_reference=str(payload.get('invoice_reference', ''))[:100],
                    coverage_start=coverage_start, coverage_end=coverage_end,
                    notes=str(payload.get('notes', '')), evidence_metadata=evidence,
                    activate_after_payment=bool(payload.get('activate_after_payment')),
                    next_billing_date=_date(payload.get('next_billing_date'), 'next_billing_date'),
                    recorded_by=context.requested_by, operation_id=context.operation_id,
                )
                data = {'payment_id': str(payment.id), 'payment_reference': str(payment.id), 'payment_status': payment.status,
                        'verification_status': payment.status, 'activation_deferred_until_verified': payment.activate_after_payment}
            elif action == 'cancel_subscription':
                effective = _datetime(payload.get('effective_at'), 'effective_at')
                if effective > timezone.now():
                    raise ControlAPIError('scheduling_not_supported', 'Future product-side cancellation is not supported.', status=422)
                subscription.status, subscription.auto_renew, subscription.control_grace_ends_at = 'cancelled', False, None
                subscription.save(update_fields=['status', 'auto_renew', 'control_grace_ends_at'])
            elif action == 'archive_tenant':
                tenant.control_previous_subscription_status = subscription.status
                tenant.is_active, tenant.archived_at = False, timezone.now()
                tenant.product_access_enabled = False
                tenant.save(update_fields=['control_previous_subscription_status', 'is_active', 'archived_at', 'product_access_enabled'])
                subscription.status, subscription.auto_renew, subscription.control_grace_ends_at = 'cancelled', False, None
                subscription.save(update_fields=['status', 'auto_renew', 'control_grace_ends_at'])
                reversible, compensation = True, 'activate_tenant'
            elif action in {'enable_product', 'disable_product'}:
                if tenant.archived_at:
                    raise ControlAPIError('invalid_state', 'Product access cannot change while the tenant is archived.', status=409, current_state=before)
                tenant.product_access_enabled = action == 'enable_product'
                tenant.save(update_fields=['product_access_enabled'])
                reversible = True
                compensation = 'disable_product' if action == 'enable_product' else 'enable_product'
            elif action == 'invite_user':
                if tenant.archived_at:
                    raise ControlAPIError('invalid_state', 'Users cannot be invited to an archived tenant.', status=409, current_state=before)
                email = str(payload.get('email', '')).strip().casefold()
                full_name = str(payload.get('full_name', '')).strip()
                role = str(payload.get('role', '')).strip()
                try:
                    validate_email(email)
                except ValidationError:
                    raise ControlAPIError('validation_failed', 'A valid email address is required.', status=422)
                if not full_name or role != 'Manager':
                    raise ControlAPIError('validation_failed', 'A name and the supported administrator role are required.', status=422)
                if get_user_model().objects.filter(email__iexact=email).exists():
                    raise ControlAPIError('user_already_exists', 'A user with this email already exists.', status=409)
                first_name, _, last_name = full_name.partition(' ')
                user = get_user_model().objects.create_user(username=email, email=email, first_name=first_name[:150], last_name=last_name[:150], password=None, is_staff=True)
                user.set_unusable_password()
                user.save(update_fields=['password'])
                StaffProfile.objects.update_or_create(user=user, defaults={'role': 'Manager'})
                message = ControlActivationOutbox.objects.create(tenant=tenant, user_id=user.pk, recipient=email, kind='administrator_invitation')
                data = {'product_user_id': str(user.pk), 'delivery_status': 'queued', 'notification_id': str(message.pk)}
            else:
                try:
                    user = get_user_model().objects.get(pk=identifiers.get('user_id') or payload.get('user_reference'))
                except (get_user_model().DoesNotExist, ValueError, TypeError):
                    raise ControlAPIError('user_not_found', 'The user was not found in this tenant.', status=404)
                if action == 'invite_admin' and not (user.is_staff or user.is_superuser):
                    raise ControlAPIError('invalid_user_role', 'Only tenant administrators can receive administrator invitations.', status=409)
                if action in {'invite_admin', 'send_password_reset'}:
                    kind = 'administrator_invitation' if action == 'invite_admin' else 'password_reset'
                    message, created = ControlActivationOutbox.objects.get_or_create(tenant=tenant, user_id=user.pk, kind=kind, defaults={'recipient': user.email})
                    if action == 'send_password_reset' and not created and message.requested_at >= timezone.now() - timedelta(minutes=5):
                        raise ControlAPIError('rate_limited', 'A password reset was requested recently. Try again later.', status=429, retryable=True)
                    message.recipient, message.state, message.attempts, message.last_error_code, message.sent_at = user.email, 'pending', 0, '', None
                    message.save()
                    data = {'notification_id': str(message.id), 'delivery_status': 'queued'}
                    if action == 'send_password_reset':
                        timeout = int(getattr(settings, 'PASSWORD_RESET_TIMEOUT', 259200))
                        data.update({'accepted': True, 'token_expires_at': (timezone.now() + timedelta(seconds=timeout)).isoformat()})
                elif action == 'force_password_reset':
                    security, _ = ControlUserSecurity.objects.get_or_create(user=user)
                    security.force_password_reset = True
                    security.password_hash_at_force = user.password
                    security.forced_at = timezone.now()
                    security.save(update_fields=['force_password_reset', 'password_hash_at_force', 'forced_at', 'updated_at'])
                    data = {'force_password_reset': True}
                elif action == 'unlock_user':
                    user.is_active = True
                    user.save(update_fields=['is_active'])
                    profile = StaffProfile.objects.filter(user=user).first()
                    if profile:
                        profile.pin_failed_attempts = 0
                        profile.pin_locked_until = None
                        profile.save(update_fields=['pin_failed_attempts', 'pin_locked_until', 'updated_at'])
                    security, _ = ControlUserSecurity.objects.get_or_create(user=user)
                    security.locked_at = None
                    security.lock_reason = ''
                    security.unlocked_at = timezone.now()
                    security.save(update_fields=['locked_at', 'lock_reason', 'unlocked_at', 'updated_at'])
                    revoked = 0
                    if payload.get('revoke_sessions_on_unlock'):
                        for session in Session.objects.filter(expire_date__gte=timezone.now()):
                            try:
                                decoded = session.get_decoded()
                                matches = str(decoded.get('_auth_user_id')) == str(user.pk) and decoded.get('_circle_core_tenant_schema') == tenant.schema_name
                            except Exception:
                                matches = False
                            if matches:
                                session.delete()
                                revoked += 1
                    data = {'account_unlocked': True, 'revoked_session_count': revoked,
                            'legacy_untagged_sessions_skipped': bool(payload.get('revoke_sessions_on_unlock'))}
                elif action == 'disable_user':
                    profile = StaffProfile.objects.filter(user=user).first()
                    if profile and profile.role == 'Owner' and not StaffProfile.objects.filter(role='Owner').exclude(user=user).exists():
                        raise ControlAPIError('last_owner', 'The tenant\'s last owner cannot be disabled.', status=409)
                    user.is_active = False
                    user.save(update_fields=['is_active'])
                    security, _ = ControlUserSecurity.objects.get_or_create(user=user)
                    security.locked_at, security.lock_reason = timezone.now(), 'Disabled by authorized support operation'
                    security.save(update_fields=['locked_at', 'lock_reason', 'updated_at'])
                    data = {'status': 'disabled'}
                elif action == 'reactivate_user':
                    user.is_active = True
                    user.save(update_fields=['is_active'])
                    security, _ = ControlUserSecurity.objects.get_or_create(user=user)
                    security.locked_at, security.lock_reason, security.unlocked_at = None, '', timezone.now()
                    security.save(update_fields=['locked_at', 'lock_reason', 'unlocked_at', 'updated_at'])
                    data = {'status': 'active'}
                elif action == 'change_user_role':
                    role = str(payload.get('role', '')).strip()
                    allowed = {'Owner', 'Manager', 'Reception', 'Cleaner', 'Viewer'}
                    if role not in allowed:
                        raise ControlAPIError('validation_failed', 'The selected role is not supported.', status=422)
                    profile, _ = StaffProfile.objects.get_or_create(user=user)
                    if profile.role == 'Owner' and role != 'Owner' and not StaffProfile.objects.filter(role='Owner').exclude(user=user).exists():
                        raise ControlAPIError('last_owner', 'The tenant must retain at least one owner.', status=409)
                    profile.role = role
                    profile.save(update_fields=['role', 'updated_at'])
                    user.is_staff = role in {'Owner', 'Manager'}
                    user.save(update_fields=['is_staff'])
                    data = {'role': role}
                else:
                    revoked = 0
                    for session in Session.objects.filter(expire_date__gte=timezone.now()):
                        try:
                            decoded = session.get_decoded()
                            matches = (
                                str(decoded.get('_auth_user_id')) == str(user.pk)
                                and decoded.get('_circle_core_tenant_schema') == tenant.schema_name
                            )
                        except Exception:
                            matches = False
                        if matches:
                            session.delete()
                            revoked += 1
                    offline = OfflineDevice.objects.filter(user=user, is_active=True).update(is_active=False, revoked_at=timezone.now())
                    data = {'revoked_session_count': revoked, 'revoked_offline_device_count': offline,
                            'legacy_untagged_sessions_skipped': True}

        after = self._snapshot(tenant)
        notify_actions = {'extend_trial', 'activate_tenant', 'suspend_tenant', 'reactivate_tenant', 'apply_grace_period',
                          'change_plan', 'cancel_subscription', 'archive_tenant', 'enable_product', 'disable_product'}
        if action in notify_actions:
            behavior = str(payload.get('notification_rule', 'product_default'))
            state = 'suppressed' if behavior == 'suppress' or not tenant.owner_email else 'queued'
            notification, _ = ControlOperationNotification.objects.update_or_create(
                operation_id=context.operation_id,
                defaults={'tenant': tenant, 'action': action, 'recipient': tenant.owner_email,
                          'behavior': behavior, 'state': state, 'safe_payload': {'action': action, 'tenant': tenant.name}},
            )
            data['notification'] = {'state': 'not_requested' if state == 'suppressed' else state, 'reference': str(notification.id)}
        return {'result': 'success', 'before_state': before, 'after_state': after, 'data': data,
                'reversible': reversible, 'compensating_action': compensation,
                'audit_metadata': {'reason': context.reason, 'approval_reference': context.approval_reference,
                                   'notification_rule': payload.get('notification_rule', 'product_default')}}
