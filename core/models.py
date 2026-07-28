import datetime
import uuid
from datetime import timedelta, time
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.contrib.auth.hashers import check_password, make_password
from django.db import models
from django.db.models import Sum
from django.utils import timezone


class Property(models.Model):
    name = models.CharField(max_length=200)
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    email = models.EmailField(blank=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "Properties"
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name

    @property
    def room_count(self):
        return self.rooms.count()


class StaffProfile(models.Model):
    ROLE_CHOICES = [
        ("Owner", "Owner / Admin"),
        ("Manager", "Manager"),
        ("Reception", "Reception"),
        ("Cleaner", "Cleaner"),
        ("Viewer", "Viewer"),
        ("Operator", "Operator (legacy)"),
    ]

    user = models.OneToOneField(
        "auth.User",
        on_delete=models.CASCADE,
        related_name="staff_profile",
    )
    phone_number = models.CharField(max_length=20, unique=True, null=True, blank=True)
    pin_hash = models.CharField(max_length=128, blank=True)
    pin_enabled = models.BooleanField(default=False)
    pin_failed_attempts = models.PositiveSmallIntegerField(default=0)
    pin_locked_until = models.DateTimeField(null=True, blank=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default="Viewer")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["user__username"]

    def __str__(self):
        return f"{self.user.username} staff profile"

    @staticmethod
    def normalize_phone(value):
        raw = (value or "").strip()
        digits = "".join(character for character in raw if character.isdigit())
        if len(digits) == 10 and digits.startswith("0"):
            return f"+27{digits[1:]}"
        if len(digits) == 11 and digits.startswith("27"):
            return f"+{digits}"
        if raw.startswith("+") and digits:
            return f"+{digits}"
        return digits

    @staticmethod
    def validate_pin(pin):
        value = str(pin or "")
        if not value.isdigit() or not 4 <= len(value) <= 6:
            raise ValidationError("PIN must contain 4 to 6 digits.")
        return value

    def set_pin(self, pin):
        self.pin_hash = make_password(self.validate_pin(pin))

    def check_pin(self, pin):
        return bool(self.pin_hash) and check_password(str(pin or ""), self.pin_hash)

    def disable_pin(self):
        self.pin_enabled = False
        self.pin_hash = ""
        self.pin_failed_attempts = 0
        self.pin_locked_until = None


class OfflineDevice(models.Model):
    client_id = models.UUIDField(unique=True, default=uuid.uuid4, editable=False)
    prop = models.ForeignKey(Property, on_delete=models.CASCADE, related_name="offline_devices")
    user = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="offline_devices")
    label = models.CharField(max_length=120)
    is_active = models.BooleanField(default=False)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="approved_offline_devices")
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class OfflineOperation(models.Model):
    STATUS_CHOICES = [("applied", "Applied"), ("conflict", "Conflict"), ("rejected", "Rejected")]
    device = models.ForeignKey(OfflineDevice, on_delete=models.CASCADE, related_name="operations")
    operation_id = models.UUIDField()
    operation_type = models.CharField(max_length=40)
    payload = models.JSONField(default=dict)
    payload_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES)
    result = models.JSONField(default=dict, blank=True)
    error = models.CharField(max_length=255, blank=True)
    client_created_at = models.DateTimeField()
    processed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["device", "operation_id"], name="unique_offline_device_operation")]
        ordering = ["-processed_at"]


class OfflineConflict(models.Model):
    operation = models.OneToOneField(OfflineOperation, on_delete=models.CASCADE, related_name="conflict")
    reason = models.CharField(max_length=255)
    server_state = models.JSONField(default=dict, blank=True)
    is_resolved = models.BooleanField(default=False)
    resolution = models.CharField(max_length=30, blank=True)
    resolved_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)


class RatePlan(models.Model):
    PRICING_BASIS_CHOICES = [
        ("per_person_per_night", "Per Person Per Night"),
        ("per_room_per_night", "Per Room Per Night"),
    ]

    name = models.CharField(max_length=100, unique=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default="ZAR")
    pricing_basis = models.CharField(max_length=25, choices=PRICING_BASIS_CHOICES, default="per_person_per_night")
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def apply_to_rooms(self):
        for room in self.rooms.all():
            room.save()


class RoomType(models.Model):
    GENDER_RESTRICTION_CHOICES = [
        ("none", "No Restriction"),
        ("ladies", "Ladies Only"),
        ("gentlemen", "Gentlemen Only"),
    ]
    BATHROOM_TYPE_CHOICES = [
        ("private", "Private"),
        ("communal", "Communal"),
        ("pending", "Pending Confirmation"),
    ]

    name = models.CharField(max_length=100, unique=True)
    bathroom_type = models.CharField(max_length=10, choices=BATHROOM_TYPE_CHOICES, default="private")
    bathroom_description = models.CharField(max_length=255, blank=True)
    gender_restriction = models.CharField(max_length=10, choices=GENDER_RESTRICTION_CHOICES, default="none")
    is_wheelchair_accessible = models.BooleanField(default=False)
    area = models.CharField(max_length=100, blank=True)
    default_rate_plan = models.ForeignKey(
        RatePlan, null=True, blank=True, on_delete=models.SET_NULL, related_name="room_types"
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "Room Types"

    def __str__(self):
        return self.name


class Room(models.Model):
    ROOM_TYPE_CHOICES = [
        ("Single", "Single"),
        ("Double", "Double"),
        ("Twin", "Twin"),
        ("Family", "Family"),
        ("Suite", "Suite"),
        ("En-Suite", "En-Suite"),
        ("Self-Catering", "Self-Catering"),
        ("Dormitory", "Dormitory"),
    ]
    PRICING_MODEL_CHOICES = [
        ("per_room", "Per Room"),
        ("per_person", "Per Person"),
    ]
    BOOKING_MODE_CHOICES = [
        ("WHOLE_ROOM", "Whole Room"),
        ("SHARED_CAPACITY", "Shared Capacity"),
    ]
    STATUS_CHOICES = [
        ("Available", "Available"),
        ("Booked", "Booked"),
        ("Occupied", "Occupied"),
        ("Cleaning", "Cleaning"),
        ("Maintenance", "Maintenance"),
        ("Blocked", "Blocked"),
    ]
    CLEANING_STATUS_CHOICES = [
        ("Clean", "Clean"),
        ("Needs Cleaning", "Needs Cleaning"),
        ("In Progress", "In Progress"),
    ]
    BOOKING_TYPE_CHOICES = [
        ("Hourly", "Hourly"),
        ("Daily", "Daily"),
        ("Weekly", "Weekly"),
    ]

    prop = models.ForeignKey(
        "Property", on_delete=models.CASCADE, null=True, blank=True, related_name="rooms"
    )
    name = models.CharField(max_length=150)
    room_type = models.CharField(max_length=20, choices=ROOM_TYPE_CHOICES)
    room_category = models.ForeignKey(
        RoomType, null=True, blank=True, on_delete=models.SET_NULL, related_name="rooms"
    )
    rate_plan = models.ForeignKey(
        RatePlan, null=True, blank=True, on_delete=models.SET_NULL, related_name="rooms"
    )
    pricing_model = models.CharField(max_length=20, choices=PRICING_MODEL_CHOICES, default="per_room")
    booking_mode = models.CharField(max_length=20, choices=BOOKING_MODE_CHOICES, default="WHOLE_ROOM")
    price_1_hour = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    price_2_hours = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    price_3_hours = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    price_5_hours = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    price_per_night = models.DecimalField(max_digits=10, decimal_places=2, db_column="base_price")
    price_per_week = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    booking_types_allowed = models.CharField(max_length=50, default="Daily")
    max_guests = models.IntegerField(default=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="Available")
    description = models.TextField(blank=True)
    internal_notes = models.TextField(blank=True)
    cleaning_status = models.CharField(max_length=20, choices=CLEANING_STATUS_CHOICES, default="Clean")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.rate_plan_id:
            self.price_per_night = self.rate_plan.amount
            self.pricing_model = (
                "per_person" if self.rate_plan.pricing_basis == "per_person_per_night" else "per_room"
            )
        super().save(*args, **kwargs)

    @property
    def base_price(self):
        return self.price_per_night

    @base_price.setter
    def base_price(self, value):
        self.price_per_night = value

    @property
    def effective_booking_mode(self):
        """Shared capacity only ever applies when the tenant has explicitly enabled it."""
        if self.booking_mode == "SHARED_CAPACITY":
            settings_obj = GuestHouseSettings.objects.filter(pk=1).first()
            if settings_obj and settings_obj.shared_capacity_booking_enabled:
                return "SHARED_CAPACITY"
        return "WHOLE_ROOM"

    def booking_types_list(self):
        return [item.strip() for item in self.booking_types_allowed.split(",") if item.strip()]

    def get_price_for_duration(self, duration_type):
        settings_obj = None
        try:
            settings_obj = GuestHouseSettings.objects.filter(pk=1).first()
        except Exception:
            settings_obj = None
        minimum_hourly_rate = getattr(settings_obj, "minimum_hourly_rate", None) if settings_obj else None
        default_2_hours = getattr(settings_obj, "default_price_2_hours", None) if settings_obj else None
        default_3_hours = getattr(settings_obj, "default_price_3_hours", None) if settings_obj else None
        if (default_2_hours is None or default_2_hours <= 0) and minimum_hourly_rate and minimum_hourly_rate > 0:
            default_2_hours = minimum_hourly_rate * Decimal("2")
        if (default_3_hours is None or default_3_hours <= 0) and minimum_hourly_rate and minimum_hourly_rate > 0:
            default_3_hours = minimum_hourly_rate * Decimal("3")

        default_prices = {
            "1_hour": minimum_hourly_rate,
            "2_hours": default_2_hours,
            "3_hours": default_3_hours,
            "daily": getattr(settings_obj, "default_price_per_night", None) if settings_obj else None,
            "24_hours": getattr(settings_obj, "default_price_24_hours", None) if settings_obj else None,
        }
        # Per-person rooms are always priced from their own configured rate — a
        # property-wide "default nightly rate" is a per-room fallback and would
        # silently replace a per-head rate with an unrelated flat-room amount.
        if self.pricing_model != "per_person":
            default_price = default_prices.get(duration_type)
            if default_price is not None and default_price > 0:
                return default_price

        prices = {
            "1_hour": self.price_1_hour,
            "2_hours": self.price_2_hours,
            "3_hours": self.price_3_hours,
            "5_hours": self.price_5_hours,
            "daily": self.price_per_night,
            # No per-room field backs "24 hours" — it only has a property-wide rate,
            # which isn't a meaningful concept for a per-person room.
            "24_hours": (None if self.pricing_model == "per_person" else (getattr(settings_obj, "default_price_24_hours", None) if settings_obj else None)),
            "weekly": self.price_per_week,
        }
        price = prices.get(duration_type)
        if price is not None:
            return price
        if self.pricing_model == "per_person":
            return None
        return default_prices.get(duration_type)


class Guest(models.Model):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    phone = models.CharField(max_length=50)
    email = models.EmailField(blank=True)
    id_passport_number = models.CharField(max_length=100, blank=True)
    address = models.TextField(blank=True)
    emergency_contact_name = models.CharField(max_length=150, blank=True)
    emergency_contact_phone = models.CharField(max_length=50, blank=True)
    notes = models.TextField(blank=True)
    is_generic = models.BooleanField(default=False)
    vehicle_registration = models.CharField(max_length=20, unique=True, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-is_generic", "last_name", "first_name"]

    def __str__(self):
        return self.full_name

    @classmethod
    def get_generic(cls):
        guest, _ = cls.objects.get_or_create(
            is_generic=True,
            defaults={
                "first_name": "Walk-in",
                "last_name": "Guest",
                "phone": "N/A",
            },
        )
        return guest

    @classmethod
    def get_or_create_for_vehicle(cls, registration):
        registration = " ".join((registration or "").strip().upper().split())
        if not registration:
            raise ValueError("A vehicle registration is required.")
        guest, _ = cls.objects.get_or_create(
            vehicle_registration=registration,
            defaults={
                "first_name": "Vehicle",
                "last_name": registration,
                "phone": "N/A",
            },
        )
        return guest

    @property
    def full_name(self):
        if self.is_generic:
            return "Walk-in Guest"
        if self.vehicle_registration:
            return f"Vehicle {self.vehicle_registration}"
        return f"{self.first_name} {self.last_name}"

    @property
    def is_vehicle_profile(self):
        return bool(self.vehicle_registration)

    def save(self, *args, **kwargs):
        if self.vehicle_registration:
            self.vehicle_registration = " ".join(self.vehicle_registration.strip().upper().split())
        else:
            self.vehicle_registration = None
        super().save(*args, **kwargs)

    @property
    def total_stays(self):
        try:
            return self.bookings.filter(status="Checked Out").count()
        except AttributeError:
            return 0

    @property
    def is_repeat_guest(self):
        try:
            return self.bookings.count() > 1
        except AttributeError:
            return False


class Booking(models.Model):
    BOOKING_SOURCE_CHOICES = [
        ("Walk-in", "Walk-in"),
        ("Phone Call", "Phone Call"),
        ("WhatsApp", "WhatsApp"),
        ("Booking.com", "Booking.com"),
        ("Airbnb", "Airbnb"),
        ("Website", "Website"),
        ("Referral", "Referral"),
        ("Other", "Other"),
    ]
    STATUS_CHOICES = [
        ("Pending", "Pending"),
        ("Confirmed", "Confirmed"),
        ("Checked In", "Checked In"),
        ("Checked Out", "Checked Out"),
        ("Cancelled", "Cancelled"),
        ("No Show", "No Show"),
    ]
    INACTIVE_STATUSES = ["Cancelled", "No Show", "Checked Out"]
    DURATION_CHOICES = [
        ("1_hour", "1 Hour"),
        ("2_hours", "2 Hours"),
        ("3_hours", "3 Hours"),
        ("5_hours", "5 Hours"),
        ("daily", "Daily"),
        ("24_hours", "24 Hours"),
        ("weekly", "Weekly"),
    ]

    guest = models.ForeignKey(Guest, on_delete=models.PROTECT, related_name="bookings")
    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name="bookings")
    check_in_date = models.DateField()
    check_out_date = models.DateField()
    booking_duration_type = models.CharField(max_length=20, choices=DURATION_CHOICES, default="daily")
    booking_start_time = models.TimeField(null=True, blank=True)
    booking_end_time = models.TimeField(null=True, blank=True)
    num_guests = models.IntegerField(default=1)
    rate_per_night = models.DecimalField(max_digits=10, decimal_places=2)
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    deposit_required = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    deposit_paid = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    balance_due = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    booking_source = models.CharField(max_length=20, choices=BOOKING_SOURCE_CHOICES, default="Walk-in")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="Pending")
    vehicle_registration = models.CharField(max_length=20, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    booking_reference = models.CharField(max_length=20, unique=True, blank=True)
    check_in_time = models.DateTimeField(null=True, blank=True)
    check_out_time = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-check_in_date", "-created_at"]

    def __str__(self):
        return f"{self.booking_reference} — {self.guest.full_name}"

    @property
    def num_nights(self):
        if self.is_hourly:
            return 0
        return (self.check_out_date - self.check_in_date).days

    @property
    def is_hourly(self):
        return self.booking_duration_type in ["1_hour", "2_hours", "3_hours", "5_hours"]

    @property
    def duration_label(self):
        labels = {
            "1_hour": "1 Hour",
            "2_hours": "2 Hours",
            "3_hours": "3 Hours",
            "5_hours": "5 Hours",
            "daily": "Per Night",
            "24_hours": "24 Hours",
            "weekly": "Per Week",
        }
        return labels.get(self.booking_duration_type, "Daily")

    def duration_hours(self):
        return {
            "1_hour": 1,
            "2_hours": 2,
            "3_hours": 3,
            "5_hours": 5,
        }.get(self.booking_duration_type, 0)

    def compute_hourly_end_time(self):
        if not self.is_hourly or not self.booking_start_time:
            return None
        start = datetime.datetime.combine(self.check_in_date, self.booking_start_time)
        return (start + timedelta(hours=self.duration_hours())).time()

    def _booking_window(self):
        if self.is_hourly:
            if not self.check_in_date or not self.booking_start_time:
                return None, None
            start = datetime.datetime.combine(self.check_in_date, self.booking_start_time)
            end_time = self.booking_end_time or self.compute_hourly_end_time()
            if not end_time:
                return start, None
            end = datetime.datetime.combine(self.check_in_date, end_time)
            if end <= start:
                end += timedelta(days=1)
            return start, end
        if not self.check_in_date or not self.check_out_date:
            return None, None
        return (
            datetime.datetime.combine(self.check_in_date, time.min),
            datetime.datetime.combine(self.check_out_date, time.min),
        )

    def overlapping_bookings(self):
        if not self.room_id or self.status in self.INACTIVE_STATUSES:
            return Booking.objects.none()
        start, end = self._booking_window()
        if not start or not end:
            return Booking.objects.none()

        possible = Booking.objects.filter(room_id=self.room_id).exclude(status__in=self.INACTIVE_STATUSES)
        if self.pk:
            possible = possible.exclude(pk=self.pk)

        conflicts = []
        for booking in possible:
            other_start, other_end = booking._booking_window()
            if other_start and other_end and start < other_end and end > other_start:
                conflicts.append(booking.pk)
        return Booking.objects.filter(pk__in=conflicts)

    def validate_room_conflict(self):
        # Hourly bookings need time-of-day windows, not whole dates, so they
        # keep using the pre-existing datetime-based overlap check below.
        # Every other duration goes through the shared availability service
        # (core/availability.py) so this logic lives in exactly one place.
        if self.is_hourly:
            conflict = self.overlapping_bookings().select_related("guest", "room").first()
            if conflict:
                raise ValidationError(
                    f"{self.room.name} is already booked for that time "
                    f"({conflict.booking_reference} - {conflict.guest.full_name})."
                )
            return

        from .availability import check_availability

        result = check_availability(
            self.room, self.check_in_date, self.check_out_date, self.num_guests,
            exclude_booking_id=self.pk,
        )
        if not result.available:
            raise ValidationError(result.reason)

    def _generate_reference(self):
        import random
        from django.utils import timezone
        today = timezone.now().strftime("%Y%m%d")
        for _ in range(100):
            suffix = f"{random.randint(0, 9999):04d}"
            ref = f"CCG-{today}-{suffix}"
            if not Booking.objects.filter(booking_reference=ref).exists():
                return ref
        raise ValueError("Could not generate a unique booking reference.")

    def compute_totals(self):
        if self.pk and self.room_allocations.exists():
            # Multi-room booking: each RoomAllocation already carries its own
            # priced line_total (allocated_guests x nights x its own PPPN rate
            # snapshot) — the booking total is simply their sum, minus the
            # existing discount rule. No tax is applied here, matching the
            # single-room path below, which has never applied one either.
            subtotal = sum((allocation.line_total for allocation in self.room_allocations.all()), Decimal("0.00"))
            self.total_amount = max(subtotal - self.discount, Decimal("0.00"))
        else:
            guests = Decimal(self.num_guests or 1)
            is_per_person = bool(self.room_id and self.room.pricing_model == "per_person")
            guest_multiplier = guests if is_per_person else Decimal("1")
            if self.is_hourly:
                if self.check_in_date:
                    self.check_out_date = self.check_in_date
                self.booking_end_time = self.compute_hourly_end_time()
                duration_price = self.room.get_price_for_duration(self.booking_duration_type) if self.room_id else None
                if duration_price is not None:
                    self.rate_per_night = duration_price
                subtotal = (self.rate_per_night or Decimal("0.00")) * guest_multiplier
                self.total_amount = max(subtotal - self.discount, Decimal("0.00"))
            elif self.check_in_date and self.check_out_date:
                nights = max((self.check_out_date - self.check_in_date).days, 0)
                if self.booking_duration_type == "weekly":
                    weekly_rate = None
                    if self.room_id:
                        weekly_rate = self.room.price_per_week
                    if weekly_rate is None:
                        weekly_rate = self.rate_per_night * Decimal("7")
                    weeks = Decimal(nights) / Decimal("7")
                    self.rate_per_night = weekly_rate
                    subtotal = weeks * weekly_rate * guest_multiplier
                    self.total_amount = max(subtotal - self.discount, Decimal("0.00"))
                elif self.booking_duration_type == "24_hours":
                    duration_price = self.room.get_price_for_duration(self.booking_duration_type) if self.room_id else None
                    if duration_price is not None:
                        self.rate_per_night = duration_price
                    subtotal = self.rate_per_night * nights * guest_multiplier
                    self.total_amount = max(subtotal - self.discount, Decimal("0.00"))
                else:
                    subtotal = self.rate_per_night * nights * guest_multiplier
                    self.total_amount = max(subtotal - self.discount, Decimal("0.00"))
        if self.pk and self.payments.exists():
            payment_totals = self.payment_totals()
            self.deposit_paid = payment_totals["deposit_paid"]
            self.balance_due = self.total_amount - payment_totals["net_paid"]
        else:
            self.balance_due = self.total_amount - self.deposit_paid

    def payment_totals(self):
        payments = self.payments.all()
        total_paid = payments.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        deposit_paid = payments.filter(payment_type="Deposit").aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        total_refunded = self.refunds.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        net_paid = total_paid - total_refunded
        return {
            "total_paid": total_paid,
            "total_refunded": total_refunded,
            "net_paid": net_paid,
            "deposit_paid": deposit_paid,
            "balance_due": self.total_amount - net_paid,
        }

    def recalculate_payment_totals(self):
        self.recalculate_balance()

    def recalculate_balance(self):
        totals = self.payment_totals()
        self.deposit_paid = totals["deposit_paid"]
        self.balance_due = totals["balance_due"]
        self.save(update_fields=["deposit_paid", "balance_due"])

    def validate_room_available(self):
        if self.room_id and self.status not in self.INACTIVE_STATUSES:
            room = self.room
            if room.status in ("Maintenance", "Blocked", "Cleaning"):
                labels = {"Maintenance": "under maintenance", "Blocked": "blocked", "Cleaning": "currently being cleaned"}
                raise ValidationError(
                    f"{room.name} is {labels.get(room.status, room.status.lower())} and cannot be booked."
                )

    def save(self, *args, **kwargs):
        from django.db import transaction
        if self.vehicle_registration:
            self.vehicle_registration = " ".join(self.vehicle_registration.strip().upper().split())
        if not self.booking_reference:
            self.booking_reference = self._generate_reference()
        self.compute_totals()
        self.validate_room_available()
        if self.room_id and self.status not in self.INACTIVE_STATUSES:
            with transaction.atomic():
                # Row-level lock on all active bookings for this room prevents
                # concurrent double-booking that would slip past Python-level checks.
                list(
                    Booking.objects.filter(room_id=self.room_id)
                    .exclude(status__in=self.INACTIVE_STATUSES)
                    .select_for_update()
                )
                self.validate_room_conflict()
                super().save(*args, **kwargs)
        else:
            self.validate_room_conflict()
            super().save(*args, **kwargs)


class RoomAllocation(models.Model):
    """
    One room's share of a booking. Existing single-room bookings are not
    required to have any RoomAllocation rows — Booking.room/num_guests/
    rate_per_night/total_amount remain the source of truth for that legacy
    case. RoomAllocation matters once a booking spans more than one room.

    No explicit "tenant" field is stored here: this app uses schema-per-tenant
    isolation (see tenants/models.py, config/settings.py TENANT_APPS) — the
    Postgres schema itself is the tenant boundary, and no model in this app
    carries a tenant foreign key. What's enforced instead is that an
    allocation's room belongs to the same Property as the booking's own room,
    the closest tenant-scoped consistency check reachable within one schema.
    """

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="room_allocations")
    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name="allocations")
    allocated_guests = models.PositiveIntegerField()
    rate_plan = models.ForeignKey(
        RatePlan, null=True, blank=True, on_delete=models.SET_NULL, related_name="room_allocations"
    )
    rate_per_night = models.DecimalField(max_digits=10, decimal_places=2)
    line_total = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("booking", "room")]
        ordering = ["booking_id", "room__name"]

    def __str__(self):
        return f"{self.booking.booking_reference} — {self.room.name} ({self.allocated_guests} guests)"

    def clean(self):
        if self.allocated_guests is not None and self.allocated_guests < 1:
            raise ValidationError("Allocated guests must be at least 1.")
        if self.room_id and self.booking_id and self.room.prop_id != self.booking.room.prop_id:
            raise ValidationError(
                "This room does not belong to the same property as the booking — "
                "an allocation cannot cross properties/tenants."
            )

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)


class Payment(models.Model):
    PAYMENT_METHOD_CHOICES = [
        ("Cash", "Cash"),
        ("EFT", "EFT"),
        ("Card", "Card"),
        ("SnapScan", "SnapScan"),
        ("Zapper", "Zapper"),
        ("Bank Deposit", "Bank Deposit"),
        ("Other", "Other"),
    ]
    PAYMENT_TYPE_CHOICES = [
        ("Payment", "Payment"),
        ("Deposit", "Deposit"),
    ]

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="payments")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_date = models.DateField(default=timezone.localdate)
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES)
    payment_type = models.CharField(max_length=20, choices=PAYMENT_TYPE_CHOICES)
    reference = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-payment_date", "-recorded_at"]

    def __str__(self):
        return f"{self.booking.booking_reference} - {self.payment_type} - {self.amount}"

    def _generate_reference(self):
        today = timezone.now().strftime("%Y%m%d")
        for index in range(1, 10000):
            ref = f"PAY-{today}-{index:04d}"
            if not Payment.objects.filter(reference=ref).exists():
                return ref
        return f"PAY-{today}-{uuid.uuid4().hex[:8].upper()}"

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = self._generate_reference()
        super().save(*args, **kwargs)
        self.booking.recalculate_payment_totals()

    def delete(self, *args, **kwargs):
        booking = self.booking
        result = super().delete(*args, **kwargs)
        booking.recalculate_payment_totals()
        return result


class BookingRefund(models.Model):
    REFUND_METHOD_CHOICES = [
        ("Cash", "Cash"),
        ("EFT", "EFT"),
        ("Card", "Card"),
        ("Other", "Other"),
    ]

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="refunds")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    refund_date = models.DateField(default=timezone.localdate)
    refund_method = models.CharField(max_length=20, choices=REFUND_METHOD_CHOICES)
    reference = models.CharField(max_length=100, blank=True)
    reason = models.TextField()
    approved_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="approved_booking_refunds")
    recorded_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="recorded_booking_refunds")
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-refund_date", "-recorded_at"]

    def __str__(self):
        return f"{self.booking.booking_reference} refund - {self.amount}"

    def _generate_reference(self):
        today = timezone.now().strftime("%Y%m%d")
        for index in range(1, 10000):
            ref = f"REF-{today}-{index:04d}"
            if not BookingRefund.objects.filter(reference=ref).exists():
                return ref
        return f"REF-{today}-{uuid.uuid4().hex[:8].upper()}"

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = self._generate_reference()
        super().save(*args, **kwargs)
        self.booking.recalculate_payment_totals()


class AuditLog(models.Model):
    ACTION_CHOICES = [
        ("create", "Create"),
        ("update", "Update"),
        ("delete", "Delete"),
        ("approve", "Approve"),
        ("refund", "Refund"),
        ("void", "Void"),
        ("login_override", "Manager Approval"),
        ("exception", "Exception"),
    ]

    actor = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_logs")
    approved_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_audit_logs",
    )
    action = models.CharField(max_length=30, choices=ACTION_CHOICES)
    object_type = models.CharField(max_length=80)
    object_id = models.CharField(max_length=80, blank=True)
    object_repr = models.CharField(max_length=255, blank=True)
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    reason = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.actor} {self.action} {self.object_type}"


class CleaningProof(models.Model):
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name="cleaning_proofs")
    uploaded_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True)
    photo = models.ImageField(upload_to="cleaning_proofs/%Y/%m/")
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.room.name} proof {self.created_at:%Y-%m-%d %H:%M}"


class DailyCloseLock(models.Model):
    close_date = models.DateField(unique=True)
    locked_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True)
    locked_at = models.DateTimeField(auto_now_add=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-close_date"]

    def __str__(self):
        return f"Daily close locked for {self.close_date:%Y-%m-%d}"


class CommunicationLog(models.Model):
    CHANNEL_CHOICES = [
        ("WhatsApp", "WhatsApp"),
        ("Email", "Email"),
        ("SMS", "SMS"),
        ("Phone", "Phone"),
    ]
    STATUS_CHOICES = [
        ("drafted", "Drafted"),
        ("opened", "Opened"),
        ("sent", "Sent"),
        ("failed", "Failed"),
    ]

    booking = models.ForeignKey(Booking, on_delete=models.SET_NULL, null=True, blank=True, related_name="communication_logs")
    guest = models.ForeignKey(Guest, on_delete=models.SET_NULL, null=True, blank=True, related_name="communication_logs")
    channel = models.CharField(max_length=20, choices=CHANNEL_CHOICES)
    template_name = models.CharField(max_length=80, blank=True)
    recipient = models.CharField(max_length=150)
    message = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="drafted")
    created_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.channel} to {self.recipient} - {self.status}"


class Expense(models.Model):
    CATEGORY_CHOICES = [
        ("Cleaning", "Cleaning"),
        ("Maintenance", "Maintenance"),
        ("Utilities", "Utilities"),
        ("Supplies", "Supplies"),
        ("Staff", "Staff"),
        ("Marketing", "Marketing"),
        ("Other", "Other"),
    ]
    PAYMENT_METHOD_CHOICES = [
        ("Cash", "Cash"),
        ("EFT", "EFT"),
        ("Card", "Card"),
        ("Other", "Other"),
    ]

    prop = models.ForeignKey(
        "Property", on_delete=models.CASCADE, null=True, blank=True, related_name="expenses"
    )
    date = models.DateField()
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    description = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    paid_to = models.CharField(max_length=150, blank=True)
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES)
    reference = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]

    def __str__(self):
        return f"{self.date} - {self.category} - {self.amount}"


class GuestHouseSettings(models.Model):
    guest_house_name = models.CharField(max_length=150, default="My Guest House")
    logo = models.ImageField(upload_to="settings/", blank=True, null=True)
    phone = models.CharField(max_length=50, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    banking_details = models.TextField(blank=True)
    vat_registered = models.BooleanField(default=False)
    vat_number = models.CharField(max_length=50, blank=True)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("15.00"))
    check_in_time = models.TimeField(default=time(14, 0))
    check_out_time = models.TimeField(default=time(10, 0))
    currency = models.CharField(max_length=10, default="R")
    cancellation_note = models.TextField(blank=True)
    invoice_notes = models.TextField(blank=True)
    receipt_notes = models.TextField(blank=True)
    enable_hourly_bookings = models.BooleanField(default=True)
    enable_weekly_bookings = models.BooleanField(default=True)
    hourly_booking_label = models.CharField(max_length=100, default="Short Stay")
    minimum_hourly_rate = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    default_price_2_hours = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    default_price_3_hours = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    default_price_per_night = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    default_price_24_hours = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    late_checkout_fee = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="Extra charge for late checkout",
    )
    early_checkin_fee = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    weekend_surcharge_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="% extra on Fri/Sat nights",
    )
    seasonal_note = models.TextField(blank=True, help_text="Note shown on availability page")
    pdf_primary_color = models.CharField(max_length=7, default="#c9a84c", help_text="Brand colour used across all PDF exports (hex, e.g. #c9a84c)")
    onboarding_complete = models.BooleanField(default=False)
    shared_capacity_booking_enabled = models.BooleanField(
        default=False,
        help_text="Allow rooms configured as Shared Capacity to take multiple overlapping bookings up to room capacity.",
    )

    class Meta:
        verbose_name = "Guest House Settings"
        verbose_name_plural = "Guest House Settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def __str__(self):
        return self.guest_house_name


class TrialLicense(models.Model):
    activated_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    guest_house_name = models.CharField(max_length=200)
    owner_email = models.EmailField()
    license_key = models.CharField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)
    trial_extended = models.BooleanField(default=False)
    converted_to_paid = models.BooleanField(default=False)
    converted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-activated_at"]

    def __str__(self):
        return self.license_key

    @property
    def days_remaining(self):
        delta = self.expires_at - timezone.now()
        if delta.total_seconds() <= 0:
            return 0
        return int((delta.total_seconds() + 86399) // 86400)

    @property
    def is_expired(self):
        return timezone.now() > self.expires_at

    @property
    def is_in_warning_period(self):
        return self.days_remaining <= 7


class SubscriptionPlan(models.Model):
    PLAN_CHOICES = [
        ("starter", "Starter"),
        ("professional", "Professional"),
        ("enterprise", "Enterprise"),
    ]
    BILLING_CHOICES = [
        ("monthly", "Monthly"),
        ("annual", "Annual"),
    ]

    name = models.CharField(max_length=50, choices=PLAN_CHOICES, unique=True)
    display_name = models.CharField(max_length=100)
    monthly_price = models.DecimalField(max_digits=10, decimal_places=2)
    annual_price = models.DecimalField(max_digits=10, decimal_places=2)
    max_rooms = models.IntegerField(default=8)
    max_users = models.IntegerField(default=1)
    feature_expenses = models.BooleanField(default=False)
    feature_full_reports = models.BooleanField(default=False)
    feature_export = models.BooleanField(default=False)
    feature_hourly_bookings = models.BooleanField(default=False)
    feature_weekly_bookings = models.BooleanField(default=False)
    feature_inventory = models.BooleanField(default=False)
    feature_staff_roles = models.BooleanField(default=False)
    feature_multi_property = models.BooleanField(default=False)
    feature_api_access = models.BooleanField(default=False)
    feature_custom_pdf_branding = models.BooleanField(default=False)
    feature_maintenance_requests = models.BooleanField(default=False)
    feature_priority_support = models.BooleanField(default=False)
    feature_spa = models.BooleanField(default=False)

    class Meta:
        ordering = ["monthly_price"]

    def __str__(self):
        return self.display_name


class Subscription(models.Model):
    STATUS_CHOICES = [
        ("trial", "Trial"),
        ("active", "Active"),
        ("expired", "Expired"),
        ("cancelled", "Cancelled"),
        ("suspended", "Suspended"),
    ]
    BILLING_CHOICES = [
        ("monthly", "Monthly"),
        ("annual", "Annual"),
    ]

    plan = models.ForeignKey(SubscriptionPlan, on_delete=models.PROTECT)
    billing_cycle = models.CharField(max_length=10, choices=BILLING_CHOICES, default="monthly")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="trial")
    started_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    auto_renew = models.BooleanField(default=True)
    owner_name = models.CharField(max_length=200)
    owner_email = models.EmailField()
    owner_phone = models.CharField(max_length=20, blank=True)
    last_payment_date = models.DateField(null=True, blank=True)
    last_payment_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    last_payment_reference = models.CharField(max_length=100, blank=True)
    next_billing_date = models.DateField(null=True, blank=True)
    payfast_token = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True)
    control_grace_ends_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    @property
    def is_active(self):
        return self.status in ["active", "trial"] and timezone.now() < self.expires_at

    @property
    def days_remaining(self):
        delta = self.expires_at - timezone.now()
        return max(0, delta.days)

    @property
    def is_trial(self):
        return self.status == "trial"

    @property
    def is_in_warning_period(self):
        return self.days_remaining <= 7

    def has_feature(self, feature_name):
        return getattr(self.plan, f"feature_{feature_name}", False)

    def __str__(self):
        return f"{self.plan.display_name} - {self.status} - {self.days_remaining} days left"


class ControlManualPayment(models.Model):
    STATUSES = [
        ('recorded', 'Recorded'), ('pending_verification', 'Pending verification'),
        ('verified', 'Verified'), ('rejected', 'Rejected'), ('reversed', 'Reversed'),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscription = models.ForeignKey(Subscription, on_delete=models.PROTECT, related_name='control_manual_payments')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3)
    payment_date = models.DateField()
    payment_method_category = models.CharField(max_length=40)
    internal_reference = models.CharField(max_length=100, unique=True)
    invoice_reference = models.CharField(max_length=100, blank=True)
    coverage_start = models.DateField(null=True, blank=True)
    coverage_end = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    evidence_metadata = models.JSONField(default=dict, blank=True)
    activate_after_payment = models.BooleanField(default=False)
    next_billing_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=30, choices=STATUSES, default='pending_verification')
    recorded_by = models.CharField(max_length=200)
    operation_id = models.UUIDField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class ControlUserSecurity(models.Model):
    user = models.OneToOneField('auth.User', on_delete=models.CASCADE, related_name='control_security')
    force_password_reset = models.BooleanField(default=False)
    password_hash_at_force = models.CharField(max_length=128, blank=True)
    forced_at = models.DateTimeField(null=True, blank=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    lock_reason = models.CharField(max_length=200, blank=True)
    unlocked_at = models.DateTimeField(null=True, blank=True)
    last_successful_login = models.DateTimeField(null=True, blank=True)
    last_failed_login = models.DateTimeField(null=True, blank=True)
    failed_login_count = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)


class MaintenanceRequest(models.Model):
    PRIORITY_CHOICES = [
        ("low", "Low"),
        ("medium", "Medium"),
        ("high", "High"),
        ("urgent", "Urgent"),
    ]
    STATUS_CHOICES = [
        ("open", "Open"),
        ("in_progress", "In Progress"),
        ("resolved", "Resolved"),
        ("closed", "Closed"),
    ]
    CATEGORY_CHOICES = [
        ("plumbing", "Plumbing"),
        ("electrical", "Electrical"),
        ("hvac", "HVAC / Air Con"),
        ("furniture", "Furniture"),
        ("appliance", "Appliance"),
        ("structural", "Structural"),
        ("cleaning", "Deep Cleaning"),
        ("pest", "Pest Control"),
        ("other", "Other"),
    ]

    room = models.ForeignKey(
        "Room",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_requests",
    )
    category = models.CharField(max_length=30, choices=CATEGORY_CHOICES)
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default="medium")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="open")
    title = models.CharField(max_length=200)
    description = models.TextField()
    reported_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="reported_maintenance",
    )
    assigned_to = models.CharField(max_length=200, blank=True, help_text="Name of contractor or staff member")
    assigned_phone = models.CharField(max_length=20, blank=True)
    estimated_cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    actual_cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    reported_at = models.DateTimeField(auto_now_add=True)
    scheduled_for = models.DateField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_notes = models.TextField(blank=True)
    block_room_until_resolved = models.BooleanField(default=False)

    class Meta:
        ordering = ["-reported_at"]

    @property
    def is_overdue(self):
        if self.scheduled_for and self.status not in ["resolved", "closed"]:
            return self.scheduled_for < timezone.now().date()
        return False

    def save(self, *args, **kwargs):
        if self.block_room_until_resolved and self.room:
            if self.status in ["open", "in_progress"]:
                self.room.status = "Maintenance"
                self.room.save(update_fields=["status"])
            elif self.status == "resolved":
                self.room.status = "Available"
                self.room.save(update_fields=["status"])
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.title} - {self.room} - {self.priority}"


class InventoryCategory(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    color = models.CharField(max_length=7, default="#c9a84c")

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "Inventory categories"

    def __str__(self):
        return self.name


class InventoryItem(models.Model):
    prop = models.ForeignKey(
        "Property", on_delete=models.CASCADE, null=True, blank=True, related_name="inventory_items"
    )
    name = models.CharField(max_length=200)
    category = models.ForeignKey(InventoryCategory, on_delete=models.SET_NULL, null=True, blank=True)
    sku = models.CharField(max_length=50, blank=True)
    unit = models.CharField(
        max_length=30,
        default="units",
        help_text="e.g. units, rolls, bottles, sets, kg",
    )
    current_stock = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    minimum_stock = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=5,
        help_text="Alert when stock falls below this level",
    )
    reorder_quantity = models.DecimalField(max_digits=10, decimal_places=2, default=10)
    cost_per_unit = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    supplier_name = models.CharField(max_length=200, blank=True)
    supplier_phone = models.CharField(max_length=20, blank=True)
    location = models.CharField(
        max_length=100,
        blank=True,
        help_text="e.g. Storage Room, Linen Cupboard, Kitchen",
    )
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    @property
    def is_low_stock(self):
        return self.current_stock <= self.minimum_stock

    @property
    def stock_value(self):
        return (self.current_stock * self.cost_per_unit).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def __str__(self):
        return self.name


class InventoryTransaction(models.Model):
    TRANSACTION_TYPES = [
        ("in", "Stock In"),
        ("out", "Stock Out"),
        ("adjust", "Adjustment"),
        ("loss", "Loss/Damage"),
    ]

    item = models.ForeignKey(InventoryItem, on_delete=models.CASCADE, related_name="transactions")
    transaction_type = models.CharField(max_length=10, choices=TRANSACTION_TYPES)
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    stock_before = models.DecimalField(max_digits=10, decimal_places=2)
    stock_after = models.DecimalField(max_digits=10, decimal_places=2)
    reference = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    room = models.ForeignKey("Room", on_delete=models.SET_NULL, null=True, blank=True, help_text="Link to room if stock was used for a specific room")
    booking = models.ForeignKey("Booking", on_delete=models.SET_NULL, null=True, blank=True)
    recorded_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-recorded_at"]

    def __str__(self):
        return f"{self.item.name} - {self.transaction_type} - {self.quantity}"


class RoomInventoryAssignment(models.Model):
    room = models.ForeignKey("Room", on_delete=models.CASCADE, related_name="inventory_assignments")
    item = models.ForeignKey(InventoryItem, on_delete=models.CASCADE)
    expected_quantity = models.DecimalField(max_digits=10, decimal_places=2, default=1)
    notes = models.TextField(blank=True)

    class Meta:
        unique_together = ["room", "item"]
        ordering = ["room__name", "item__name"]

    def __str__(self):
        return f"{self.room.name} - {self.item.name}"


class TrialEngagement(models.Model):
    """
    Singleton per tenant — tracks engagement activity during (and after) trial.
    Used by Command Center to compute health scores and prioritise follow-up.
    """
    login_count = models.PositiveIntegerField(default=0)
    rooms_added = models.PositiveIntegerField(default=0)
    guests_added = models.PositiveIntegerField(default=0)
    bookings_added = models.PositiveIntegerField(default=0)
    reports_viewed = models.PositiveIntegerField(default=0)
    last_login_at = models.DateTimeField(null=True, blank=True)
    last_activity_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Trial Engagement"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def health_score(self):
        score = 0
        if self.login_count >= 10:
            score += 3
        elif self.login_count >= 5:
            score += 2
        elif self.login_count >= 2:
            score += 1
        if self.bookings_added >= 3:
            score += 3
        elif self.bookings_added >= 1:
            score += 2
        if self.rooms_added >= 1:
            score += 1
        if self.guests_added >= 1:
            score += 1
        if self.reports_viewed >= 2:
            score += 2
        elif self.reports_viewed >= 1:
            score += 1
        if score >= 8:
            return "High"
        if score >= 4:
            return "Medium"
        return "Low"

    @property
    def health_color(self):
        return {"High": "#22c55e", "Medium": "#f59e0b", "Low": "#ef4444"}[self.health_score]

    def __str__(self):
        return f"Engagement — {self.health_score} (logins:{self.login_count} bookings:{self.bookings_added})"


class SpaService(models.Model):
    CATEGORY_CHOICES = [
        ("massage", "Massage"),
        ("facial", "Facial"),
        ("body", "Body Treatment"),
        ("nail", "Nail Care"),
        ("hair", "Hair"),
        ("hydrotherapy", "Hydrotherapy"),
        ("wellness", "Wellness & Meditation"),
        ("other", "Other"),
    ]

    prop = models.ForeignKey(
        "Property",
        on_delete=models.CASCADE,
        related_name="spa_services",
    )
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=30, choices=CATEGORY_CHOICES, default="massage")
    description = models.TextField(blank=True)
    duration_minutes = models.PositiveIntegerField(default=60, help_text="Duration in minutes")
    price = models.DecimalField(max_digits=10, decimal_places=2)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["category", "name"]

    def __str__(self):
        return f"{self.name} ({self.duration_minutes} min)"


class SpaAppointment(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("confirmed", "Confirmed"),
        ("in_progress", "In Progress"),
        ("completed", "Completed"),
        ("cancelled", "Cancelled"),
        ("no_show", "No Show"),
    ]
    PAYMENT_STATUS_CHOICES = [
        ("unpaid", "Unpaid"),
        ("deposit_paid", "Deposit Paid"),
        ("paid", "Paid"),
        ("refunded", "Refunded"),
    ]

    prop = models.ForeignKey(
        "Property",
        on_delete=models.CASCADE,
        related_name="spa_appointments",
    )
    service = models.ForeignKey(
        SpaService,
        on_delete=models.PROTECT,
        related_name="appointments",
    )
    guest = models.ForeignKey(
        "Guest",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="spa_appointments",
    )
    booking = models.ForeignKey(
        "Booking",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="spa_appointments",
        help_text="Link to room booking (optional)",
    )
    guest_name = models.CharField(max_length=200, blank=True, help_text="Walk-in guest name (if no guest profile)")
    guest_phone = models.CharField(max_length=30, blank=True)
    therapist = models.CharField(max_length=200, blank=True, help_text="Name of therapist")
    scheduled_date = models.DateField()
    scheduled_time = models.TimeField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    price_charged = models.DecimalField(max_digits=10, decimal_places=2, help_text="Actual price charged")
    payment_status = models.CharField(max_length=20, choices=PAYMENT_STATUS_CHOICES, default="unpaid")
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="spa_appointments_created",
    )
    assigned_therapist = models.ForeignKey(
        "SpaTherapist",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="appointments",
    )
    treatment_room = models.ForeignKey(
        "SpaTreatmentRoom",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="appointments",
    )
    package = models.ForeignKey(
        "SpaPackage",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="appointments",
    )
    voucher = models.ForeignKey(
        "SpaVoucher",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="appointments",
    )
    tip_amount = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0.00"))
    commission_amount = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    consultation_notes = models.TextField(blank=True)
    reminder_sent = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["scheduled_date", "scheduled_time"]

    @property
    def display_guest_name(self):
        if self.guest:
            return self.guest.full_name
        return self.guest_name or "Walk-in"

    @property
    def scheduled_datetime(self):
        import datetime
        return datetime.datetime.combine(self.scheduled_date, self.scheduled_time)

    @property
    def display_therapist(self):
        if self.assigned_therapist_id:
            return self.assigned_therapist.name
        return self.therapist or "Unassigned"

    @property
    def total_charged(self):
        total = (self.price_charged or Decimal("0")) + (self.tip_amount or Decimal("0"))
        voucher_val = self.voucher.value if self.voucher and self.voucher.value else Decimal("0")
        return max(total - voucher_val, Decimal("0"))

    def payment_totals(self):
        payments = self.spa_payments.all()
        total_paid = payments.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        deposit_paid = payments.filter(payment_type="Deposit").aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        balance_due = max((self.price_charged or Decimal("0")) - total_paid, Decimal("0.00"))
        return {
            "total_paid": total_paid,
            "deposit_paid": deposit_paid,
            "balance_due": balance_due,
        }

    def recalculate_payment_status(self):
        totals = self.payment_totals()
        if totals["balance_due"] <= 0:
            self.payment_status = "paid"
        elif totals["deposit_paid"] > 0:
            self.payment_status = "deposit_paid"
        else:
            self.payment_status = "unpaid"
        self.save(update_fields=["payment_status"])

    def __str__(self):
        return f"{self.service.name} — {self.display_guest_name} on {self.scheduled_date}"


class SpaPayment(models.Model):
    PAYMENT_METHOD_CHOICES = [
        ("Cash", "Cash"),
        ("EFT", "EFT"),
        ("Card", "Card"),
        ("SnapScan", "SnapScan"),
        ("Zapper", "Zapper"),
        ("Bank Deposit", "Bank Deposit"),
        ("Other", "Other"),
    ]
    PAYMENT_TYPE_CHOICES = [
        ("Payment", "Payment"),
        ("Deposit", "Deposit"),
    ]

    appointment = models.ForeignKey(
        "SpaAppointment",
        on_delete=models.CASCADE,
        related_name="spa_payments",
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_date = models.DateField(default=timezone.localdate)
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES)
    payment_type = models.CharField(max_length=20, choices=PAYMENT_TYPE_CHOICES, default="Payment")
    reference = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-payment_date", "-recorded_at"]

    def __str__(self):
        return f"SPA-{self.appointment_id} — {self.payment_type} R{self.amount}"

    def _generate_reference(self):
        today = timezone.now().strftime("%Y%m%d")
        for index in range(1, 10000):
            ref = f"SPA-PAY-{today}-{index:04d}"
            if not SpaPayment.objects.filter(reference=ref).exists():
                return ref
        return f"SPA-PAY-{today}-{uuid.uuid4().hex[:8].upper()}"

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = self._generate_reference()
        super().save(*args, **kwargs)
        self.appointment.recalculate_payment_status()

    def delete(self, *args, **kwargs):
        appt = self.appointment
        result = super().delete(*args, **kwargs)
        appt.recalculate_payment_status()
        return result


class SpaTherapist(models.Model):
    WORKING_DAY_CHOICES = [
        ("Mon", "Monday"),
        ("Tue", "Tuesday"),
        ("Wed", "Wednesday"),
        ("Thu", "Thursday"),
        ("Fri", "Friday"),
        ("Sat", "Saturday"),
        ("Sun", "Sunday"),
    ]

    prop = models.ForeignKey(
        "Property",
        on_delete=models.CASCADE,
        related_name="spa_therapists",
    )
    name = models.CharField(max_length=200)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    specialties = models.TextField(blank=True, help_text="Services this therapist specialises in")
    commission_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("0.00"),
        help_text="Commission percentage (e.g. 15 = 15%)",
    )
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class SpaTreatmentRoom(models.Model):
    prop = models.ForeignKey(
        "Property",
        on_delete=models.CASCADE,
        related_name="spa_treatment_rooms",
    )
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class SpaPackage(models.Model):
    prop = models.ForeignKey(
        "Property",
        on_delete=models.CASCADE,
        related_name="spa_packages",
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    package_price = models.DecimalField(max_digits=10, decimal_places=2)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    @property
    def service_count(self):
        return self.items.count()

    @property
    def total_individual_price(self):
        return sum(item.service.price for item in self.items.select_related("service").all())

    def __str__(self):
        return self.name


class SpaPackageItem(models.Model):
    package = models.ForeignKey(SpaPackage, on_delete=models.CASCADE, related_name="items")
    service = models.ForeignKey(SpaService, on_delete=models.CASCADE, related_name="package_items")
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "pk"]

    def __str__(self):
        return f"{self.package.name} — {self.service.name}"


class SpaVoucher(models.Model):
    prop = models.ForeignKey(
        "Property",
        on_delete=models.CASCADE,
        related_name="spa_vouchers",
    )
    code = models.CharField(max_length=20, unique=True, editable=False)
    value = models.DecimalField(max_digits=10, decimal_places=2)
    issued_to_name = models.CharField(max_length=200)
    issued_to_email = models.EmailField(blank=True)
    issued_to_phone = models.CharField(max_length=30, blank=True)
    issued_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="spa_vouchers_issued",
    )
    valid_from = models.DateField()
    valid_until = models.DateField(null=True, blank=True)
    redeemed_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_redeemed(self):
        return self.redeemed_at is not None

    @property
    def is_expired(self):
        if self.valid_until and timezone.localdate() > self.valid_until:
            return True
        return False

    @property
    def is_usable(self):
        return self.is_active and not self.is_redeemed and not self.is_expired

    def save(self, *args, **kwargs):
        if not self.code:
            import secrets
            import string
            alphabet = string.ascii_uppercase + string.digits
            self.code = "SPA-" + "".join(secrets.choice(alphabet) for _ in range(8))
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.code} — R{self.value} for {self.issued_to_name}"


class SpaWaitlist(models.Model):
    STATUS_CHOICES = [
        ("waiting", "Waiting"),
        ("notified", "Notified"),
        ("booked", "Booked"),
        ("expired", "Expired"),
    ]

    prop = models.ForeignKey(
        "Property",
        on_delete=models.CASCADE,
        related_name="spa_waitlist",
    )
    service = models.ForeignKey(
        SpaService,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="waitlist_entries",
    )
    preferred_therapist = models.ForeignKey(
        SpaTherapist,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="waitlist_entries",
    )
    preferred_date = models.DateField(null=True, blank=True)
    guest = models.ForeignKey(
        "Guest",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="spa_waitlist",
    )
    guest_name = models.CharField(max_length=200, blank=True)
    guest_phone = models.CharField(max_length=30, blank=True)
    guest_email = models.EmailField(blank=True)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="waiting")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def display_guest_name(self):
        if self.guest:
            return self.guest.full_name
        return self.guest_name or "Unknown"

    def __str__(self):
        return f"Waitlist — {self.display_guest_name} for {self.service}"


class SpaClientProfile(models.Model):
    SKIN_TYPE_CHOICES = [
        ("normal", "Normal"),
        ("dry", "Dry"),
        ("oily", "Oily"),
        ("combination", "Combination"),
        ("sensitive", "Sensitive"),
    ]
    PRESSURE_CHOICES = [
        ("light", "Light"),
        ("medium", "Medium"),
        ("firm", "Firm"),
        ("deep", "Deep Tissue"),
    ]

    guest = models.OneToOneField(
        "Guest",
        on_delete=models.CASCADE,
        related_name="spa_profile",
    )
    allergies = models.TextField(blank=True)
    contraindications = models.TextField(blank=True, help_text="Medical conditions or medications to be aware of")
    skin_type = models.CharField(max_length=20, choices=SKIN_TYPE_CHOICES, blank=True)
    pressure_preference = models.CharField(max_length=20, choices=PRESSURE_CHOICES, blank=True)
    preferred_therapist = models.ForeignKey(
        SpaTherapist,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="preferred_by_clients",
    )
    general_notes = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Spa Profile — {self.guest}"


class SpaServiceProduct(models.Model):
    service = models.ForeignKey(
        SpaService,
        on_delete=models.CASCADE,
        related_name="product_usage",
    )
    inventory_item = models.ForeignKey(
        "InventoryItem",
        on_delete=models.CASCADE,
        related_name="spa_service_usage",
    )
    quantity_used = models.DecimalField(
        max_digits=8, decimal_places=3, default=Decimal("1.000"),
        help_text="Quantity of this product consumed per treatment",
    )

    class Meta:
        unique_together = [("service", "inventory_item")]

    def __str__(self):
        return f"{self.service.name} uses {self.quantity_used} × {self.inventory_item.name}"
