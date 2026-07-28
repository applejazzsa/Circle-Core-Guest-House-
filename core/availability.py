"""
Single source of truth for room availability, covering both WHOLE_ROOM and
SHARED_CAPACITY inventory. Every booking-creation/edit path (BookingForm,
Booking.save()) calls check_availability() here rather than re-implementing
overlap or capacity detection — see the calls from BookingForm.clean() and
Booking.validate_room_conflict().

Scope: date-range ("nightly") bookings, matching the overlap rule

    existing_check_in < requested_check_out AND existing_check_out > requested_check_in

Hourly bookings (Booking.is_hourly — booking_duration_type in 1_hour/2_hours/
3_hours/5_hours) use time-of-day windows rather than whole dates, and continue
to use Booking.overlapping_bookings()/_booking_window(); extending this
service to hour-level overlap was not part of the shared-capacity work this
service supports, so both call sites still branch on is_hourly for that case.

Status mapping — no invented status names, derived directly from the
existing Booking model so the two can never drift apart:
  reserving:      Pending, Confirmed, Checked In
  non-reserving:  Cancelled, No Show, Checked Out   (Booking.INACTIVE_STATUSES)
This application has no "temporary hold" or "rejected" status, so neither is
invented here.

Tenant scoping: this app isolates tenants by Postgres schema (see
tenants/models.py, config/settings.py TENANT_APPS), not by a shared tenant_id
column, so every query here is already tenant-scoped by construction — the
connection can only ever see the current schema's own Room/Booking/
RoomAllocation rows. The `room` passed in is assumed already resolved by the
caller (e.g. via the request's active Property), matching how every other
view in this codebase resolves rooms.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

from .models import Booking, GuestHouseSettings, RoomAllocation

# Derived from the existing Booking status choices — never hand-maintained
# separately, so this can never silently drift from Booking.INACTIVE_STATUSES.
RESERVING_STATUSES = [
    value for value, _label in Booking.STATUS_CHOICES if value not in Booking.INACTIVE_STATUSES
]


@dataclass
class AvailabilityResult:
    available: bool
    effective_mode: str
    max_capacity: int
    occupied_capacity: int
    remaining_capacity: int
    requested_guests: int
    conflicting_allocations: list = field(default_factory=list)
    first_failing_date: object = None
    reason: str = ""
    diagnostic: str = ""
    # Bonus field beyond the required set: per-night occupancy, useful for
    # diagnostics and for a future capacity-aware availability screen.
    nightly_occupancy: dict = field(default_factory=dict)


def _nights(check_in, check_out):
    return [check_in + timedelta(days=offset) for offset in range((check_out - check_in).days)]


def check_availability(room, check_in, check_out, requested_guests, *, exclude_booking_id=None):
    """
    Determine whether `requested_guests` can be booked into `room` for
    [check_in, check_out). Pass exclude_booking_id when validating an edit to
    a booking that already occupies this room, so it doesn't conflict with
    itself.
    """
    if not isinstance(check_in, date) or not isinstance(check_out, date):
        return AvailabilityResult(
            available=False,
            effective_mode=getattr(room, "effective_booking_mode", "WHOLE_ROOM"),
            max_capacity=getattr(room, "max_guests", 0),
            occupied_capacity=0,
            remaining_capacity=0,
            requested_guests=requested_guests or 0,
            reason="Check-in and check-out dates are required.",
            diagnostic=f"check_availability called with non-date check_in={check_in!r} check_out={check_out!r}",
        )

    if check_out <= check_in:
        return AvailabilityResult(
            available=False,
            effective_mode=room.effective_booking_mode,
            max_capacity=room.max_guests,
            occupied_capacity=0,
            remaining_capacity=0,
            requested_guests=requested_guests or 0,
            reason="Check-out date must be after check-in date.",
            diagnostic=f"invalid stay: check_in={check_in} check_out={check_out}",
        )

    if requested_guests is None or requested_guests < 1:
        return AvailabilityResult(
            available=False,
            effective_mode=room.effective_booking_mode,
            max_capacity=room.max_guests,
            occupied_capacity=0,
            remaining_capacity=0,
            requested_guests=requested_guests or 0,
            reason="Number of guests must be at least 1.",
            diagnostic=f"invalid requested_guests={requested_guests!r}",
        )

    mode = room.effective_booking_mode
    if mode == "SHARED_CAPACITY":
        return _check_shared_capacity(room, check_in, check_out, requested_guests, exclude_booking_id)
    return _check_whole_room(room, check_in, check_out, requested_guests, exclude_booking_id)


def _check_whole_room(room, check_in, check_out, requested_guests, exclude_booking_id):
    # One query: push the date-overlap rule into SQL rather than looping in
    # Python over every booking for this room.
    overlapping = Booking.objects.filter(
        room=room,
        status__in=RESERVING_STATUSES,
        check_in_date__lt=check_out,
        check_out_date__gt=check_in,
    )
    if exclude_booking_id:
        overlapping = overlapping.exclude(pk=exclude_booking_id)
    conflicts = list(overlapping.select_related("guest"))

    if conflicts:
        blocker = conflicts[0]
        return AvailabilityResult(
            available=False,
            effective_mode="WHOLE_ROOM",
            max_capacity=room.max_guests,
            occupied_capacity=room.max_guests,
            remaining_capacity=0,
            requested_guests=requested_guests,
            conflicting_allocations=conflicts,
            first_failing_date=max(blocker.check_in_date, check_in),
            reason=(
                f"{room.name} is already booked for that time "
                f"({blocker.booking_reference} - {blocker.guest.full_name})."
            ),
            diagnostic=f"WHOLE_ROOM conflict: {len(conflicts)} overlapping reserving booking(s) on room {room.pk}",
        )

    return AvailabilityResult(
        available=True,
        effective_mode="WHOLE_ROOM",
        max_capacity=room.max_guests,
        occupied_capacity=0,
        remaining_capacity=room.max_guests,
        requested_guests=requested_guests,
        reason="Room is available for the requested dates.",
        diagnostic="WHOLE_ROOM: no overlapping reserving bookings",
    )


def _check_shared_capacity(room, check_in, check_out, requested_guests, exclude_booking_id):
    nights = _nights(check_in, check_out)

    # One query for the whole stay: fetch every overlapping, reserving
    # allocation once, then aggregate per night in Python — never one query
    # per room per night.
    allocations = RoomAllocation.objects.filter(
        room=room,
        booking__status__in=RESERVING_STATUSES,
        booking__check_in_date__lt=check_out,
        booking__check_out_date__gt=check_in,
    ).select_related("booking", "booking__guest")
    if exclude_booking_id:
        allocations = allocations.exclude(booking_id=exclude_booking_id)
    allocations = list(allocations)

    nightly_occupancy = {}
    contributors_by_night = {}
    for night in nights:
        occupied = 0
        contributors = []
        for allocation in allocations:
            booking = allocation.booking
            if booking.check_in_date <= night < booking.check_out_date:
                occupied += allocation.allocated_guests
                contributors.append(booking)
        nightly_occupancy[night] = occupied
        contributors_by_night[night] = contributors

    remaining_by_night = {night: room.max_guests - occupied for night, occupied in nightly_occupancy.items()}
    min_remaining = min(remaining_by_night.values())
    available = all(remaining >= requested_guests for remaining in remaining_by_night.values())

    if available:
        return AvailabilityResult(
            available=True,
            effective_mode="SHARED_CAPACITY",
            max_capacity=room.max_guests,
            occupied_capacity=room.max_guests - min_remaining,
            remaining_capacity=min_remaining,
            requested_guests=requested_guests,
            reason="Enough shared capacity is available for every night of the stay.",
            diagnostic=f"SHARED_CAPACITY: min remaining={min_remaining} across {len(nights)} night(s)",
            nightly_occupancy=nightly_occupancy,
        )

    first_failing = next(night for night in nights if remaining_by_night[night] < requested_guests)
    return AvailabilityResult(
        available=False,
        effective_mode="SHARED_CAPACITY",
        max_capacity=room.max_guests,
        occupied_capacity=nightly_occupancy[first_failing],
        remaining_capacity=remaining_by_night[first_failing],
        requested_guests=requested_guests,
        conflicting_allocations=contributors_by_night[first_failing],
        first_failing_date=first_failing,
        reason=(
            f"{room.name} only has {remaining_by_night[first_failing]} of {requested_guests} "
            f"requested spaces available on {first_failing:%Y-%m-%d}."
        ),
        diagnostic=(
            f"SHARED_CAPACITY: night {first_failing} occupied={nightly_occupancy[first_failing]} "
            f"max={room.max_guests} requested={requested_guests}"
        ),
        nightly_occupancy=nightly_occupancy,
    )


# ── Display/reporting helpers ──────────────────────────────────────────────
#
# These back the room list, room calendar, and availability search screens.
# They deliberately do NOT go through check_availability(), which requires a
# requested_guests >= 1 and is meant for validating a proposed booking, not
# for reporting today's/a range's occupancy. Both helpers below issue exactly
# one query regardless of how many rooms or days are being displayed —
# never one query per room per night.

# Operational states that take a room out of service regardless of capacity,
# matching the exact set Room.validate_room_available() already blocks on.
OUT_OF_SERVICE_STATUSES = ("Maintenance", "Blocked", "Cleaning")


def effective_modes_for_rooms(rooms):
    """
    {room.pk: "SHARED_CAPACITY"|"WHOLE_ROOM"} for every room in `rooms`,
    fetching the tenant's shared_capacity_booking_enabled flag exactly once.
    Room.effective_booking_mode queries GuestHouseSettings every time it's
    called — correct and cheap for validating one booking, but an N+1 query
    bug if called per-room inside a list/template loop. Every display screen
    that iterates a room list should use this instead.
    """
    settings_obj = GuestHouseSettings.objects.filter(pk=1).first()
    enabled = bool(settings_obj and settings_obj.shared_capacity_booking_enabled)
    return {
        room.pk: ("SHARED_CAPACITY" if (enabled and room.booking_mode == "SHARED_CAPACITY") else "WHOLE_ROOM")
        for room in rooms
    }


def shared_room_status_label(occupied, max_capacity, room_status):
    """Available / Partially Occupied / Full / Out of Service for a
    SHARED_CAPACITY room, given already-computed occupancy."""
    if room_status in OUT_OF_SERVICE_STATUSES:
        return "Out of Service"
    if occupied <= 0:
        return "Available"
    if occupied >= max_capacity:
        return "Full"
    return "Partially Occupied"


def bulk_occupancy_by_room_and_date(room_ids, start_date, end_date):
    """
    Occupied-guest counts and contributing bookings for every room id in
    `room_ids`, for every night in [start_date, end_date) — in one query.

    Returns {(room_id, date): {"occupied": int, "bookings": [Booking, ...]}}.
    A (room_id, date) pair with no active allocation simply has no entry —
    callers should treat a missing key as occupied=0.
    """
    room_ids = list(room_ids)
    if not room_ids or end_date <= start_date:
        return {}

    allocations = (
        RoomAllocation.objects.filter(
            room_id__in=room_ids,
            booking__status__in=RESERVING_STATUSES,
            booking__check_in_date__lt=end_date,
            booking__check_out_date__gt=start_date,
        )
        .select_related("booking", "booking__guest")
    )

    nights = _nights(start_date, end_date)
    result = {}
    for allocation in allocations:
        booking = allocation.booking
        for night in nights:
            if booking.check_in_date <= night < booking.check_out_date:
                entry = result.setdefault((allocation.room_id, night), {"occupied": 0, "bookings": []})
                entry["occupied"] += allocation.allocated_guests
                entry["bookings"].append(booking)
    return result


def occupancy_snapshot(room, on_date):
    """(occupied, max_capacity, remaining) for a single SHARED_CAPACITY room
    on a single date — one query. For bulk/multi-room/multi-day use,
    call bulk_occupancy_by_room_and_date() instead."""
    occupancy = bulk_occupancy_by_room_and_date([room.pk], on_date, on_date + timedelta(days=1))
    occupied = occupancy.get((room.pk, on_date), {"occupied": 0})["occupied"]
    return occupied, room.max_guests, room.max_guests - occupied
