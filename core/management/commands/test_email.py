"""
Quick email configuration test.

Usage:
    python manage.py test_email your@email.com
"""

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Send a test email to verify SMTP configuration.'

    def add_arguments(self, parser):
        parser.add_argument('recipient', help='Email address to send the test to.')

    def handle(self, *args, **options):
        recipient = options['recipient']
        self.stdout.write(f'Sending test email to {recipient} via {settings.EMAIL_HOST}...')
        try:
            send_mail(
                subject='Circle Core — Email Test',
                message='This is a test email from Circle Core Guest House. If you receive this, your email configuration is working correctly.',
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[recipient],
                fail_silently=False,
            )
            self.stdout.write(self.style.SUCCESS(f'[OK] Email sent successfully from {settings.DEFAULT_FROM_EMAIL}'))
        except Exception as exc:
            self.stdout.write(self.style.ERROR(f'[FAILED] {exc}'))
            self.stdout.write(self.style.WARNING(
                '\nCommon fixes:\n'
                '  - Check EMAIL_HOST_PASSWORD in your .env\n'
                '  - Confirm the mailbox exists in Xneelo control panel\n'
                '  - Make sure EMAIL_HOST_USER matches the mailbox address exactly\n'
                '  - Try port 465 with EMAIL_USE_TLS=False and EMAIL_USE_SSL=True'
            ))
