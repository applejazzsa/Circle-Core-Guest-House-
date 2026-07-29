"""
Transactional, multi-room booking operations built on top of
core/availability.py::check_availability() and the RoomAllocation model.

This module is additive: single-room WHOLE_ROOM bookings still go through
the plain booking_add/booking_edit/booking_cancel views in core/views.py
completely unchanged. For SHARED_CAPACITY rooms, core/views.py's
booking_checkin/booking_checkout now delegate here (see
check_in_multi_room_booking/check_out_multi_room_booking below) so that one
occupant checking in/out never disturbs any other occupant of the same room
— every tenant without the feature enabled is entirely untouched, since
Room.effective_booking_mode can only ever be SHARED_CAPACITY when the
tenant's own flag is on.

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

Room-status side effects (Available/Occupied/Cleaning/...): for WHOLE_ROOM
rooms these mirror _sync_room_status in core/views.py exactly. For
SHARED_CAPACITY rooms, the room's single `status` field only ever moves on
the two edges that make sense for a shared dormitory — the first check-in
into an empty room, and the last check-out that leaves it empty — never on
every individual occupant's own check-in/check-out, since other occupants
may still be present. The room's real partial-occupancy state is (and
remains) computed on the fly by shared_room_status_label()/
occupancy_snapshot(), not stored.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone

from .availability import check_availability
from .models import AuditLog, Booking, GuestHouseSettings, Payment, Room, RoomAllocation


def _price_allocation(room, allocated_guests, check_in, check_out, rate_override=None):
    nights = max((check_out - check_in).days, 0)
    if rate_override is not None:
        rate = rate_override
    else:
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

        plans.append(_price_allocation(room, guests, check_in, check_out, rate_override=request.get("rate_override")))

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


def create_individual_shared_room_booking(
    *, guest, room, check_in, check_out, allocated_guests=1, rate=None,
    booking_source="Walk-in", payment_info=None, notes="", staff_user=None,
    tenant=None, prop=None, status="Confirmed",
):
    """
    Book one independently paying guest (or a small family/group sharing a
    single payment account, when allocated_guests > 1) into a
    SHARED_CAPACITY room, alongside whichever other guests already occupy it.

    Every call is a thin, validated wrapper around create_multi_room_booking()
    with exactly one room in the allocation list — it creates a brand-new
    Booking + its own RoomAllocation and never reads or rewrites any other
    booking already on the room. Existing occupants are only ever
    re-validated for remaining capacity (under the same room lock), never
    modified.

    tenant is accepted for audit-trail context only: this app isolates
    tenants by Postgres schema (see tenants/models.py, config/settings.py
    TENANT_APPS) — there is no tenant_id column on Room to check against,
    same as every other model in this app. The real "this room belongs to
    this tenant" guarantee is structural: the caller must already be
    operating against the correct tenant's schema (exactly how every other
    view in core/views.py resolves rooms via the request's active Property),
    and _lock_rooms() below re-fetches the room fresh under whatever schema
    is currently connected — a room id that doesn't exist in the current
    schema fails closed with "Room id ... could not be found." Passing prop
    additionally re-verifies the room belongs to the expected Property, the
    same check create_multi_room_booking() already applies for group bookings.

    Raises ValidationError (with .messages) and writes nothing if any check
    fails. Returns the saved Booking.
    """
    settings_obj = GuestHouseSettings.objects.filter(pk=1).first()
    if not settings_obj or not settings_obj.shared_capacity_booking_enabled:
        raise ValidationError(["Shared-capacity booking is not enabled for this property."])

    if room.effective_booking_mode != "SHARED_CAPACITY":
        raise ValidationError([f"{room.name} is not configured for shared-capacity booking."])

    if room.status in ("Maintenance", "Blocked", "Cleaning"):
        labels = {"Maintenance": "under maintenance", "Blocked": "blocked", "Cleaning": "currently being cleaned"}
        raise ValidationError([f"{room.name} is {labels.get(room.status, room.status.lower())} and cannot be booked."])

    if not isinstance(allocated_guests, int) or isinstance(allocated_guests, bool) or allocated_guests < 1:
        raise ValidationError(["Allocated guest spaces must be a positive whole number."])

    # prop is only used for the same "belongs to this property" check
    # create_multi_room_booking() already applies for group bookings — it is
    # deliberately NOT defaulted to room.prop here, since room may be a stale
    # Python object read under a different tenant's schema (see the tenant
    # note in this function's docstring); the real "does this room actually
    # exist in the caller's own tenant schema" guarantee comes from
    # _lock_rooms() re-fetching it fresh under whichever schema is currently
    # connected, a moment from now.
    booking = create_multi_room_booking(
        guest=guest,
        prop=prop,
        check_in=check_in,
        check_out=check_out,
        allocations=[{"room": room, "allocated_guests": allocated_guests, "rate_override": rate}],
        total_guests=allocated_guests,
        booking_source=booking_source,
        status=status,
        notes=notes,
    )

    if payment_info:
        Payment.objects.create(
            booking=booking,
            amount=payment_info["amount"],
            payment_method=payment_info.get("payment_method", "Cash"),
            payment_type=payment_info.get("payment_type", "Payment"),
            reference=payment_info.get("reference", ""),
            notes=payment_info.get("notes", ""),
        )
        booking.refresh_from_db()

    allocation = booking.room_allocations.get(room=room)
    AuditLog.objects.create(
        actor=staff_user,
        action="create",
        object_type="Booking",
        object_id=str(booking.pk),
        object_repr=str(booking)[:255],
        after={
            "tenant_schema": tenant.schema_name if tenant is not None else connection.schema_name,
            "booking_reference": booking.booking_reference,
            "guest": getattr(guest, "full_name", str(guest)),
            "room": room.name,
            "allocated_guests": allocated_guests,
            "check_in_date": check_in.isoformat(),
            "check_out_date": check_out.isoformat(),
            "rate_per_night": str(allocation.rate_per_night),
            "calculated_amount": str(allocation.line_total),
        },
        reason="Individual shared-capacity guest booking",
    )
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
        was_checked_in = booking.status == "Checked In"
        booking.status = "Cancelled"
        booking.save(skip_conflict_check=True)
        for room in _booking_rooms(booking):
            if room.effective_booking_mode == "WHOLE_ROOM" and room.status not in ("Maintenance", "Blocked"):
                room.status = "Available"
                room.save(update_fields=["status"])
            elif (
                room.effective_booking_mode == "SHARED_CAPACITY"
                and was_checked_in
                and room.status not in ("Maintenance", "Blocked")
                and not _other_checked_in_bookings_exist(room, booking.pk)
            ):
                # Matches the WHOLE_ROOM convention above: cancelling always
                # just releases the room, it never implies a cleaning turnover.
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


def _other_checked_in_bookings_exist(room, exclude_booking_id):
    return (
        Booking.objects.filter(room=room, status="Checked In")
        .exclude(pk=exclude_booking_id)
        .exists()
    )


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
            elif room.status == "Available":
                # First occupant into an empty shared room — flip the room's
                # own display status the same way a WHOLE_ROOM check-in would,
                # without implying anything about remaining shared capacity
                # (that's computed separately by shared_room_status_label()).
                room.status = "Occupied"
                room.save(update_fields=["status"])
        return booking


def check_out_multi_room_booking(booking, staff_user=None):
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
                continue

            remaining_occupants = Booking.objects.filter(room=room, status="Checked In").exclude(pk=booking.pk).count()
            if remaining_occupants == 0:
                # This was the last occupant still checked into this shared
                # room — now, and only now, does it actually need cleaning,
                # following the existing whole-room cleaning workflow.
                if room.status not in ("Maintenance", "Blocked"):
                    room.status = "Cleaning"
                    room.cleaning_status = "Needs Cleaning"
                    room.save(update_fields=["status", "cleaning_status"])
            else:
                # Other guests remain checked in — the room stays in
                # inventory and its status/cleaning_status are untouched
                # (never "Available", never "Cleaning" for the whole room
                # over one vacated space). This app has no per-bed/per-space
                # housekeeping model, so the turnover need is recorded as a
                # staff-only note instead of a whole-room status change.
                AuditLog.objects.create(
                    actor=staff_user,
                    action="update",
                    object_type="Room",
                    object_id=str(room.pk),
                    object_repr=str(room)[:255],
                    after={
                        "housekeeping_note": (
                            f"{getattr(booking.guest, 'full_name', str(booking.guest))} checked out of "
                            f"{booking.num_guests} space(s) in {room.name}; {remaining_occupants} other "
                            f"occupant(s) remain checked in — partial turnover, room stays in service."
                        ),
                        "booking_reference": booking.booking_reference,
                        "vacated_spaces": booking.num_guests,
                        "remaining_occupants": remaining_occupants,
                    },
                    reason="Partial shared-room turnover (no per-bed housekeeping tracking)",
                )
        return booking
