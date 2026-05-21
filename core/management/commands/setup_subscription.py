from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import Subscription, SubscriptionPlan


class Command(BaseCommand):
    help = "Create or replace the Circle Core subscription for this installation."

    def handle(self, *args, **options):
        plans = list(SubscriptionPlan.objects.order_by("monthly_price"))
        if not plans:
            raise CommandError("No subscription plans found. Run migrations to seed plans first.")

        owner_name = input("Owner name: ").strip()
        owner_email = input("Owner email: ").strip()
        owner_phone = input("Owner phone: ").strip()

        self.stdout.write("Plan choice:")
        for index, plan in enumerate(plans, start=1):
            self.stdout.write(f"{index}= {plan.display_name}")
        plan_index = int(input("Choose plan: ").strip() or "1") - 1
        plan = plans[plan_index]

        billing_choice = input("Billing cycle (1=Monthly, 2=Annual): ").strip() or "1"
        billing_cycle = "annual" if billing_choice == "2" else "monthly"

        status_choice = input("Trial or Active? (1=Start 30-day trial, 2=Mark as Active): ").strip() or "1"
        status = "active" if status_choice == "2" else "trial"

        now = timezone.now()
        if status == "trial":
            expires_at = now + timedelta(days=30)
            trial_ends_at = expires_at
        elif billing_cycle == "annual":
            expires_at = now + timedelta(days=365)
            trial_ends_at = None
        else:
            expires_at = now + timedelta(days=30)
            trial_ends_at = None

        Subscription.objects.all().delete()
        subscription = Subscription.objects.create(
            plan=plan,
            billing_cycle=billing_cycle,
            status=status,
            expires_at=expires_at,
            trial_ends_at=trial_ends_at,
            owner_name=owner_name,
            owner_email=owner_email,
            owner_phone=owner_phone,
            next_billing_date=expires_at.date(),
        )

        self.stdout.write(
            self.style.SUCCESS(
                "Subscription configured. "
                f"Plan: {subscription.plan.display_name}. "
                f"Billing: {subscription.get_billing_cycle_display()}. "
                f"Status: {subscription.get_status_display()}. "
                f"Expires: {subscription.expires_at:%Y-%m-%d}. "
                f"Days remaining: {subscription.days_remaining}."
            )
        )
