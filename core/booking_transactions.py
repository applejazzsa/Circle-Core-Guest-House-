"""
Transactional, multi-room booking operations built on top of
core/availability.py::check_availability() and the RoomAllocation model.

This module is additive: it does not replace or get called by the existing
single-room booking_add/booking_edit/booking_cancel/booking_checkin/
booking_checkout views in core/views.py, which continue to work completely
unchanged for every tenant. It gives a whole-new capability — a booking made
of one or more room allocations, validated and priced all-or-nothing inside a
single locked transaction — ready for a future multi-room booking UI to call.

Every function here follows the same shape:
  1. transaction.atomic()
  2. Lock every room involved, in ascending pk order (a fixed global lock
     order avoids deadlocks between two concurrent multi-room bookings that
     touch overlapping room sets).
  3. Re-validate availability for every allocation while the locks are held,
     via the single shared check_availability() service.
  4. All-or-nothing: if ANY allocation fails, raise ValidationError with every
     failure message collected — nothing is written, and the transaction
     rolls back.
  5. Recalculate pricing per allocation and for the booking as a whole.
  6. Save the booking and its allocations.

Room-status side effects (Available/Occupied/Cleaning/...) are only applied
for WHOLE_ROOM rooms here, matching _sync_room_status in core/views.py.
Deriving a correct partial-occupancy status for SHARED_CAPACITY rooms was
explicitly deferred to a later phase in the original design doc, and isn't
touched by this module — mutating a shared dormitory's single `status` field
just because one of several concurrent bookings checked in/out would be
wrong, not merely incomplete.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .availability import check_availability
from .models import Booking, Room, RoomAllocation


def _price_allocation(room, allocated_guests, check_in, check_out):
    nights = max((check_out - check_in).days, 0)
    rate = room.get_price_for_duration("daily")
    if rate is None or rate <= 0:
        rate = room.price_per_night
    guest_multiplier = Decimal(allocated_guests) if room.pricing_model == "per_person" else Decimal("1")
    line_total = Decimal(rate) * guest_multiplier * nights
    return {"room": room, "allocated_guests": allocated_guests, "rate_per_night": rate, "line_total": line_total}


def _lock_rooms(room_ids):
    """Lock every room in the set, in a fixed ascending-pk order, so two
    concurrent multi-room bookings touching overlapping room sets can never
    deadlock against each other — they simply wait in the same order."""
    ordered_ids = sorted(set(room_ids))
    rooms = list(Room.objects.select_for_update().filter(pk__in=ordered_ids).order_by("pk"))
    if len(rooms) != len(ordered_ids):
        found = {room.pk for room in rooms}
        missing = [pk for pk in ordered_ids if pk not in found]
        raise ValidationError([f"Room id {pk} could not be found." for pk in missing])
    return {room.pk: room for room in rooms}


def _validate_allocations(rooms_by_id, allocation_requests, check_in, check_out, total_guests, prop, exclude_booking_id):
    errors = []
    seen_rooms = set()
    plans = []

    for request in allocation_requests:
        room = rooms_by_id[request["room"].pk]
        guests = request["allocated_guests"]

        if room.pk in seen_rooms:
            errors.append(f"{room.name} is listed more than once in this booking.")
            continue
        seen_rooms.add(room.pk)

        if prop is not None and room.prop_id != prop.pk:
            errors.append(f"{room.name} belongs to another property.")
            continue

        if guests is None or guests < 1:
            errors.append(f"{room.name} must have at least 1 allocated guest.")
            continue

        result = check_availability(room, check_in, check_out, guests, exclude_booking_id=exclude_booking_id)
        if not result.available:
            errors.append(result.reason)
            continue

        plans.append(_price_allocation(room, guests, check_in, check_out))

    if total_guests is not None:
        allocated_sum = sum(r["allocated_guests"] for r in allocation_requests if r["allocated_guests"])
        if allocated_sum != total_guests:
            errors.append("Allocated guest totals do not match the booking guest total.")

    return plans, errors


def create_multi_room_booking(
    *, guest, prop, check_in, check_out, allocations, total_guests,
    discount=Decimal("0.00"), deposit_required=Decimal("0.00"),
    booking_source="Walk-in", status="Pending", notes="",
):
    """
    allocations: list of {"room": Room, "allocated_guests": int}
    Returns the saved Booking (with .room_allocations populated).
    Raises ValidationError (with .messages listing every failure) and writes
    nothing if any allocation is invalid.
    """
    if check_out is None or check_in is None or check_out <= check_in:
        raise ValidationError(["Check-out date must be after check-in date."])
    if not allocations:
        raise ValidationError(["A booking must have at least one room allocation."])

    with transaction.atomic():
        rooms_by_id = _lock_rooms(a["room"].pk for a in allocations)
        plans, errors = _validate_allocations(
            rooms_by_id, allocations, check_in, check_out, total_guests, prop, exclude_booking_id=None
        )
        if errors:
            raise ValidationError(errors)

        primary = plans[0]
        booking = Booking(
            guest=guest,
            room=primary["room"],
            check_in_date=check_in,
            check_out_date=check_out,
            booking_duration_type="daily",
            num_guests=total_guests,
            rate_per_night=primary["rate_per_night"],
            discount=discount,
            deposit_required=deposit_required,
            status=status,
            booking_source=booking_source,
            notes=notes,
        )
        booking.save(skip_conflict_check=True)  # first save: no allocations exist yet, so compute_totals() falls back to single-room math transiently
        for plan in plans:
            RoomAllocation.objects.create(
                booking=booking,
                room=plan["room"],
                allocated_guests=plan["allocated_guests"],
                rate_per_night=plan["rate_per_night"],
                line_total=plan["line_total"],
            )
        booking.save(skip_conflict_check=True)  # second save: room_allocations now exist, so total_amount reflects the full multi-room sum
        return booking


def edit_multi_room_booking(
    booking, *, check_in=None, check_out=None, allocations=None, total_guests=None, discount=None,
):
    """
    Covers guest-count changes, date changes, adding/removing/changing a
    room, and changing allocated guests uniformly: pass the full desired
    allocation list and/or new dates; omitted arguments keep the booking's
    current values. The booking's own existing allocations are always
    excluded from their own occupancy calculation.
    """
    new_check_in = check_in if check_in is not None else booking.check_in_date
    new_check_out = check_out if check_out is not None else booking.check_out_date
    if new_check_out is None or new_check_in is None or new_check_out <= new_check_in:
        raise ValidationError(["Check-out date must be after check-in date."])

    if allocations is None:
        existing = list(booking.room_allocations.select_related("room").all())
        if existing:
            allocations = [{"room": a.room, "allocated_guests": a.allocated_guests} for a in existing]
        else:
            allocations = [{"room": booking.room, "allocated_guests": booking.num_guests}]
    if not allocations:
        raise ValidationError(["A booking must have at least one room allocation."])

    new_total_guests = total_guests if total_guests is not None else sum(a["allocated_guests"] for a in allocations)
    prop = booking.room.prop if booking.room_id else None

    with transaction.atomic():
        rooms_by_id = _lock_rooms(a["room"].pk for a in allocations)
        plans, errors = _validate_allocations(
            rooms_by_id, allocations, new_check_in, new_check_out, new_total_guests, prop,
            exclude_booking_id=booking.pk,
        )
        if errors:
            raise ValidationError(errors)

        primary = plans[0]
        booking.check_in_date = new_check_in
        booking.check_out_date = new_check_out
        booking.num_guests = new_total_guests
        booking.room = primary["room"]
        booking.rate_per_night = primary["rate_per_night"]
        if discount is not None:
            booking.discount = discount

        booking.room_allocations.all().delete()
        for plan in plans:
            RoomAllocation.objects.create(
                booking=booking,
                room=plan["room"],
                allocated_guests=plan["allocated_guests"],
                rate_per_night=plan["rate_per_night"],
                line_total=plan["line_total"],
            )
        booking.save(skip_conflict_check=True)
        return booking


def _booking_rooms(booking):
    allocations = list(booking.room_allocations.select_related("room").all())
    if allocations:
        return [a.room for a in allocations]
    return [booking.room] if booking.room_id else []


def cancel_multi_room_booking(booking):
    """Cancelling always succeeds — it only ever releases capacity, never consumes it."""
    with transaction.atomic():
        _lock_rooms(room.pk for room in _booking_rooms(booking))
        booking.status = "Cancelled"
        booking.save(skip_conflict_check=True)
        for room in _booking_rooms(booking):
            if room.effective_booking_mode == "WHOLE_ROOM" and room.status not in ("Maintenance", "Blocked"):
                room.status = "Available"
                room.save(update_fields=["status"])
        return booking


def reinstate_multi_room_booking(booking, new_status="Confirmed"):
    """Reinstating a cancelled/no-show booking must recheck capacity — it may
    have been taken by someone else in the meantime."""
    allocations = list(booking.room_allocations.select_related("room").all())
    if allocations:
        allocation_requests = [{"room": a.room, "allocated_guests": a.allocated_guests} for a in allocations]
    else:
        allocation_requests = [{"room": booking.room, "allocated_guests": booking.num_guests}]
    prop = booking.room.prop if booking.room_id else None

    with transaction.atomic():
        rooms_by_id = _lock_rooms(a["room"].pk for a in allocation_requests)
        plans, errors = _validate_allocations(
            rooms_by_id, allocation_requests, booking.check_in_date, booking.check_out_date,
            booking.num_guests, prop, exclude_booking_id=booking.pk,
        )
        if errors:
            raise ValidationError(errors)

        booking.status = new_status
        booking.save(skip_conflict_check=True)
        for room in _booking_rooms(booking):
            if room.effective_booking_mode == "WHOLE_ROOM":
                room.status = "Booked"
                room.save(update_fields=["status"])
        return booking


def check_in_multi_room_booking(booking):
    with transaction.atomic():
        _lock_rooms(room.pk for room in _booking_rooms(booking))
        if booking.status not in ("Confirmed", "Pending"):
            raise ValidationError(["Only pending or confirmed bookings can be checked in."])
        booking.status = "Checked In"
        booking.check_in_time = timezone.now()
        booking.save(skip_conflict_check=True)
        for room in _booking_rooms(booking):
            if room.effective_booking_mode == "WHOLE_ROOM":
                room.status = "Occupied"
                room.save(update_fields=["status"])
        return booking


def check_out_multi_room_booking(booking):
    with transaction.atomic():
        _lock_rooms(room.pk for room in _booking_rooms(booking))
        if booking.status != "Checked In":
            raise ValidationError(["Only checked-in bookings can be checked out."])
        booking.status = "Checked Out"
        booking.check_out_time = timezone.now()
        booking.save(skip_conflict_check=True)
        for room in _booking_rooms(booking):
            if room.effective_booking_mode == "WHOLE_ROOM":
                room.status = "Cleaning"
                room.cleaning_status = "Needs Cleaning"
                room.save(update_fields=["status", "cleaning_status"])
        return booking
