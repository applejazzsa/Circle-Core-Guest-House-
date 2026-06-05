import datetime
from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError
from PIL import Image, UnidentifiedImageError

from .models import Booking, BookingRefund, Expense, Guest, GuestHouseSettings, Payment, Room


PREMIUM_FIELD_CLASSES = (
    "block w-full rounded-xl border border-gray-200 bg-white px-4 py-3 text-gray-900 "
    "transition focus:border-[#c9a84c] focus:outline-none focus:ring-1 focus:ring-[#c9a84c]"
)
PREMIUM_FILE_CLASSES = (
    "block w-full rounded-xl border border-gray-200 bg-white px-4 py-3 text-sm text-gray-700 "
    "file:mr-4 file:rounded-lg file:border-0 file:bg-[#1a1a2e] file:px-4 file:py-2 "
    "file:text-sm file:font-semibold file:text-white hover:file:bg-[#2a2a4e]"
)
MAX_IMAGE_UPLOAD_SIZE = 5 * 1024 * 1024
ALLOWED_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}


def validate_image_upload(upload):
    if not upload:
        return
    if upload.size > MAX_IMAGE_UPLOAD_SIZE:
        raise ValidationError("Image uploads must be 5MB or smaller.")
    content_type = getattr(upload, "content_type", "")
    if content_type not in ALLOWED_IMAGE_CONTENT_TYPES:
        raise ValidationError("Upload a JPG, PNG, or WebP image.")
    try:
        image = Image.open(upload)
        image.verify()
    except (UnidentifiedImageError, OSError):
        raise ValidationError("Upload a valid image file.")
    finally:
        upload.seek(0)


class GuestHouseSettingsForm(forms.ModelForm):
    class Meta:
        model = GuestHouseSettings
        fields = [
            "guest_house_name",
            "logo",
            "phone",
            "email",
            "address",
            "banking_details",
            "vat_registered",
            "vat_number",
            "vat_rate",
            "check_in_time",
            "check_out_time",
            "currency",
            "cancellation_note",
            "invoice_notes",
            "receipt_notes",
            "enable_hourly_bookings",
            "enable_weekly_bookings",
            "hourly_booking_label",
            "minimum_hourly_rate",
            "default_price_2_hours",
            "default_price_3_hours",
            "default_price_per_night",
            "default_price_24_hours",
            "late_checkout_fee",
            "early_checkin_fee",
            "weekend_surcharge_pct",
            "seasonal_note",
            "pos_api_key",
        ]
        widgets = {
            "check_in_time": forms.TimeInput(attrs={"type": "time"}),
            "check_out_time": forms.TimeInput(attrs={"type": "time"}),
            "address": forms.Textarea(attrs={"rows": 3}),
            "banking_details": forms.Textarea(attrs={"rows": 3}),
            "cancellation_note": forms.Textarea(attrs={"rows": 3}),
            "invoice_notes": forms.Textarea(attrs={"rows": 3}),
            "receipt_notes": forms.Textarea(attrs={"rows": 3}),
            "seasonal_note": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            if field_name == "vat_registered":
                field.widget.attrs.update(
                    {
                        "class": "h-4 w-4 rounded border-slate-300 text-gold focus:ring-gold",
                    }
                )
                continue
            if field_name in ["enable_hourly_bookings", "enable_weekly_bookings"]:
                field.widget.attrs.update(
                    {
                        "class": "h-4 w-4 rounded border-slate-300 text-gold focus:ring-gold",
                    }
                )
                continue
            if field_name == "logo":
                field.widget.attrs.update(
                    {
                        "class": (
                            "mt-1 block w-full text-sm text-slate-700 "
                            "file:mr-4 file:rounded-md file:border-0 file:bg-navy "
                            "file:px-4 file:py-2 file:text-sm file:font-semibold "
                            "file:text-white hover:file:bg-navy/90"
                        )
                    }
                )
                continue
            field.widget.attrs.update({"class": PREMIUM_FIELD_CLASSES})

    def clean_logo(self):
        logo = self.cleaned_data.get("logo")
        validate_image_upload(logo)
        return logo


class RoomForm(forms.ModelForm):
    booking_types_allowed = forms.MultipleChoiceField(
        choices=Room.BOOKING_TYPE_CHOICES,
        initial=["Daily"],
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    class Meta:
        model = Room
        fields = [
            "name",
            "room_type",
            "price_per_night",
            "price_per_week",
            "price_1_hour",
            "price_2_hours",
            "price_3_hours",
            "price_5_hours",
            "booking_types_allowed",
            "max_guests",
            "status",
            "cleaning_status",
            "description",
            "internal_notes",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "internal_notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            if field_name == "booking_types_allowed":
                field.widget.attrs.update({"class": "h-4 w-4 rounded border-gray-300 text-gold focus:ring-gold"})
                continue
            field.widget.attrs.update({"class": PREMIUM_FIELD_CLASSES})
        if self.instance and self.instance.pk:
            self.fields["booking_types_allowed"].initial = self.instance.booking_types_list()

    def clean_booking_types_allowed(self):
        value = self.cleaned_data.get("booking_types_allowed") or ["Daily"]
        return ",".join(value)

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        qs = Room.objects.filter(name__iexact=name)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("A room with this name already exists.")
        return name

    def clean_max_guests(self):
        max_guests = self.cleaned_data["max_guests"]
        if max_guests < 1:
            raise forms.ValidationError("Room capacity must be at least 1.")
        return max_guests

    def clean_price_per_night(self):
        price = self.cleaned_data["price_per_night"]
        if price <= 0:
            raise forms.ValidationError("Nightly rate must be greater than zero.")
        return price


class GuestForm(forms.ModelForm):
    class Meta:
        model = Guest
        fields = [
            "first_name",
            "last_name",
            "phone",
            "email",
            "id_passport_number",
            "address",
            "emergency_contact_name",
            "emergency_contact_phone",
            "notes",
        ]
        widgets = {
            "address": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({"class": PREMIUM_FIELD_CLASSES})


class BookingForm(forms.ModelForm):
    class Meta:
        model = Booking
        fields = [
            "guest",
            "room",
            "check_in_date",
            "check_out_date",
            "num_guests",
            "booking_duration_type",
            "booking_start_time",
            "booking_end_time",
            "rate_per_night",
            "discount",
            "deposit_required",
            "booking_source",
            "status",
            "notes",
        ]
        widgets = {
            "check_in_date": forms.DateInput(attrs={"type": "date"}),
            "check_out_date": forms.DateInput(attrs={"type": "date"}),
            "booking_start_time": forms.TimeInput(attrs={"type": "time"}),
            "booking_end_time": forms.TimeInput(attrs={"type": "time", "readonly": "readonly"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({"class": PREMIUM_FIELD_CLASSES})
        # Ensure the generic walk-in guest exists and appears first
        Guest.get_generic()
        self.fields["guest"].queryset = Guest.objects.all()
        self.fields["guest"].label_from_instance = lambda obj: (
            f"[ Walk-in Guest ]" if obj.is_generic else f"{obj.full_name} ({obj.phone})"
        )
        self.fields["room"].label_from_instance = lambda obj: f"{obj.name} ({obj.room_type})"

    def clean(self):
        cleaned_data = super().clean()
        check_in = cleaned_data.get("check_in_date")
        check_out = cleaned_data.get("check_out_date")
        room = cleaned_data.get("room")
        duration_type = cleaned_data.get("booking_duration_type") or "daily"
        start_time = cleaned_data.get("booking_start_time")
        num_guests = cleaned_data.get("num_guests")
        discount = cleaned_data.get("discount") or 0
        deposit_required = cleaned_data.get("deposit_required") or 0

        if num_guests is not None and num_guests < 1:
            raise forms.ValidationError("Number of guests must be at least 1.")
        if discount < 0:
            raise forms.ValidationError("Discount cannot be negative.")
        if deposit_required < 0:
            raise forms.ValidationError("Deposit required cannot be negative.")

        if room and room.status in ["Maintenance", "Blocked", "Cleaning"]:
            editing_same_room = self.instance.pk and self.instance.room_id == room.pk
            if not editing_same_room:
                status_labels = {"Maintenance": "under maintenance", "Blocked": "blocked", "Cleaning": "currently being cleaned"}
                status_label = status_labels.get(room.status, room.status.lower())
                raise forms.ValidationError(f"{room.name} cannot be booked because it is {status_label}.")

        if check_in and check_out:
            if duration_type not in ["1_hour", "2_hours", "3_hours", "5_hours"] and check_out <= check_in:
                raise forms.ValidationError("Check-out date must be after check-in date.")

            if room:
                configured_rate = room.get_price_for_duration(duration_type)
                if configured_rate is None or configured_rate <= 0:
                    raise forms.ValidationError(
                        f"{room.name} does not have a configured rate for the selected booking duration."
                    )
                cleaned_data["rate_per_night"] = configured_rate

                if duration_type in ["1_hour", "2_hours", "3_hours", "5_hours"]:
                    if not start_time:
                        raise forms.ValidationError("Start time is required for hourly bookings.")
                    hours = {"1_hour": 1, "2_hours": 2, "3_hours": 3, "5_hours": 5}[duration_type]
                    start_dt = datetime.datetime.combine(check_in, start_time)
                    end_dt = start_dt + datetime.timedelta(hours=hours)
                    cleaned_data["check_out_date"] = check_in
                    cleaned_data["booking_end_time"] = end_dt.time()
                candidate = self.instance if self.instance.pk else Booking()
                candidate.room = room
                candidate.check_in_date = check_in
                candidate.check_out_date = cleaned_data.get("check_out_date") or check_out
                candidate.booking_duration_type = duration_type
                candidate.booking_start_time = start_time
                candidate.booking_end_time = cleaned_data.get("booking_end_time")
                candidate.status = cleaned_data.get("status") or "Pending"
                candidate.rate_per_night = cleaned_data.get("rate_per_night") or configured_rate
                candidate.discount = discount
                candidate.deposit_required = deposit_required
                if duration_type in ["1_hour", "2_hours", "3_hours", "5_hours"]:
                    subtotal = configured_rate
                else:
                    nights = max((candidate.check_out_date - candidate.check_in_date).days, 0)
                    if duration_type == "weekly":
                        subtotal = configured_rate * (Decimal(nights) / Decimal("7"))
                    else:
                        subtotal = configured_rate * nights
                candidate.compute_totals()
                if discount > subtotal:
                    raise forms.ValidationError("Discount cannot be bigger than the booking subtotal.")
                if deposit_required > candidate.total_amount:
                    raise forms.ValidationError("Deposit required cannot be bigger than the booking total.")
                conflict = candidate.overlapping_bookings().select_related("guest").first()
                if conflict:
                    raise forms.ValidationError(
                        f"Double booking conflict: {room.name} is already booked for that time "
                        f"({conflict.booking_reference} - {conflict.guest.full_name})."
                    )

        return cleaned_data


class PaymentForm(forms.ModelForm):
    class Meta:
        model = Payment
        fields = [
            "amount",
            "payment_date",
            "payment_method",
            "reference",
            "notes",
        ]
        widgets = {
            "payment_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({"class": PREMIUM_FIELD_CLASSES})
        self.fields["payment_method"].choices = Payment.PAYMENT_METHOD_CHOICES
        self.fields["payment_method"].initial = "Cash"

    def save(self, commit=True):
        payment = super().save(commit=False)
        payment.payment_type = "Payment"
        if commit:
            payment.save()
        return payment

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount <= 0:
            raise forms.ValidationError("Payment amount must be greater than zero.")
        return amount


class BookingRefundForm(forms.ModelForm):
    class Meta:
        model = BookingRefund
        fields = [
            "amount",
            "refund_date",
            "refund_method",
            "reference",
            "reason",
        ]
        widgets = {
            "refund_date": forms.DateInput(attrs={"type": "date"}),
            "reason": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, max_refund=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_refund = max_refund
        for field in self.fields.values():
            field.widget.attrs.update({"class": PREMIUM_FIELD_CLASSES})
        self.fields["refund_method"].initial = "Cash"

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount <= 0:
            raise forms.ValidationError("Refund amount must be greater than zero.")
        if self.max_refund is not None and amount > self.max_refund:
            raise forms.ValidationError(f"Refund cannot exceed available paid amount of R {self.max_refund}.")
        return amount


class ExpenseForm(forms.ModelForm):
    class Meta:
        model = Expense
        fields = [
            "date",
            "category",
            "description",
            "amount",
            "paid_to",
            "payment_method",
            "reference",
            "notes",
        ]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({"class": PREMIUM_FIELD_CLASSES})
