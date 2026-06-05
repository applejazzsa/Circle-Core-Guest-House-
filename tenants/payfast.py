"""PayFast payment gateway utilities for Circle Core subscription billing."""

import hashlib
import logging
import urllib.parse

import urllib.request

from django.conf import settings

from core.subscriptions import next_renewal_datetime

logger = logging.getLogger(__name__)

PAYFAST_SANDBOX_URL = 'https://sandbox.payfast.co.za/eng/process'
PAYFAST_LIVE_URL = 'https://www.payfast.co.za/eng/process'
PAYFAST_SANDBOX_VALIDATE = 'https://sandbox.payfast.co.za/eng/query/validate'
PAYFAST_LIVE_VALIDATE = 'https://www.payfast.co.za/eng/query/validate'


def get_payfast_url():
    if getattr(settings, 'PAYFAST_SANDBOX', True):
        return PAYFAST_SANDBOX_URL
    return PAYFAST_LIVE_URL


def get_validate_url():
    if getattr(settings, 'PAYFAST_SANDBOX', True):
        return PAYFAST_SANDBOX_VALIDATE
    return PAYFAST_LIVE_VALIDATE


def generate_signature(data: dict, passphrase: str = '') -> str:
    """Generate PayFast MD5 signature. Field order in data dict must match form order."""
    fields = []
    for key, value in data.items():
        if value != '' and value is not None and key != 'signature':
            fields.append(f'{key}={urllib.parse.quote_plus(str(value).strip())}')
    query = '&'.join(fields)
    if passphrase:
        query += f'&passphrase={urllib.parse.quote_plus(passphrase.strip())}'
    return hashlib.md5(query.encode('utf-8')).hexdigest()


def build_payment_data(subscription, schema_name: str) -> dict:
    """Build the ordered PayFast payment form data for a subscription."""
    plan = subscription.plan
    tenant_url = f'https://{schema_name}.{settings.BASE_DOMAIN}'
    is_annual = subscription.billing_cycle == 'annual'
    amount = plan.annual_price if is_annual else plan.monthly_price
    frequency = '6' if is_annual else '3'
    billing_date = next_renewal_datetime(subscription.billing_cycle).date().isoformat()

    name_parts = subscription.owner_name.strip().split(None, 1)
    name_first = name_parts[0]
    name_last = name_parts[1] if len(name_parts) > 1 else ''

    # Field order matters for PayFast signature — do not rearrange
    data = {
        'merchant_id': settings.PAYFAST_MERCHANT_ID,
        'merchant_key': settings.PAYFAST_MERCHANT_KEY,
        'return_url': f'{tenant_url}/subscription/payment/success/',
        'cancel_url': f'{tenant_url}/subscription/expired/',
        'notify_url': f'{tenant_url}/payfast/itn/',
        'name_first': name_first,
        'name_last': name_last,
        'email_address': subscription.owner_email,
        'm_payment_id': f'SUB-{schema_name}-{subscription.pk}',
        'amount': f'{amount:.2f}',
        'item_name': f'Circle Core {plan.display_name}',
        'item_description': f'{subscription.get_billing_cycle_display()} subscription - {plan.display_name} plan',
        'subscription_type': '1',
        'billing_date': billing_date,
        'recurring_amount': f'{amount:.2f}',
        'frequency': frequency,
        'cycles': '0',
    }

    passphrase = getattr(settings, 'PAYFAST_PASSPHRASE', '')
    data['signature'] = generate_signature(data, passphrase)
    return data


def verify_itn_signature(post_data: dict) -> bool:
    """Verify the ITN POST signature from PayFast."""
    data = {k: v for k, v in post_data.items() if k != 'signature'}
    passphrase = getattr(settings, 'PAYFAST_PASSPHRASE', '')
    expected = generate_signature(data, passphrase)
    received = post_data.get('signature', '')
    if expected != received:
        logger.warning('PayFast ITN signature mismatch. Expected %s, got %s', expected, received)
        return False
    return True


def validate_itn_with_payfast(post_data: dict) -> bool:
    """Do a server-to-server validation of the ITN with PayFast."""
    try:
        body = urllib.parse.urlencode(post_data).encode('utf-8')
        req = urllib.request.Request(get_validate_url(), data=body)
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        with urllib.request.urlopen(req, timeout=10) as resp:
            response = resp.read().decode('utf-8').strip()
        return response == 'VALID'
    except Exception as exc:
        logger.error('PayFast ITN validation request failed: %s', exc)
        return False
