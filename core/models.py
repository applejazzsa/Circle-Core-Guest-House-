import datetime
import uuid
from datetime import timedelta, time
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
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


class Room(models.Model):
    ROOM_TYPE_CHOICES = [
        ("Single", "Single"),
        ("Double", "Double"),
        ("Twin", "Twin"),
        ("Family", "Family"),
        ("Suite", "Suite"),
        ("Self-Catering", "Self-Catering"),
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

    @property
    def base_price(self):
        return self.price_per_night

    @base_price.setter
    def base_price(self, value):
        self.price_per_night = value

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
        default_price = default_prices.get(duration_type)
        if default_price is not None and default_price > 0:
            return default_price

        prices = {
            "1_hour": self.price_1_hour,
            "2_hours": self.price_2_hours,
            "3_hours": self.price_3_hours,
            "5_hours": self.price_5_hours,
            "daily": self.price_per_night,
            "24_hours": getattr(settings_obj, "default_price_24_hours", None) if settings_obj else None,
            "weekly": self.price_per_week,
        }
        price = prices.get(duration_type)
        if price is not None:
            return price
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

    @property
    def full_name(self):
        if self.is_generic:
            return "Walk-in Guest"
        return f"{self.first_name} {self.last_name}"

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
        conflict = self.overlapping_bookings().select_related("guest", "room").first()
        if conflict:
            raise ValidationError(
                f"{self.room.name} is already booked for that time "
                f"({conflict.booking_reference} - {conflict.guest.full_name})."
            )

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
        if self.is_hourly:
            if self.check_in_date:
                self.check_out_date = self.check_in_date
            self.booking_end_time = self.compute_hourly_end_time()
            duration_price = self.room.get_price_for_duration(self.booking_duration_type) if self.room_id else None
            if duration_price is not None:
                self.rate_per_night = duration_price
            self.total_amount = max((self.rate_per_night or Decimal("0.00")) - self.discount, Decimal("0.00"))
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
                self.total_amount = max((weeks * weekly_rate) - self.discount, Decimal("0.00"))
            elif self.booking_duration_type == "24_hours":
                duration_price = self.room.get_price_for_duration(self.booking_duration_type) if self.room_id else None
                if duration_price is not None:
                    self.rate_per_night = duration_price
                self.total_amount = max((self.rate_per_night * nights) - self.discount, Decimal("0.00"))
            else:
                self.total_amount = max(
                    (self.rate_per_night * nights) - self.discount,
                    Decimal("0.00"),
                )
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
    pos_api_key = models.CharField(max_length=100, blank=True, help_text="Secret key for POS integration")
    onboarding_complete = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Guest House Settings"
        verbose_name_plural = "Guest House Settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    def __str__(self):
        return self.guest_house_name

    def regenerate_pos_api_key(self):
        self.pos_api_key = f"sk-{uuid.uuid4().hex}"
        self.save(update_fields=["pos_api_key"])
        return self.pos_api_key

    @property
    def masked_pos_api_key(self):
        if not self.pos_api_key:
            return ""
        return f"{self.pos_api_key[:4]}{'•' * 7}{self.pos_api_key[-4:]}"


class POSTransaction(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("completed", "Completed"),
        ("failed", "Failed"),
        ("refunded", "Refunded"),
    ]

    booking = models.ForeignKey(
        Booking,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pos_transactions",
    )
    pos_reference = models.CharField(max_length=100, unique=True)
    pos_system = models.CharField(max_length=100, default="Unknown POS")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=10, default="R")
    payment_method = models.CharField(max_length=50)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    raw_payload = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    processed = models.BooleanField(default=False)
    processing_notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        return f"POS {self.pos_reference} - R {self.amount}"


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
    feature_pos_integration = models.BooleanField(default=False)
    feature_multi_property = models.BooleanField(default=False)
    feature_api_access = models.BooleanField(default=False)
    feature_custom_pdf_branding = models.BooleanField(default=False)
    feature_maintenance_requests = models.BooleanField(default=False)
    feature_priority_support = models.BooleanField(default=False)

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


class POSCategory(models.Model):
    prop = models.ForeignKey(
        "Property", on_delete=models.CASCADE, null=True, blank=True, related_name="pos_categories"
    )
    name = models.CharField(max_length=100)
    color = models.CharField(max_length=7, default='#22c55e')
    icon = models.CharField(max_length=50, blank=True,
        help_text="Emoji or icon name e.g. 🍺 or coffee")
    sort_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['sort_order', 'name']

    def __str__(self):
        return self.name


class POSItem(models.Model):
    prop = models.ForeignKey(
        "Property", on_delete=models.CASCADE, null=True, blank=True, related_name="pos_items"
    )
    name = models.CharField(max_length=200)
    category = models.ForeignKey(POSCategory, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='items')
    price = models.DecimalField(max_digits=10, decimal_places=2)
    barcode = models.CharField(max_length=100, blank=True,
        help_text="EAN-13, QR, or custom barcode")
    sku = models.CharField(max_length=50, blank=True)
    is_quick_item = models.BooleanField(default=False,
        help_text="Show on quick items grid for fast access")
    is_active = models.BooleanField(default=True)
    track_inventory = models.BooleanField(default=False)
    stock_quantity = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    low_stock_alert = models.DecimalField(max_digits=10, decimal_places=2, default=5)
    color = models.CharField(max_length=7, default='#1a1e26',
        help_text="Card background color on POS grid")
    emoji = models.CharField(max_length=10, blank=True,
        help_text="Emoji displayed on POS item card")
    sort_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['sort_order', 'name']

    def __str__(self):
        return f"{self.name} — R{self.price}"

    @property
    def is_low_stock(self):
        return self.track_inventory and self.stock_quantity <= self.low_stock_alert

    @property
    def is_out_of_stock(self):
        return self.track_inventory and self.stock_quantity <= 0


class POSSale(models.Model):
    PAYMENT_METHOD_CHOICES = [
        ('cash', 'Cash'),
        ('card', 'Card'),
        ('eft', 'EFT'),
        ('snapscan', 'SnapScan'),
        ('zapper', 'Zapper'),
        ('room_charge', 'Charge to Room'),
        ('split', 'Split Payment'),
        ('complimentary', 'Complimentary'),
    ]
    STATUS_CHOICES = [
        ('completed', 'Completed'),
        ('refunded', 'Refunded'),
        ('partial_refund', 'Partial Refund'),
        ('voided', 'Voided'),
    ]

    prop = models.ForeignKey(
        "Property", on_delete=models.CASCADE, null=True, blank=True, related_name="pos_sales"
    )
    sale_reference = models.CharField(max_length=20, unique=True)
    booking = models.ForeignKey('Booking', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='pos_sales')
    guest = models.ForeignKey('Guest', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='pos_sales')
    shift = models.ForeignKey('POSShift', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='sales')

    subtotal = models.DecimalField(max_digits=10, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=10, decimal_places=2)

    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES)
    cash_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    card_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    cash_tendered = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    change_given = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='completed')
    notes = models.TextField(blank=True)
    refund_reason = models.TextField(blank=True)
    original_sale = models.ForeignKey('self', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='refunds')
    served_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL,
        null=True, related_name='pos_sales')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.sale_reference:
            import random
            date_str = timezone.now().strftime('%Y%m%d')
            for _ in range(100):
                rand = random.randint(1000, 9999)
                ref = f"POS-{date_str}-{rand}"
                if not POSSale.objects.filter(sale_reference=ref).exists():
                    self.sale_reference = ref
                    break
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.sale_reference} — R{self.total}"

    @property
    def items_summary(self):
        items = self.items.all()
        if not items:
            return "—"
        names = [f"{item.name} x{item.quantity:.0f}" for item in items[:3]]
        suffix = f" +{items.count() - 3} more" if items.count() > 3 else ""
        return ", ".join(names) + suffix


class POSSaleItem(models.Model):
    sale = models.ForeignKey(POSSale, on_delete=models.CASCADE, related_name='items')
    pos_item = models.ForeignKey(POSItem, on_delete=models.SET_NULL,
        null=True, blank=True)
    name = models.CharField(max_length=200)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    discount_pct = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    line_total = models.DecimalField(max_digits=10, decimal_places=2)

    def save(self, *args, **kwargs):
        discount = self.unit_price * (self.discount_pct / 100)
        self.line_total = (self.unit_price - discount) * self.quantity
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} x{self.quantity}"


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


class POSShift(models.Model):
    prop = models.ForeignKey(
        "Property", on_delete=models.CASCADE, null=True, blank=True, related_name="pos_shifts"
    )
    opened_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL,
        null=True, related_name='pos_shifts_opened')
    closed_by = models.ForeignKey('auth.User', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='pos_shifts_closed')
    opening_float = models.DecimalField(max_digits=10, decimal_places=2,
        default=0, help_text="Cash in till at start of shift")
    closing_cash = models.DecimalField(max_digits=10, decimal_places=2,
        null=True, blank=True)
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-opened_at']

    @property
    def is_open(self):
        return self.closed_at is None

    @property
    def sales_during_shift(self):
        qs = POSSale.objects.filter(
            created_at__gte=self.opened_at,
            status='completed',
        )
        if self.closed_at:
            qs = qs.filter(created_at__lte=self.closed_at)
        return qs

    @property
    def total_sales(self):
        result = self.sales_during_shift.aggregate(total=Sum('total'))['total']
        return result or Decimal('0.00')

    @property
    def cash_sales_total(self):
        result = self.sales_during_shift.filter(
            payment_method__in=['cash', 'split']
        ).aggregate(total=Sum('cash_amount'))['total']
        return result or Decimal('0.00')

    @property
    def expected_cash(self):
        return self.opening_float + self.cash_sales_total

    @property
    def cash_variance(self):
        if self.closing_cash is None:
            return None
        return self.closing_cash - self.expected_cash

    def __str__(self):
        return f"Shift opened {self.opened_at.strftime('%d %b %Y %H:%M')}"
