"""
Send one of every email type the app generates to a test address.

Usage:
    python manage.py send_preview_emails applejazzrsa@gmail.com
"""

import time
from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.utils import timezone


class Command(BaseCommand):
    help = "Send a preview of every email template to a single address."

    def add_arguments(self, parser):
        parser.add_argument("recipient", help="Email address to send all previews to.")

    def handle(self, *args, **options):
        recipient = options["recipient"]

        from django_tenants.utils import schema_context
        from tenants.models import GuestHouseTenant

        # Find first non-public tenant and run inside it
        tenant = GuestHouseTenant.objects.exclude(schema_name="public").first()
        if not tenant:
            self.stdout.write(self.style.ERROR("No tenant found."))
            return

        self.stdout.write(f"Using tenant: {tenant.name} (schema: {tenant.schema_name})\n")

        with schema_context(tenant.schema_name):
            self._send_all(recipient, tenant)

    def _send_all(self, recipient, tenant_obj):
        from core.models import Booking, GuestHouseSettings, Payment, Subscription

        # ── Shared context objects ─────────────────────────────────────
        settings_obj, _ = GuestHouseSettings.objects.get_or_create(pk=1)
        booking = (
            Booking.objects.select_related("guest", "room")
            .order_by("-created_at")
            .first()
        )
        payment = (
            Payment.objects.select_related("booking__guest")
            .order_by("-recorded_at")
            .first()
        )
        subscription = Subscription.objects.select_related("plan").first()

        if not booking:
            self.stdout.write(self.style.ERROR(
                "No bookings found. Create at least one booking first."
            ))
            return

        if not hasattr(tenant_obj, "owner_name"):
            tenant_obj.owner_name = booking.guest.first_name if booking else "Owner"

        # Build login URL from tenant domain or fall back to BASE_DOMAIN
        from django.conf import settings as django_settings
        domain_obj = tenant_obj.domains.filter(is_primary=True).first() if hasattr(tenant_obj, 'domains') else None
        if domain_obj:
            login_url = f"https://{domain_obj.domain}/login/"
        else:
            base_domain = getattr(django_settings, 'BASE_DOMAIN', 'circlecore.co.za')
            login_url = f"https://{tenant_obj.schema_name}.{base_domain}/login/"

        base_ctx = {"booking": booking, "settings": settings_obj, "login_url": login_url}

        emails = [
            # ── Booking flow ──────────────────────────────────────────
            {
                "subject": "📋 [1/15] Booking Confirmation",
                "template": "emails/booking_confirmation.html",
                "ctx": base_ctx,
            },
            {
                "subject": "🔔 [2/15] Admin — New Booking Alert",
                "template": "emails/admin_new_booking.html",
                "ctx": base_ctx,
            },
            {
                "subject": "🏨 [3/15] Guest Check-In Welcome",
                "template": "emails/booking_checkin.html",
                "ctx": base_ctx,
            },
            {
                "subject": "👋 [4/15] Guest Check-Out Thank You",
                "template": "emails/booking_checkout.html",
                "ctx": base_ctx,
            },
            {
                "subject": "✅ [5/15] Payment Received",
                "template": "emails/payment_received.html",
                "ctx": {"booking": booking, "payment": payment, "settings": settings_obj},
            },
            {
                "subject": "❌ [6/15] Booking Cancelled",
                "template": "emails/booking_cancelled.html",
                "ctx": base_ctx,
            },
            {
                "subject": "⚠️ [7/15] Balance Reminder",
                "template": "emails/balance_reminder.html",
                "ctx": base_ctx,
            },
            # ── Trial & subscription ───────────────────────────────────
            {
                "subject": "[8/15] Welcome to Circle Core (New Tenant)",
                "template": "emails/welcome_tenant.html",
                "ctx": {"tenant": tenant_obj, "guest_house_name": tenant_obj.name, "subscription": subscription, "login_url": login_url, "verify_url": login_url},
            },
            {
                "subject": "[9/15] Trial Day 1 - Getting Started",
                "template": "emails/trial_day1.html",
                "ctx": {"tenant": tenant_obj, "subscription": subscription, "login_url": login_url},
            },
            {
                "subject": "[10/15] Trial Day 2 - Tips",
                "template": "emails/trial_day2.html",
                "ctx": {"tenant": tenant_obj, "subscription": subscription, "login_url": login_url},
            },
            {
                "subject": "[11/15] Trial Day 5 - Features",
                "template": "emails/trial_day5.html",
                "ctx": {"tenant": tenant_obj, "subscription": subscription, "login_url": login_url},
            },
            {
                "subject": "[12/15] Trial Day 10 - Final Push",
                "template": "emails/trial_day10.html",
                "ctx": {"tenant": tenant_obj, "subscription": subscription, "login_url": login_url},
            },
            {
                "subject": "[13/15] Trial Expiry Reminder",
                "template": "emails/trial_reminder.html",
                "ctx": {"tenant": tenant_obj, "subscription": subscription, "login_url": login_url},
            },
            # ── Demo requests ──────────────────────────────────────────
            {
                "subject": "[14/15] Demo Request - Customer Confirmation",
                "template": "emails/demo_request_customer.html",
                "ctx": {
                    "name": "Matthews Maphosa",
                    "guest_house_name": "My Guest House",
                    "email": recipient,
                    "phone": "+27 79 266 2287",
                    "subscription": subscription,
                },
            },
            {
                "subject": "📥 [15/15] Demo Request — Internal Notification",
                "template": "emails/demo_request_internal.html",
                "ctx": {
                    "name": "Matthews Maphosa",
                    "guest_house_name": "My Guest House",
                    "email": recipient,
                    "phone": "+27 79 266 2287",
                    "message": "I want to see how the booking and payment modules work.",
                },
            },
        ]

        total = len(emails)
        sent = 0
        failed = 0

        self.stdout.write(f"\nSending {total} email previews to {recipient}...\n")

        for i, email in enumerate(emails, 1):
            try:
                html = render_to_string(email["template"], email["ctx"])
                plain = strip_tags(html)
                send_mail(
                    subject=email["subject"],
                    message=plain,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[recipient],
                    html_message=html,
                    fail_silently=False,
                )
                sent += 1
                safe_subj = email['subject'].encode('ascii', 'replace').decode('ascii')
                self.stdout.write(self.style.SUCCESS(f"  [{i}/{total}] OK   {safe_subj}"))
                time.sleep(1)
            except Exception as exc:
                failed += 1
                safe_subj = email['subject'].encode('ascii', 'replace').decode('ascii')
                self.stdout.write(self.style.ERROR(f"  [{i}/{total}] FAIL {safe_subj}"))
                self.stdout.write(self.style.WARNING(f"       Error: {exc}"))

        self.stdout.write(f"\n{'='*50}")
        self.stdout.write(self.style.SUCCESS(f"Done — {sent} sent") + f", {failed} failed")
        self.stdout.write(f"Check your inbox at {recipient}\n")
