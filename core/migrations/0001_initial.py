import datetime
from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="GuestHouseSettings",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "guest_house_name",
                    models.CharField(default="My Guest House", max_length=150),
                ),
                (
                    "logo",
                    models.ImageField(
                        blank=True,
                        null=True,
                        upload_to="settings/",
                    ),
                ),
                ("phone", models.CharField(blank=True, max_length=50)),
                ("email", models.EmailField(blank=True, max_length=254)),
                ("address", models.TextField(blank=True)),
                ("banking_details", models.TextField(blank=True)),
                ("vat_registered", models.BooleanField(default=False)),
                ("vat_number", models.CharField(blank=True, max_length=50)),
                (
                    "vat_rate",
                    models.DecimalField(
                        decimal_places=2,
                        default=Decimal("15.00"),
                        max_digits=5,
                    ),
                ),
                ("check_in_time", models.TimeField(default=datetime.time(14, 0))),
                ("check_out_time", models.TimeField(default=datetime.time(10, 0))),
                ("currency", models.CharField(default="R", max_length=10)),
                ("cancellation_note", models.TextField(blank=True)),
                ("invoice_notes", models.TextField(blank=True)),
                ("receipt_notes", models.TextField(blank=True)),
            ],
            options={
                "verbose_name": "Guest House Settings",
                "verbose_name_plural": "Guest House Settings",
            },
        ),
    ]
