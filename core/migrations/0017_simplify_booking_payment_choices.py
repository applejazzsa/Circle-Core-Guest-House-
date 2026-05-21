from django.db import migrations, models


def normalize_booking_payments(apps, schema_editor):
    Payment = apps.get_model("core", "Payment")
    Payment.objects.filter(payment_method="Other").update(payment_method="Cash")
    Payment.objects.exclude(payment_type="Payment").update(payment_type="Payment")


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0016_settings_default_rates_and_24_hours"),
    ]

    operations = [
        migrations.RunPython(normalize_booking_payments, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="payment",
            name="payment_method",
            field=models.CharField(choices=[("Cash", "Cash"), ("EFT", "EFT"), ("Card", "Card")], max_length=20),
        ),
        migrations.AlterField(
            model_name="payment",
            name="payment_type",
            field=models.CharField(choices=[("Payment", "Payment")], max_length=20),
        ),
    ]
