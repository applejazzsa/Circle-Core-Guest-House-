# Generated for Circle Core Guest House on 2026-07-28
#
# Tenant-safe by construction: TENANT_APPS migrations (this app is one) run
# once per tenant schema via django-tenants' migration executor, so this
# RunPython body only ever sees the current schema's own Booking/RoomAllocation
# rows — no cross-tenant data is ever touched or visible here.

from django.db import migrations, transaction


def backfill_room_allocations(apps, schema_editor):
    Booking = apps.get_model("core", "Booking")
    RoomAllocation = apps.get_model("core", "RoomAllocation")

    total_bookings = Booking.objects.count()
    pending = Booking.objects.filter(room_allocations__isnull=True)
    pending_count = pending.count()
    created = 0

    with transaction.atomic():
        for booking in pending.iterator():
            if not booking.room_id:
                raise RuntimeError(
                    f"Critical inconsistency: booking id={booking.pk} has no room_id; "
                    "aborting backfill so nothing partial is committed."
                )
            if booking.num_guests is None or booking.num_guests < 1:
                raise RuntimeError(
                    f"Critical inconsistency: booking id={booking.pk} has invalid "
                    f"num_guests={booking.num_guests!r}; aborting backfill so nothing partial is committed."
                )
            RoomAllocation.objects.create(
                booking_id=booking.pk,
                room_id=booking.room_id,
                allocated_guests=booking.num_guests,
                rate_plan=None,
                rate_per_night=booking.rate_per_night,
                # Mirrors the booking's already-charged total exactly — no
                # recalculation, so no historical amount is altered or duplicated.
                line_total=booking.total_amount,
            )
            created += 1

    already_had_allocation = total_bookings - pending_count
    print(
        f"    [room_allocation backfill] total_bookings={total_bookings} "
        f"already_had_allocation={already_had_allocation} newly_created={created}"
    )


def remove_backfilled_room_allocations(apps, schema_editor):
    # Safe only while this backfill is the sole creator of RoomAllocation rows —
    # true at this point in the project, since the booking/availability flow
    # has not yet been wired to create allocations of its own. Reversing this
    # migration therefore just removes every RoomAllocation row.
    RoomAllocation = apps.get_model("core", "RoomAllocation")
    deleted, _ = RoomAllocation.objects.all().delete()
    print(f"    [room_allocation backfill] reverse: deleted {deleted} allocation row(s)")


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0042_room_allocation'),
    ]

    operations = [
        migrations.RunPython(backfill_room_allocations, remove_backfilled_room_allocations),
    ]
