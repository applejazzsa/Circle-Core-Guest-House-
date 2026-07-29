import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.core.mail import send_mail
from django.db import connection, transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags
from django.utils.text import slugify
from django_tenants.utils import schema_context

from .models import Domain, GuestHouseTenant
from core.subscriptions import apply_paid_renewal, trial_end


def _get_client_ip(request) -> str:
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', 'unknown')


def _is_rate_limited(ip: str, limit: int = 5, window: int = 3600) -> bool:
    """Allow at most `limit` registrations per IP per `window` seconds."""
    key = f'reg_rl_{ip}'
    count = cache.get(key, 0)
    if count >= limit:
        return True
    cache.set(key, count + 1, timeout=window)
    return False


def _cleanup_failed_tenant(schema_name: str) -> None:
    """Remove orphaned tenant + schema left by a failed registration."""
    try:
        tenant = GuestHouseTenant.objects.filter(schema_name=schema_name).first()
        if tenant:
            tenant.delete(allow_hard_delete=True)
        logger.info('Cleaned up failed tenant schema: %s', schema_name)
    except Exception as exc:
        logger.error('Could not clean up tenant schema %s: %s', schema_name, exc)

logger = logging.getLogger(__name__)

PLANS = [
    {
        'key': 'starter',
        'name': 'Starter',
        'price': 399,
        'description': 'Perfect for small guest houses',
        'limit': 'Up to 8 rooms · 1 user',
    },
    {
        'key': 'professional',
        'name': 'Professional',
        'price': 799,
        'description': 'For growing properties',
        'limit': 'Up to 20 rooms · 5 users',
    },
    {
        'key': 'enterprise',
        'name': 'Enterprise',
        'price': 1499,
        'description': 'Large properties & multi-room',
        'limit': 'Unlimited rooms & users',
    },
]


# ── Public pages ─────────────────────────────────────────────────────────────

def pwa_manifest(request):
    return JsonResponse({
        'name': 'Circle Core Guest House',
        'short_name': 'Circle Core',
        'description': 'Guest house management for bookings, rooms, housekeeping, POS, and reporting.',
        'start_url': '/',
        'scope': '/',
        'display': 'standalone',
        'display_override': ['window-controls-overlay', 'standalone', 'browser'],
        'background_color': '#0d0f12',
        'theme_color': '#13161b',
        'orientation': 'any',
        'categories': ['business', 'productivity'],
        'icons': [
            {
                'src': '/static/icons/circle-core-icon-192.png',
                'sizes': '192x192',
                'type': 'image/png',
                'purpose': 'any maskable',
            },
            {
                'src': '/static/icons/circle-core-icon-512.png',
                'sizes': '512x512',
                'type': 'image/png',
                'purpose': 'any maskable',
            },
            {
                'src': '/static/icons/circle-core-icon.svg',
                'sizes': 'any',
                'type': 'image/svg+xml',
                'purpose': 'any maskable',
            },
        ],
        'shortcuts': [
            {'name': 'Dashboard', 'url': '/', 'description': 'Open the dashboard'},
            {'name': 'Rooms', 'url': '/rooms/', 'description': 'View rooms'},
            {'name': 'New Booking', 'url': '/bookings/add/', 'description': 'Create a booking'},
            {'name': 'POS', 'url': '/pos/terminal/', 'description': 'Open point of sale'},
        ],
    }, content_type='application/manifest+json')


def service_worker(request):
    script = """
const CACHE_NAME = 'circle-core-shell-v6';
const CORE_ASSETS = [
  '/static/css/theme.css',
  '/static/js/connectivity.js',
  '/static/js/offline-reception.js',
  '/static/icons/circle-core-icon-192.png',
  '/static/icons/circle-core-icon-512.png',
  '/static/icons/circle-core-icon.svg',
  '/manifest.webmanifest'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(CORE_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).then(response => {
        if (url.pathname === '/offline/' && response.ok && !response.redirected) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put('/offline/', copy));
        }
        return response;
      }).catch(async () => {
        const offline = await caches.match('/offline/');
        if (offline) return offline;
        return new Response(
          '<!doctype html><meta name="viewport" content="width=device-width"><title>Circle Core Offline</title><style>body{font:16px system-ui;background:#0d0f12;color:#f8fafc;padding:2rem;max-width:38rem;margin:auto}button{padding:.75rem 1rem;border:0;border-radius:4px;background:#22c55e;font-weight:700}</style><h1>No internet connection</h1><p>Circle Core will switch to Offline Reception after this device has been enrolled and synchronized once while online.</p><button onclick="location.reload()">Try again</button>',
          {status: 503, headers: {'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store'}}
        );
      })
    );
    return;
  }

  if (url.pathname.startsWith('/static/') || url.pathname === '/manifest.webmanifest') {
    event.respondWith(
      caches.match(request).then(cached => cached || fetch(request).then(response => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(request, copy));
        }
        return response;
      }))
    );
  }
});
""".strip()
    response = HttpResponse(script, content_type='text/javascript')
    response['Service-Worker-Allowed'] = '/'
    response['Cache-Control'] = 'no-cache'
    return response


def healthz(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
        return JsonResponse({'status': 'ok', 'schema': connection.schema_name})
    except Exception as exc:
        logger.error('Health check failed: %s', exc)
        return JsonResponse({'status': 'error'}, status=503)


def landing(request):
    return render(request, 'public/landing.html', {'plans': PLANS})


def privacy_policy(request):
    return render(request, 'public/privacy.html')


def terms(request):
    return render(request, 'public/terms.html')


def data_requests(request):
    return render(request, 'public/data_requests.html')


def public_login(request):
    """Guide users from the public site to their tenant-specific login URL."""
    if request.method == 'POST' and settings.DEBUG:
        login_url = _create_local_demo_workspace(request)
        return redirect(login_url)

    tenant_links = []
    scheme = 'http' if settings.DEBUG else 'https'
    port = request.get_port()
    port_suffix = f':{port}' if settings.DEBUG and port not in ('80', '443') else ''

    if settings.DEBUG:
        tenants = (
            GuestHouseTenant.objects
            .filter(is_active=True)
            .exclude(schema_name='public')
            .prefetch_related('domains')
            .order_by('name')
        )
        for tenant in tenants:
            domain_name = f'{tenant.schema_name}.localhost'
            Domain.objects.get_or_create(domain=domain_name, defaults={'tenant': tenant, 'is_primary': False})
            tenant_links.append({
                'name': tenant.name,
                'domain': domain_name,
                'login_url': f'{scheme}://{domain_name}{port_suffix}/login/',
            })

    return render(request, 'public/login_help.html', {
        'tenant_links': tenant_links,
        'base_domain': settings.BASE_DOMAIN,
        'debug': settings.DEBUG,
    })


def register(request):
    errors = {}
    form_data = {}

    if request.method == 'POST':
        if _is_rate_limited(_get_client_ip(request)):
            all_errors = 'Too many registration attempts. Please try again in an hour.'
            return render(request, 'public/register.html', {
                'errors': {'__all__': all_errors},
                'all_errors': all_errors,
                'form_data': {},
                'plans': PLANS,
            }, status=429)

        form_data = request.POST.dict()
        guest_house_name = form_data.get('guest_house_name', '').strip()
        owner_name = form_data.get('owner_name', '').strip()
        owner_email = form_data.get('owner_email', '').strip().lower()
        owner_phone = form_data.get('owner_phone', '').strip()
        username = form_data.get('username', '').strip()
        password = form_data.get('password', '')
        plan_key = form_data.get('plan', 'starter')

        if not guest_house_name:
            errors['guest_house_name'] = 'Guest house name is required.'
        if not owner_name:
            errors['owner_name'] = 'Your name is required.'
        if not owner_email:
            errors['owner_email'] = 'Email address is required.'
        if not username:
            errors['username'] = 'Username is required.'
        elif len(username) < 3:
            errors['username'] = 'Username must be at least 3 characters.'
        if not password:
            errors['password'] = 'Password is required.'
        else:
            try:
                validate_password(password)
            except ValidationError as exc:
                errors['password'] = ' '.join(exc.messages)

        if not errors and GuestHouseTenant.objects.filter(owner_email=owner_email).exists():
            errors['owner_email'] = 'An account with this email already exists.'

        if not errors:
            base_schema = slugify(guest_house_name).replace('-', '_')[:40] or 'guesthouse'
            schema_name = base_schema
            counter = 1
            while GuestHouseTenant.objects.filter(schema_name=schema_name).exists():
                schema_name = f'{base_schema}_{counter}'
                counter += 1

            domain_url = f'{schema_name}.{settings.BASE_DOMAIN}'
            local_domain_url = f'{schema_name}.localhost'
            tenant_created = False
            try:
                with transaction.atomic():
                    tenant = GuestHouseTenant(
                        schema_name=schema_name,
                        name=guest_house_name,
                        owner_name=owner_name,
                        owner_email=owner_email,
                        owner_phone=owner_phone,
                        is_verified=False,
                    )
                    tenant.save()
                    tenant_created = True

                    Domain.objects.create(domain=domain_url, tenant=tenant, is_primary=True)
                    if settings.DEBUG:
                        Domain.objects.get_or_create(
                            domain=local_domain_url,
                            defaults={'tenant': tenant, 'is_primary': False},
                        )

                    with schema_context(schema_name):
                        User = get_user_model()
                        user = User.objects.create_superuser(
                            username=username,
                            email=owner_email,
                            password=password,
                        )
                        owner_group, _ = Group.objects.get_or_create(name='Owner')
                        user.groups.add(owner_group)

                        from core.models import GuestHouseSettings, Subscription, SubscriptionPlan
                        plan = SubscriptionPlan.objects.filter(name=plan_key).first()
                        if plan:
                            trial_expires_at = trial_end()
                            Subscription.objects.create(
                                plan=plan,
                                status='trial',
                                trial_ends_at=trial_expires_at,
                                expires_at=trial_expires_at,
                                next_billing_date=trial_expires_at.date(),
                                owner_name=owner_name,
                                owner_email=owner_email,
                                owner_phone=owner_phone,
                            )

                        GuestHouseSettings.objects.get_or_create(
                            pk=1,
                            defaults={'guest_house_name': guest_house_name},
                        )

                # Seed demo data so the dashboard is alive on first login
                _seed_demo_data(schema_name)

                if settings.DEBUG:
                    login_url = f'http://{local_domain_url}:{request.get_port()}/login/'
                else:
                    login_url = f'https://{domain_url}/login/'
                _send_welcome_email(owner_name, owner_email, guest_house_name, login_url, schema_name)
                return redirect(login_url)

            except Exception as exc:
                logger.exception('Registration failed for %s', owner_email)
                if tenant_created:
                    _cleanup_failed_tenant(schema_name)
                errors['__all__'] = 'Registration failed. Please try again.'

    return render(request, 'public/register.html', {
        'errors': errors,
        'all_errors': errors.get('__all__'),
        'form_data': form_data,
        'plans': PLANS,
    })


def verify_email(request, token):
    """Activate tenant account after email verification."""
    try:
        data = signing.loads(token, salt='email-verification', max_age=86400 * 7)
        schema_name = data['schema']
        tenant = GuestHouseTenant.objects.get(schema_name=schema_name)
        tenant.is_verified = True
        tenant.save(update_fields=['is_verified'])
        domain = tenant.domains.filter(is_primary=True).first()
        if domain:
            return redirect(f'https://{domain.domain}/login/?verified=1')
        return redirect('landing')
    except (signing.BadSignature, signing.SignatureExpired, GuestHouseTenant.DoesNotExist):
        return render(request, 'public/verify_failed.html', status=400)


# ── Tenant PayFast views ─────────────────────────────────────────────────────

@login_required
def payfast_initiate(request):
    """Generate and auto-submit the PayFast payment form."""
    from core.models import Subscription
    from .payfast import build_payment_data, get_payfast_url

    subscription = Subscription.objects.select_related('plan').first()
    if not subscription:
        return redirect('login')

    schema_name = connection.schema_name
    form_data = build_payment_data(subscription, schema_name)

    return render(request, 'subscription/payment_redirect.html', {
        'form_data': form_data,
        'payfast_url': get_payfast_url(),
    })


def payfast_itn(request):
    """PayFast Instant Transaction Notification (ITN) handler."""
    if request.method != 'POST':
        return HttpResponse(status=405)

    from .payfast import validate_itn_with_payfast, verify_itn_signature
    from core.models import Subscription

    post_data = request.POST.dict()

    if not verify_itn_signature(post_data):
        return HttpResponse('Invalid signature', status=400)

    if not validate_itn_with_payfast(post_data):
        logger.warning('PayFast ITN server validation failed for schema %s', connection.schema_name)
        return HttpResponse('Validation failed', status=400)

    payment_status = post_data.get('payment_status', '')
    token = post_data.get('token', '')
    payment_id = post_data.get('m_payment_id', '')
    expected_prefix = f'SUB-{connection.schema_name}-'
    if not payment_id.startswith(expected_prefix):
        logger.warning(
            'PayFast ITN schema mismatch. schema=%s m_payment_id=%s',
            connection.schema_name,
            payment_id,
        )
        return HttpResponse('Schema mismatch', status=400)

    if payment_status == 'COMPLETE':
        subscription = Subscription.objects.first()
        if subscription:
            apply_paid_renewal(subscription)
            subscription.last_payment_date = timezone.now().date()
            subscription.last_payment_amount = post_data.get('amount_gross') or None
            subscription.last_payment_reference = post_data.get('pf_payment_id', '')
            if token:
                subscription.payfast_token = token
            subscription.save()
            logger.info('Subscription activated for schema %s via PayFast', connection.schema_name)

    return HttpResponse('OK')


def payment_success(request):
    """Return page after PayFast redirects the user back."""
    return render(request, 'subscription/payment_success.html')


def request_demo(request):
    """Demo request form — creates a Lead and sends notification emails."""
    errors = {}
    submitted = False

    if request.method == 'POST':
        full_name = request.POST.get('full_name', '').strip()
        business_name = request.POST.get('business_name', '').strip()
        email = request.POST.get('email', '').strip().lower()
        phone = request.POST.get('phone', '').strip()
        num_rooms_raw = request.POST.get('num_rooms', '').strip()
        notes = request.POST.get('notes', '').strip()

        if not full_name:
            errors['full_name'] = 'Your name is required.'
        if not business_name:
            errors['business_name'] = 'Business name is required.'
        if not email:
            errors['email'] = 'Email address is required.'
        if not phone:
            errors['phone'] = 'Phone number is required.'

        if not errors:
            from .models import Lead
            num_rooms = int(num_rooms_raw) if num_rooms_raw.isdigit() else None
            lead = Lead.objects.create(
                full_name=full_name,
                business_name=business_name,
                email=email,
                phone=phone,
                num_rooms=num_rooms,
                notes=notes,
                source='website',
            )
            _send_demo_request_emails(lead)
            submitted = True

    return render(request, 'public/request_demo.html', {
        'errors': errors,
        'all_errors': errors.get('__all__'),
        'submitted': submitted,
        'form_data': request.POST if request.method == 'POST' else {},
    })


# ── Helpers ───────────────────────────────────────────────────────────────────

def _seed_demo_data(schema_name: str) -> None:
    """Run the demo data seeder for a newly created tenant. Errors are non-fatal."""
    try:
        from django.core.management import call_command
        call_command('seed_demo_data', schema=schema_name)
    except Exception as exc:
        logger.warning('Demo data seeding failed for %s: %s', schema_name, exc)


def _create_local_demo_workspace(request) -> str:
    """Create/reset a predictable local tenant login for development."""
    schema_name = 'demo'
    domain_name = f'{schema_name}.localhost'
    tenant, created = GuestHouseTenant.objects.get_or_create(
        schema_name=schema_name,
        defaults={
            'name': 'Demo Guest House',
            'owner_name': 'Demo Owner',
            'owner_email': 'demo@localhost.test',
            'owner_phone': '',
            'is_active': True,
            'is_verified': True,
        },
    )

    if not created and not tenant.is_active:
        tenant.is_active = True
        tenant.save(update_fields=['is_active'])

    Domain.objects.get_or_create(
        domain=domain_name,
        defaults={'tenant': tenant, 'is_primary': False},
    )

    with schema_context(schema_name):
        User = get_user_model()
        user, user_created = User.objects.get_or_create(
            username='admin',
            defaults={
                'email': 'demo@localhost.test',
                'is_staff': True,
                'is_superuser': True,
            },
        )
        user.email = 'demo@localhost.test'
        user.is_staff = True
        user.is_superuser = True
        user.set_password('admin12345')
        user.save()

        owner_group, _ = Group.objects.get_or_create(name='Owner')
        user.groups.add(owner_group)

        from core.models import GuestHouseSettings, Subscription, SubscriptionPlan

        plan, _ = SubscriptionPlan.objects.get_or_create(
            name='professional',
            defaults={
                'display_name': 'Professional',
                'monthly_price': 799,
                'annual_price': 7670,
                'max_rooms': 20,
                'max_users': 5,
                'feature_expenses': True,
                'feature_full_reports': True,
                'feature_hourly_bookings': True,
                'feature_weekly_bookings': True,
                'feature_inventory': True,
                'feature_staff_roles': True,
                'feature_custom_pdf_branding': True,
                'feature_maintenance_requests': True,
            },
        )

        trial_expires_at = trial_end()
        Subscription.objects.update_or_create(
            owner_email='demo@localhost.test',
            defaults={
                'plan': plan,
                'status': 'trial',
                'trial_ends_at': trial_expires_at,
                'expires_at': trial_expires_at,
                'next_billing_date': trial_expires_at.date(),
                'owner_name': 'Demo Owner',
                'owner_phone': '',
            },
        )

        GuestHouseSettings.objects.get_or_create(
            pk=1,
            defaults={'guest_house_name': 'Demo Guest House'},
        )

    _seed_demo_data(schema_name)

    port = request.get_port()
    port_suffix = f':{port}' if port not in ('80', '443') else ''
    return f'http://{domain_name}{port_suffix}/login/'


def _send_demo_request_emails(lead) -> None:
    """Send confirmation to the customer and notification to Circle Core."""
    try:
        html_customer = render_to_string('emails/demo_request_customer.html', {'lead': lead})
        send_mail(
            subject='Thanks for your interest — Circle Core Guest House',
            message=strip_tags(html_customer),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[lead.email],
            html_message=html_customer,
            fail_silently=True,
        )
    except Exception as exc:
        logger.warning('Demo customer email failed: %s', exc)

    try:
        html_internal = render_to_string('emails/demo_request_internal.html', {'lead': lead})
        internal_email = getattr(settings, 'CIRCLE_CORE_SALES_EMAIL', settings.DEFAULT_FROM_EMAIL)
        send_mail(
            subject=f'New Demo Request — {lead.business_name}',
            message=strip_tags(html_internal),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[internal_email],
            html_message=html_internal,
            fail_silently=True,
        )
    except Exception as exc:
        logger.warning('Demo internal email failed: %s', exc)


def _send_welcome_email(owner_name, owner_email, guest_house_name, login_url, schema_name):
    try:
        token = signing.dumps({'schema': schema_name}, salt='email-verification')
        verify_url = f'https://{settings.BASE_DOMAIN}/verify/{token}/'
        ctx = {
            'owner_name': owner_name,
            'guest_house_name': guest_house_name,
            'login_url': login_url,
            'verify_url': verify_url,
        }
        html = render_to_string('emails/welcome_tenant.html', ctx)
        send_mail(
            subject=f'Welcome to Circle Core — {guest_house_name}',
            message=strip_tags(html),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[owner_email],
            html_message=html,
            fail_silently=False,
        )
    except Exception as exc:
        logger.warning('Welcome email failed for %s: %s', owner_email, exc)
