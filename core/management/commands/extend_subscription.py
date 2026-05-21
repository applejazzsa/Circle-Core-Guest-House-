from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import Subscription


class Command(BaseCommand):
    help = "Extend the current Circle Core subscription by N days."

    def add_arguments(self, parser):
        parser.add_argument("days", type=int, help="Number of days to add to the current subscription.")

    def handle(self, *args, **options):
        days = options["days"]
        if days <= 0:
            raise CommandError("Days must be greater than zero.")

        subscription = Subscription.objects.select_related("plan").first()
        if not subscription:
            raise CommandError("No subscription found. Run python manage.py setup_subscription first.")

        base = subscription.expires_at if subscription.expires_at > timezone.now() else timezone.now()
        subscription.expires_at = base + timezone.timedelta(days=days)
        if subscription.status == "expired":
            subscription.status = "active"
            subscription.save(update_fields=["expires_at", "status"])
        else:
            subscription.save(update_fields=["expires_at"])

        self.stdout.write(
            self.style.SUCCESS(
                f"Subscription extended by {days} days. New expiry: {subscription.expires_at:%Y-%m-%d}. "
                f"Days remaining: {subscription.days_remaining}."
            )
        )
