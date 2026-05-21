"""
Email notification utilities for Circle Core Guest House.
All functions are silent-fail: errors are logged and never crash the request.
"""

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils.html import strip_tags

logger = logging.getLogger(__name__)


def _send(subject, recipient, template, context):
    if not recipient:
        return False, ""
    try:
        html_body = render_to_string(template, context)
        plain_body = strip_tags(html_body)
        send_mail(
            subject=subject,
            message=plain_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[recipient],
            html_message=html_body,
            fail_silently=False,
        )
        return True, plain_body
    except Exception as exc:
        logger.warning("Email send failed [%s -> %s]: %s", subject, recipient, exc)
        return False, str(exc)


def _log_email(booking, recipient, template_name, message, success):
    try:
        from .models import CommunicationLog

        CommunicationLog.objects.create(
            booking=booking,
            guest=booking.guest if booking else None,
            channel="Email",
            template_name=template_name,
            recipient=recipient,
            message=(message or "")[:5000],
            status="sent" if success else "failed",
        )
    except Exception as exc:
        logger.warning("Communication log failed [Email -> %s]: %s", recipient, exc)


def _send_and_log(booking, subject, recipient, template, context, template_name):
    success, message = _send(subject, recipient, template, context)
    _log_email(booking, recipient, template_name, message, success)


def _guest_email(booking):
    email = getattr(booking.guest, "email", None)
    return email.strip() if email and email.strip() else None


def _settings_email(guest_house_settings):
    email = getattr(guest_house_settings, "email", None)
    return email.strip() if email and email.strip() else None


def notify_booking_created(booking, guest_house_settings):
    ctx = {"booking": booking, "settings": guest_house_settings}
    guest_email = _guest_email(booking)
    if guest_email:
        _send_and_log(
            booking,
            f"Booking Confirmed - {booking.booking_reference}",
            guest_email,
            "emails/booking_confirmation.html",
            ctx,
            "booking_confirmation",
        )

    admin_email = _settings_email(guest_house_settings)
    if admin_email:
        _send_and_log(
            booking,
            f"New Booking: {booking.booking_reference} - {booking.guest.full_name}",
            admin_email,
            "emails/admin_new_booking.html",
            ctx,
            "admin_new_booking",
        )


def notify_booking_checkin(booking, guest_house_settings):
    ctx = {"booking": booking, "settings": guest_house_settings}
    guest_email = _guest_email(booking)
    if guest_email:
        _send_and_log(
            booking,
            f"Welcome to {guest_house_settings.guest_house_name or 'Circle Core Guest House'}!",
            guest_email,
            "emails/booking_checkin.html",
            ctx,
            "booking_checkin",
        )


def notify_booking_checkout(booking, guest_house_settings):
    ctx = {"booking": booking, "settings": guest_house_settings}
    guest_email = _guest_email(booking)
    if guest_email:
        _send_and_log(
            booking,
            f"Thank You for Staying - {guest_house_settings.guest_house_name or 'Circle Core'}",
            guest_email,
            "emails/booking_checkout.html",
            ctx,
            "booking_checkout",
        )


def notify_payment_received(booking, payment, guest_house_settings):
    ctx = {"booking": booking, "payment": payment, "settings": guest_house_settings}
    guest_email = _guest_email(booking)
    if guest_email:
        _send_and_log(
            booking,
            f"Payment Received - {booking.booking_reference}",
            guest_email,
            "emails/payment_received.html",
            ctx,
            "payment_received",
        )


def notify_booking_cancelled(booking, guest_house_settings):
    ctx = {"booking": booking, "settings": guest_house_settings}
    guest_email = _guest_email(booking)
    if guest_email:
        _send_and_log(
            booking,
            f"Booking Cancelled - {booking.booking_reference}",
            guest_email,
            "emails/booking_cancelled.html",
            ctx,
            "booking_cancelled",
        )


def notify_balance_reminder(booking, guest_house_settings):
    ctx = {"booking": booking, "settings": guest_house_settings}
    guest_email = _guest_email(booking)
    if guest_email:
        _send_and_log(
            booking,
            f"Outstanding Balance - {booking.booking_reference}",
            guest_email,
            "emails/balance_reminder.html",
            ctx,
            "balance_reminder",
        )
