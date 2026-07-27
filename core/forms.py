import datetime
from decimal import Decimal

from django import forms
from django.contrib.auth import authenticate
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError
from PIL import Image, UnidentifiedImageError

from .models import (
    Booking, BookingRefund, Expense, Guest, GuestHouseSettings, InventoryItem, Payment, RatePlan,
    Room, RoomType, SpaAppointment, SpaClientProfile, SpaPackage, SpaPackageItem, SpaPayment,
    SpaService, SpaServiceProduct, SpaTherapist, SpaTreatmentRoom, SpaVoucher, SpaWaitlist, StaffProfile,
)


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


class EmailLoginForm(AuthenticationForm):
    username = forms.CharField(label="Email or username", max_length=254)


class PhonePinLoginForm(forms.Form):
    phone_number = forms.CharField(label="Phone Number", max_length=30)
    pin = forms.CharField(
        label="PIN",
        min_length=4,
        max_length=6,
        strip=False,
        widget=forms.PasswordInput(attrs={"inputmode": "numeric", "autocomplete": "current-password"}),
    )

    error_message = "Unable to sign in with those details."

    def __init__(self, request=None, *args, **kwargs):
        self.request = request
        self.user_cache = None
        super().__init__(*args, **kwargs)

    def clean_phone_number(self):
        phone = StaffProfile.normalize_phone(self.cleaned_data["phone_number"])
        if not phone:
            raise forms.ValidationError("Phone number is required.")
        return phone

    def clean_pin(self):
        try:
            return StaffProfile.validate_pin(self.cleaned_data["pin"])
        except ValidationError as exc:
            raise forms.ValidationError(exc.messages[0]) from exc

    def clean(self):
        cleaned_data = super().clean()
        phone = cleaned_data.get("phone_number")
        pin = cleaned_data.get("pin")
        if phone and pin:
            self.user_cache = authenticate(self.request, phone_number=phone, pin=pin)
            if self.user_cache is None:
                raise forms.ValidationError(self.error_message, code="invalid_login")
        return cleaned_data

    def get_user(self):
        return self.user_cache


class StaffPinSetupForm(forms.Form):
    phone_number = forms.CharField(required=False, max_length=30)
    pin_enabled = forms.BooleanField(required=False)
    pin = forms.CharField(
        required=False,
        min_length=4,
        max_length=6,
        strip=False,
        widget=forms.PasswordInput(attrs={"inputmode": "numeric", "autocomplete": "new-password"}),
    )
    role = forms.ChoiceField(choices=StaffProfile.ROLE_CHOICES)

    def __init__(self, *args, staff_user=None, **kwargs):
        self.staff_user = staff_user
        super().__init__(*args, **kwargs)

    def clean_phone_number(self):
        phone = StaffProfile.normalize_phone(self.cleaned_data.get("phone_number"))
        if not phone:
            return None
        duplicate = StaffProfile.objects.filter(phone_number=phone)
        if self.staff_user:
            duplicate = duplicate.exclude(user=self.staff_user)
        if duplicate.exists():
            raise forms.ValidationError("This phone number is already assigned to another staff member.")
        return phone

    def clean_pin(self):
        pin = self.cleaned_data.get("pin", "")
        if not pin:
            return ""
        try:
            return StaffProfile.validate_pin(pin)
        except ValidationError as exc:
            raise forms.ValidationError(exc.messages[0]) from exc

    def clean(self):
        cleaned_data = super().clean()
        if not cleaned_data.get("pin_enabled"):
            return cleaned_data
        if not cleaned_data.get("phone_number"):
            self.add_error("phone_number", "Phone number is required when PIN login is enabled.")
        existing_hash = ""
        if self.staff_user:
            profile = StaffProfile.objects.filter(user=self.staff_user).first()
            existing_hash = profile.pin_hash if profile else ""
        if not cleaned_data.get("pin") and not existing_hash:
            self.add_error("pin", "Set a PIN before enabling PIN login.")
        return cleaned_data

    def save(self, user):
        profile, _ = StaffProfile.objects.get_or_create(user=user)
        profile.phone_number = self.cleaned_data.get("phone_number")
        profile.role = self.cleaned_data["role"]
        if not self.cleaned_data.get("pin_enabled"):
            profile.disable_pin()
        else:
            if self.cleaned_data.get("pin"):
                profile.set_pin(self.cleaned_data["pin"])
            profile.pin_enabled = True
            profile.pin_failed_attempts = 0
            profile.pin_locked_until = None
        profile.save()
        return profile


class GuestHouseSettingsForm(forms.ModelForm):
    class Meta:
        model = GuestHouseSettings
        fields = [
            "guest_house_name",
            "logo",
            "phone",
            "email",
            "address",
            "vat_registered",
            "vat_number",
            "vat_rate",
            "check_in_time",
            "check_out_time",
            "currency",
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
            "pdf_primary_color",
            "banking_details",
            "cancellation_note",
            "invoice_notes",
            "receipt_notes",
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
            "room_category",
            "rate_plan",
            "pricing_model",
            "booking_mode",
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
        self.prop = kwargs.pop("prop", None)
        super().__init__(*args, **kwargs)
        if self.prop is None and self.instance and self.instance.pk:
            self.prop = self.instance.prop
        settings_obj = GuestHouseSettings.objects.filter(pk=1).first()
        if not (settings_obj and settings_obj.shared_capacity_booking_enabled):
            self.fields.pop("booking_mode", None)
        for field_name, field in self.fields.items():
            if field_name == "booking_types_allowed":
                field.widget.attrs.update({"class": "room-checkbox"})
                continue
            field.widget.attrs.update({"class": "form-input"})
        self.fields["name"].widget.attrs.update({"placeholder": "e.g. Garden Suite", "autofocus": True})
        self.fields["max_guests"].widget.attrs.update({"min": 1})
        for field_name in (
            "price_per_night", "price_per_week", "price_1_hour", "price_2_hours",
            "price_3_hours", "price_5_hours",
        ):
            self.fields[field_name].widget.attrs.update({"min": "0", "step": "0.01"})
        if self.instance and self.instance.pk:
            self.fields["booking_types_allowed"].initial = self.instance.booking_types_list()

    def clean_booking_types_allowed(self):
        value = self.cleaned_data.get("booking_types_allowed") or ["Daily"]
        return ",".join(value)

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        qs = Room.objects.filter(prop=self.prop, name__iexact=name)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("A room with this name already exists at this property.")
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
    IDENTITY_CHOICES = (
        ("walk_in", "Walk-in guest"),
        ("guest", "Guest profile"),
        ("plate", "Vehicle number"),
    )
    identity_mode = forms.ChoiceField(choices=IDENTITY_CHOICES, initial="walk_in", widget=forms.HiddenInput)

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
            "vehicle_registration",
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
        # Keep the system walk-in guest internal; staff choose a real guest or plate mode.
        Guest.get_generic()
        self.fields["guest"].required = False
        self.fields["guest"].queryset = Guest.objects.filter(is_generic=False)
        self.fields["guest"].label_from_instance = lambda obj: f"{obj.full_name} ({obj.phone})"
        self.fields["room"].label_from_instance = lambda obj: f"{obj.name} ({obj.room_type})"
        if not self.is_bound and self.instance.pk:
            if self.instance.vehicle_registration:
                self.initial["identity_mode"] = "plate"
            elif self.instance.guest.is_generic:
                self.initial["identity_mode"] = "walk_in"
            else:
                self.initial["identity_mode"] = "guest"

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
        identity_mode = cleaned_data.get("identity_mode") or "walk_in"
        vehicle_registration = (cleaned_data.get("vehicle_registration") or "").strip().upper()
        cleaned_data["vehicle_registration"] = vehicle_registration

        if identity_mode == "plate":
            if not vehicle_registration:
                self.add_error("vehicle_registration", "Enter the vehicle number plate.")
            else:
                cleaned_data["guest"] = Guest.get_or_create_for_vehicle(vehicle_registration)
        elif identity_mode == "walk_in":
            cleaned_data["guest"] = Guest.get_generic()
            cleaned_data["vehicle_registration"] = ""
        elif not cleaned_data.get("guest") or cleaned_data["guest"].is_generic:
            self.add_error("guest", "Select a guest or use Number Plate.")
        elif cleaned_data["guest"].vehicle_registration:
            cleaned_data["vehicle_registration"] = cleaned_data["guest"].vehicle_registration

        if num_guests is not None and num_guests < 1:
            raise forms.ValidationError("Number of guests must be at least 1.")
        if num_guests and room and num_guests > room.max_guests:
            self.add_error("num_guests", f"{room.name} sleeps a maximum of {room.max_guests} guest{'s' if room.max_guests != 1 else ''}.")
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
                candidate.num_guests = num_guests or 1
                candidate.discount = discount
                candidate.deposit_required = deposit_required
                guest_multiplier = Decimal(num_guests or 1) if room.pricing_model == "per_person" else Decimal("1")
                if duration_type in ["1_hour", "2_hours", "3_hours", "5_hours"]:
                    subtotal = configured_rate * guest_multiplier
                else:
                    nights = max((candidate.check_out_date - candidate.check_in_date).days, 0)
                    if duration_type == "weekly":
                        subtotal = configured_rate * (Decimal(nights) / Decimal("7")) * guest_multiplier
                    else:
                        subtotal = configured_rate * nights * guest_multiplier
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


class QuickCheckInForm(forms.Form):
    IDENTITY_CHOICES = [
        ("plate", "Number plate"),
        ("existing", "Existing guest"),
        ("walk_in", "Walk-in guest"),
    ]
    DURATION_LABELS = {
        "1_hour": "1 Hour",
        "2_hours": "2 Hours",
        "3_hours": "3 Hours",
        "5_hours": "5 Hours",
        "daily": "Overnight",
    }

    identity_mode = forms.ChoiceField(choices=IDENTITY_CHOICES, initial="plate")
    guest = forms.ModelChoiceField(queryset=Guest.objects.none(), required=False, empty_label="Choose a guest")
    vehicle_registration = forms.CharField(required=False, max_length=20)
    duration = forms.ChoiceField(choices=())
    num_guests = forms.IntegerField(min_value=1, initial=1)

    def __init__(self, *args, room, **kwargs):
        super().__init__(*args, **kwargs)
        self.room = room
        self.fields["guest"].queryset = Guest.objects.filter(is_generic=False).order_by("first_name", "last_name")
        self.fields["num_guests"].max_value = room.max_guests
        self.fields["num_guests"].widget.attrs.update({"min": 1, "max": room.max_guests})

        duration_choices = []
        for value in ("1_hour", "2_hours", "3_hours", "5_hours", "daily"):
            price = room.get_price_for_duration(value)
            if price is not None and price > 0:
                duration_choices.append((value, f"{self.DURATION_LABELS[value]} — R {price:.2f}"))
        self.fields["duration"].choices = duration_choices
        if duration_choices:
            self.fields["duration"].initial = duration_choices[0][0]

    def clean_vehicle_registration(self):
        return self.cleaned_data.get("vehicle_registration", "").strip().upper()

    def clean(self):
        cleaned_data = super().clean()
        identity_mode = cleaned_data.get("identity_mode")
        if identity_mode == "plate" and not cleaned_data.get("vehicle_registration"):
            self.add_error("vehicle_registration", "Enter the vehicle number plate.")
        if identity_mode == "existing" and not cleaned_data.get("guest"):
            self.add_error("guest", "Choose the guest who is checking in.")
        if not self.fields["duration"].choices:
            self.add_error("duration", "This room has no configured check-in rates.")
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
            field.widget.attrs.update({"class": "form-input"})
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


class SplitPaymentDetailsForm(forms.Form):
    payment_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({"class": "form-input"})


class PaymentTenderForm(forms.Form):
    payment_method = forms.ChoiceField(choices=(("", "Select method"),) + tuple(Payment.PAYMENT_METHOD_CHOICES))
    amount = forms.DecimalField(min_value=Decimal("0.01"), max_digits=10, decimal_places=2)
    reference = forms.CharField(required=False, max_length=100)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({"class": "form-input"})
        self.fields["amount"].widget.attrs.update({"min": "0.01", "step": "0.01", "placeholder": "0.00"})
        self.fields["reference"].widget.attrs.update({"placeholder": "Optional reference"})


class BasePaymentTenderFormSet(forms.BaseFormSet):
    def __init__(self, *args, balance_due=None, **kwargs):
        self.balance_due = balance_due
        super().__init__(*args, **kwargs)

    def clean(self):
        super().clean()
        if any(self.errors):
            return

        tenders = [form.cleaned_data for form in self.forms if form.cleaned_data]
        methods = [tender["payment_method"] for tender in tenders]
        if len(methods) != len(set(methods)):
            raise forms.ValidationError("Use each payment method only once. Combine amounts that use the same method.")

        total = sum((tender["amount"] for tender in tenders), Decimal("0.00"))
        if self.balance_due is not None and total > self.balance_due:
            raise forms.ValidationError(
                f"Split payment total cannot exceed the outstanding balance of R {self.balance_due:.2f}."
            )


PaymentTenderFormSet = forms.formset_factory(
    PaymentTenderForm,
    formset=BasePaymentTenderFormSet,
    extra=3,
    min_num=2,
    max_num=3,
    validate_min=True,
    validate_max=True,
)


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


class SpaServiceForm(forms.ModelForm):
    class Meta:
        model = SpaService
        fields = ["name", "category", "description", "duration_minutes", "price", "is_active"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({"class": "form-input"})


class SpaAppointmentForm(forms.ModelForm):
    class Meta:
        model = SpaAppointment
        fields = [
            "service", "guest", "booking", "guest_name", "guest_phone",
            "assigned_therapist", "therapist", "treatment_room", "package",
            "scheduled_date", "scheduled_time",
            "price_charged", "status",
            "consultation_notes", "notes",
        ]
        widgets = {
            "scheduled_date": forms.DateInput(attrs={"type": "date"}),
            "scheduled_time": forms.TimeInput(attrs={"type": "time"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
            "consultation_notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, prop=None, exclude_pk=None, **kwargs):
        self._prop = prop
        self._exclude_pk = exclude_pk
        super().__init__(*args, **kwargs)
        if prop:
            self.fields["service"].queryset = SpaService.objects.filter(prop=prop, is_active=True)
            self.fields["guest"].queryset = Guest.objects.all()
            self.fields["booking"].queryset = Booking.objects.all().order_by("-created_at")
            self.fields["assigned_therapist"].queryset = SpaTherapist.objects.filter(prop=prop, is_active=True)
            self.fields["treatment_room"].queryset = SpaTreatmentRoom.objects.filter(prop=prop, is_active=True)
            self.fields["package"].queryset = SpaPackage.objects.filter(prop=prop, is_active=True)
        optional = ["guest", "booking", "guest_name", "guest_phone", "therapist",
                    "assigned_therapist", "treatment_room", "package",
                    "consultation_notes", "status"]
        for f in optional:
            self.fields[f].required = False
        for field in self.fields.values():
            field.widget.attrs.update({"class": "form-input"})

    def clean(self):
        cleaned = super().clean()
        service = cleaned.get("service")
        therapist = cleaned.get("assigned_therapist")
        room = cleaned.get("treatment_room")
        date = cleaned.get("scheduled_date")
        time = cleaned.get("scheduled_time")
        if not (service and date and time):
            return cleaned
        import datetime
        start_dt = datetime.datetime.combine(date, time)
        duration = datetime.timedelta(minutes=service.duration_minutes)
        end_dt = start_dt + duration
        qs = SpaAppointment.objects.filter(
            prop=self._prop, scheduled_date=date,
            status__in=["pending", "confirmed", "in_progress"],
        )
        if self._exclude_pk:
            qs = qs.exclude(pk=self._exclude_pk)
        for appt in qs.select_related("service"):
            a_start = datetime.datetime.combine(date, appt.scheduled_time)
            a_end = a_start + datetime.timedelta(minutes=appt.service.duration_minutes)
            overlaps = start_dt < a_end and end_dt > a_start
            if overlaps and therapist and appt.assigned_therapist_id == therapist.pk:
                raise forms.ValidationError(
                    f"{therapist.name} already has an appointment from "
                    f"{appt.scheduled_time.strftime('%H:%M')} to {a_end.strftime('%H:%M')}."
                )
            if overlaps and room and appt.treatment_room_id == room.pk:
                raise forms.ValidationError(
                    f"{room.name} is already booked from "
                    f"{appt.scheduled_time.strftime('%H:%M')} to {a_end.strftime('%H:%M')}."
                )
        return cleaned


class SpaTherapistForm(forms.ModelForm):
    class Meta:
        model = SpaTherapist
        fields = ["name", "phone", "email", "specialties", "commission_pct", "notes", "is_active"]
        widgets = {
            "specialties": forms.Textarea(attrs={"rows": 2}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({"class": "form-input"})


class SpaTreatmentRoomForm(forms.ModelForm):
    class Meta:
        model = SpaTreatmentRoom
        fields = ["name", "description", "is_active"]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({"class": "form-input"})


class SpaPackageForm(forms.ModelForm):
    class Meta:
        model = SpaPackage
        fields = ["name", "description", "package_price", "is_active"]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({"class": "form-input"})


class SpaVoucherForm(forms.ModelForm):
    class Meta:
        model = SpaVoucher
        fields = ["value", "issued_to_name", "issued_to_email", "issued_to_phone",
                  "valid_from", "valid_until", "notes"]
        widgets = {
            "valid_from": forms.DateInput(attrs={"type": "date"}),
            "valid_until": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["issued_to_email"].required = False
        self.fields["issued_to_phone"].required = False
        self.fields["valid_until"].required = False
        self.fields["notes"].required = False
        for field in self.fields.values():
            field.widget.attrs.update({"class": "form-input"})


class SpaWaitlistForm(forms.ModelForm):
    class Meta:
        model = SpaWaitlist
        fields = ["service", "preferred_therapist", "preferred_date",
                  "guest", "guest_name", "guest_phone", "guest_email", "notes"]
        widgets = {
            "preferred_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, prop=None, **kwargs):
        super().__init__(*args, **kwargs)
        if prop:
            self.fields["service"].queryset = SpaService.objects.filter(prop=prop, is_active=True)
            self.fields["preferred_therapist"].queryset = SpaTherapist.objects.filter(prop=prop, is_active=True)
            self.fields["guest"].queryset = Guest.objects.all()
        optional = ["service", "preferred_therapist", "preferred_date", "guest",
                    "guest_name", "guest_phone", "guest_email", "notes"]
        for f in optional:
            self.fields[f].required = False
        for field in self.fields.values():
            field.widget.attrs.update({"class": "form-input"})


class SpaClientProfileForm(forms.ModelForm):
    class Meta:
        model = SpaClientProfile
        fields = ["allergies", "contraindications", "skin_type", "pressure_preference",
                  "preferred_therapist", "general_notes"]
        widgets = {
            "allergies": forms.Textarea(attrs={"rows": 2}),
            "contraindications": forms.Textarea(attrs={"rows": 2}),
            "general_notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, prop=None, **kwargs):
        super().__init__(*args, **kwargs)
        if prop:
            self.fields["preferred_therapist"].queryset = SpaTherapist.objects.filter(prop=prop, is_active=True)
        optional = ["allergies", "contraindications", "skin_type", "pressure_preference",
                    "preferred_therapist", "general_notes"]
        for f in optional:
            self.fields[f].required = False
        for field in self.fields.values():
            field.widget.attrs.update({"class": "form-input"})


class SpaServiceProductForm(forms.ModelForm):
    class Meta:
        model = SpaServiceProduct
        fields = ["inventory_item", "quantity_used"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({"class": "form-input"})


class SpaPaymentForm(forms.ModelForm):
    class Meta:
        model = SpaPayment
        fields = ["amount", "payment_date", "payment_method", "payment_type", "reference", "notes"]
        widgets = {
            "payment_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.update({"class": PREMIUM_FIELD_CLASSES})
        self.fields["payment_method"].choices = SpaPayment.PAYMENT_METHOD_CHOICES
        self.fields["payment_method"].initial = "Cash"
        self.fields["payment_type"].choices = SpaPayment.PAYMENT_TYPE_CHOICES
        self.fields["payment_type"].initial = "Payment"

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount <= 0:
            raise forms.ValidationError("Payment amount must be greater than zero.")
        return amount
