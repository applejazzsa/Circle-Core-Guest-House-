"""
Send trial, renewal, and grace-period reminder emails.

Run daily at 9am:
    python manage.py send_trial_reminders --settings=config.settings_production
"""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags
from django_tenants.utils import schema_context

from core.subscriptions import GRACE_DAYS, grace_ends_at
from tenants.models import GuestHouseTenant

logger = logging.getLogger(__name__)

TRIAL_REMINDER_SCHEDULE = {
    10: ("emails/trial_day10.html", "How is your Circle Core trial going? - 10 days left"),
    5: ("emails/trial_day5.html", "5 days left on your Circle Core trial"),
    2: ("emails/trial_day2.html", "48 hours to secure your Circle Core account"),
    1: ("emails/trial_day1.html", "Your Circle Core trial expires today"),
    0: ("emails/trial_day0.html", "Your Circle Core trial has expired — here's what happens next"),
}

RENEWAL_REMINDER_DAYS = {5, 2, 1}
GRACE_REMINDER_DAYS = {1, 3, 5}


class Command(BaseCommand):
    help = "Send trial expiry, paid renewal, and grace-period reminder emails."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Print reminders without sending emails.")
        parser.add_argument("--schema", help="Only process this specific tenant schema.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        target_schema = options.get("schema")

        queryset = GuestHouseTenant.objects.filter(is_active=True)
        if target_schema:
            queryset = queryset.filter(schema_name=target_schema)

        sent = 0
        skipped = 0

        for tenant in queryset:
            with schema_context(tenant.schema_name):
                try:
                    from core.models import Subscription

                    subscription = Subscription.objects.select_related("plan").first()
                    if not subscription:
                        continue

                    email = subscription.owner_email
                    subject, html = self._build_email(tenant, subscription)
                    if not subject:
                        continue

                    if dry_run:
                        self.stdout.write(f"[DRY RUN] {tenant.schema_name} <{email}> - {subject}")
                        skipped += 1
                        continue

                    send_mail(
                        subject=subject,
                        message=strip_tags(html),
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        recipient_list=[email],
                        html_message=html,
                        fail_silently=False,
                    )
                    sent += 1
                    self.stdout.write(self.style.SUCCESS(f"Sent: {tenant.schema_name} <{email}> - {subject}"))

                except Exception as exc:
                    self.stderr.write(f"Error - {tenant.schema_name}: {exc}")
                    logger.exception("send_trial_reminders failed for %s", tenant.schema_name)

        if dry_run:
            self.stdout.write(self.style.WARNING(f"Dry run complete. {skipped} email(s) would be sent."))
        else:
            self.stdout.write(self.style.SUCCESS(f"Done. {sent} reminder email(s) sent."))

    def _tenant_login_url(self, tenant):
        domain = tenant.domains.filter(is_primary=True).first()
        return f"https://{domain.domain}/" if domain else "#"

    def _build_email(self, tenant, subscription):
        if subscription.status == "trial":
            return self._build_trial_email(tenant, subscription)
        if subscription.status == "active":
            return self._build_paid_email(tenant, subscription)
        return "", ""

    def _build_trial_email(self, tenant, subscription):
        days = subscription.days_remaining
        if days not in TRIAL_REMINDER_SCHEDULE:
            return "", ""
        # Day-0 email: only fire on the actual expiry date to avoid re-sending for old expired trials
        if days == 0:
            today = timezone.localdate()
            if subscription.expires_at.date() != today:
                return "", ""
        template, subject = TRIAL_REMINDER_SCHEDULE[days]
        html = render_to_string(
            template,
            {
                "tenant": tenant,
                "subscription": subscription,
                "days": days,
                "login_url": self._tenant_login_url(tenant),
            },
        )
        return subject, html

    def _build_paid_email(self, tenant, subscription):
        today = timezone.localdate()
        expiry_date = subscription.expires_at.date()
        days_until = (expiry_date - today).days
        login_url = self._tenant_login_url(tenant)

        if days_until in RENEWAL_REMINDER_DAYS:
            cycle = subscription.get_billing_cycle_display().lower()
            subject = f"Circle Core {cycle} subscription renews in {days_until} day"
            if days_until != 1:
                subject += "s"
            html = (
                f"<p>Hi {subscription.owner_name},</p>"
                f"<p>Your Circle Core {cycle} subscription for {tenant.name} renews on "
                f"<strong>{expiry_date:%d %b %Y}</strong>.</p>"
                "<p>Monthly plans renew on the 1st of the month. Annual plans renew on their anniversary date.</p>"
                f'<p><a href="{login_url}">Open your account</a></p>'
            )
            return subject, html

        if today >= expiry_date and timezone.now() < grace_ends_at(subscription):
            days_in_grace = (today - expiry_date).days + 1
            if days_in_grace not in GRACE_REMINDER_DAYS:
                return "", ""
            days_left = max(GRACE_DAYS - days_in_grace + 1, 0)
            subject = f"Circle Core payment grace period - {days_left} day"
            if days_left != 1:
                subject += "s"
            subject += " left"
            html = (
                f"<p>Hi {subscription.owner_name},</p>"
                f"<p>Your Circle Core subscription for {tenant.name} is in its {GRACE_DAYS}-day grace period.</p>"
                "<p>Please renew before the grace period ends to avoid read-only access.</p>"
                f'<p><a href="{login_url}">Open your account</a></p>'
            )
            return subject, html

        return "", ""
