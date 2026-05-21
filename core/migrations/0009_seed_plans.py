from decimal import Decimal

from django.db import migrations


def seed_plans(apps, schema_editor):
    SubscriptionPlan = apps.get_model("core", "SubscriptionPlan")

    plans = [
        {
            "name": "starter",
            "display_name": "Starter",
            "monthly_price": Decimal("399.00"),
            "annual_price": Decimal("3830.00"),
            "max_rooms": 8,
            "max_users": 1,
        },
        {
            "name": "professional",
            "display_name": "Professional",
            "monthly_price": Decimal("799.00"),
            "annual_price": Decimal("7670.00"),
            "max_rooms": 20,
            "max_users": 5,
            "feature_expenses": True,
            "feature_full_reports": True,
            "feature_hourly_bookings": True,
            "feature_weekly_bookings": True,
            "feature_inventory": True,
            "feature_staff_roles": True,
            "feature_pos_integration": True,
            "feature_custom_pdf_branding": True,
            "feature_maintenance_requests": True,
        },
        {
            "name": "enterprise",
            "display_name": "Enterprise",
            "monthly_price": Decimal("1499.00"),
            "annual_price": Decimal("14390.00"),
            "max_rooms": 9999,
            "max_users": 9999,
            "feature_expenses": True,
            "feature_full_reports": True,
            "feature_export": True,
            "feature_hourly_bookings": True,
            "feature_weekly_bookings": True,
            "feature_inventory": True,
            "feature_staff_roles": True,
            "feature_pos_integration": True,
            "feature_multi_property": True,
            "feature_api_access": True,
            "feature_custom_pdf_branding": True,
            "feature_maintenance_requests": True,
            "feature_priority_support": True,
        },
    ]

    feature_fields = [
        "feature_expenses",
        "feature_full_reports",
        "feature_export",
        "feature_hourly_bookings",
        "feature_weekly_bookings",
        "feature_inventory",
        "feature_staff_roles",
        "feature_pos_integration",
        "feature_multi_property",
        "feature_api_access",
        "feature_custom_pdf_branding",
        "feature_maintenance_requests",
        "feature_priority_support",
    ]

    for plan_data in plans:
        defaults = plan_data.copy()
        name = defaults.pop("name")
        for field in feature_fields:
            defaults.setdefault(field, False)
        SubscriptionPlan.objects.update_or_create(name=name, defaults=defaults)


def unseed_plans(apps, schema_editor):
    SubscriptionPlan = apps.get_model("core", "SubscriptionPlan")
    SubscriptionPlan.objects.filter(name__in=["starter", "professional", "enterprise"]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0008_subscriptionplan_subscription"),
    ]

    operations = [
        migrations.RunPython(seed_plans, unseed_plans),
    ]
