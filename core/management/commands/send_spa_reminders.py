"""
Send SMS/email reminders for spa appointments scheduled tomorrow.

Run daily at 18:00:
    python manage.py send_spa_reminders
"""

import logging
import datetime

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.utils import timezone
from django_tenants.utils import schema_context

from tenants.models import GuestHouseTenant

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Send appointment reminders for tomorrow's spa bookings"

    def handle(self, *args, **options):
        tomorrow = timezone.localdate() + datetime.timedelta(days=1)
        tenants = GuestHouseTenant.objects.exclude(schema_name="public")
        sent = 0
        skipped = 0

        for tenant in tenants:
            with schema_context(tenant.schema_name):
                try:
                    from core.models import GuestHouseSettings, SpaAppointment

                    try:
                        gh_settings = GuestHouseSettings.objects.first()
                    except Exception:
                        gh_settings = None

                    prop_name = gh_settings.property_name if gh_settings else tenant.name
                    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@circlecoresas.co.za")
                    reply_to = gh_settings.contact_email if gh_settings and gh_settings.contact_email else from_email

                    appts = SpaAppointment.objects.filter(
                        scheduled_date=tomorrow,
                        status__in=["pending", "confirmed"],
                        reminder_sent=False,
                    ).select_related("service", "assigned_therapist", "guest", "walk_in_booking")

                    for appt in appts:
                        guest_email = None
                        if appt.guest and appt.guest.email:
                            guest_email = appt.guest.email

                        if not guest_email:
                            skipped += 1
                            continue

                        guest_name = appt.display_guest_name
                        service_name = appt.service.name
                        appt_time = appt.scheduled_time.strftime("%H:%M")
                        therapist_line = f" with {appt.assigned_therapist.name}" if appt.assigned_therapist else ""

                        subject = f"Reminder: Your {service_name} appointment tomorrow at {appt_time} — {prop_name}"
                        message = (
                            f"Hi {guest_name},\n\n"
                            f"This is a reminder that you have a {service_name} appointment tomorrow "
                            f"({tomorrow.strftime('%A, %d %B %Y')}) at {appt_time}{therapist_line} "
                            f"at {prop_name}.\n\n"
                            f"If you need to cancel or reschedule, please contact us as soon as possible.\n\n"
                            f"We look forward to seeing you!\n\n"
                            f"— {prop_name}"
                        )

                        try:
                            send_mail(
                                subject,
                                message,
                                from_email,
                                [guest_email],
                                fail_silently=False,
                                reply_to=[reply_to],
                            )
                            appt.reminder_sent = True
                            appt.save(update_fields=["reminder_sent"])
                            sent += 1
                            logger.info("Spa reminder sent to %s for appt %s", guest_email, appt.pk)
                        except Exception as exc:
                            logger.error("Failed to send reminder for appt %s: %s", appt.pk, exc)
                            skipped += 1

                except Exception as exc:
                    logger.error("Error processing tenant %s: %s", tenant.schema_name, exc)

        self.stdout.write(self.style.SUCCESS(f"Spa reminders: {sent} sent, {skipped} skipped"))
