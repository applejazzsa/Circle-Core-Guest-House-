from django.db import migrations, models


def create_vehicle_guest_profiles(apps, schema_editor):
    Guest = apps.get_model("core", "Guest")
    Booking = apps.get_model("core", "Booking")

    profiles = {}
    for booking in Booking.objects.exclude(vehicle_registration="").order_by("pk").iterator():
        registration = " ".join((booking.vehicle_registration or "").strip().upper().split())
        if not registration:
            continue

        guest = profiles.get(registration)
        if guest is None:
            guest, _ = Guest.objects.get_or_create(
                vehicle_registration=registration,
                defaults={
                    "first_name": "Vehicle",
                    "last_name": registration,
                    "phone": "N/A",
                },
            )
            profiles[registration] = guest

        updates = []
        if booking.vehicle_registration != registration:
            booking.vehicle_registration = registration
            updates.append("vehicle_registration")
        if booking.guest_id != guest.pk:
            booking.guest_id = guest.pk
            updates.append("guest")
        if updates:
            booking.save(update_fields=updates)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0034_spa_payments"),
    ]

    operations = [
        migrations.AddField(
            model_name="guest",
            name="vehicle_registration",
            field=models.CharField(blank=True, max_length=20, null=True, unique=True),
        ),
        migrations.RunPython(create_vehicle_guest_profiles, migrations.RunPython.noop),
    ]
