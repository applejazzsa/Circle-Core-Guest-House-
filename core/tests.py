"""
Basic test suite for Circle Core Guest House.
Run with: python manage.py test core
"""

import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase

from .models import (
    Booking,
    Guest,
    GuestHouseSettings,
    Payment,
    Property,
    Room,
    Subscription,
    SubscriptionPlan,
)


class TenantClient(Client):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("HTTP_HOST", "tenant.test.com")
        super().__init__(*args, **kwargs)


class CircleCoreTenantTestCase(TenantTestCase):
    client_class = TenantClient

    @classmethod
    def setup_tenant(cls, tenant):
        tenant.name = "Test Guest House"
        tenant.owner_name = "Test Owner"
        tenant.owner_email = f"{cls.get_test_schema_name()}@example.com"
        tenant.owner_phone = "0810000000"
        tenant.is_active = True
        tenant.is_verified = True

    @classmethod
    def setup_domain(cls, domain):
        domain.is_primary = True


# ── Helpers ───────────────────────────────────────────────────────────────────


def make_owner(username="owner", password="testpass123"):
    user = User.objects.create_superuser(username, f"{username}@example.com", password)
    return user


def make_room(name="Room 1", price=500):
    prop = Property.objects.filter(is_active=True).order_by("pk").first()
    if not prop:
        prop = Property.objects.create(name="Test Property", is_active=True)
    return Room.objects.create(
        prop=prop,
        name=name,
        room_type="Double",
        max_guests=2,
        status="Available",
        cleaning_status="Clean",
        price_per_night=Decimal(str(price)),
    )


def make_guest(first_name="Test", last_name="Guest", phone="0810000001"):
    return Guest.objects.create(
        first_name=first_name,
        last_name=last_name,
        phone=phone,
        is_generic=False,
    )


def make_booking(room, guest, days_ahead=1, nights=2):
    check_in = timezone.localdate() + datetime.timedelta(days=days_ahead)
    check_out = check_in + datetime.timedelta(days=nights)
    return Booking.objects.create(
        room=room,
        guest=guest,
        check_in_date=check_in,
        check_out_date=check_out,
        booking_duration_type="daily",
        num_guests=1,
        rate_per_night=room.price_per_night,
        status="Confirmed",
        booking_source="Walk-in",
        balance_due=room.price_per_night * nights,
        total_amount=room.price_per_night * nights,
    )


def activate_trial(owner):
    """Set up a trial subscription so middleware doesn't block requests."""
    # Use the professional plan seeded by migration — it has all features enabled.
    plan = SubscriptionPlan.objects.get(name="professional")
    Subscription.objects.create(
        plan=plan,
        status="trial",
        expires_at=timezone.now() + datetime.timedelta(days=30),
        owner_name=owner.username,
        owner_email=owner.email,
    )
    GuestHouseSettings.objects.get_or_create(pk=1, defaults={"guest_house_name": "Test House"})


# ── Model tests ───────────────────────────────────────────────────────────────


class RoomModelTest(CircleCoreTenantTestCase):
    def test_room_str(self):
        room = make_room("Deluxe Suite")
        self.assertIn("Deluxe Suite", str(room))

    def test_room_default_status(self):
        room = make_room()
        self.assertEqual(room.status, "Available")

    def test_room_price_stored(self):
        room = make_room(price=750)
        self.assertEqual(room.price_per_night, Decimal("750"))


class GuestModelTest(CircleCoreTenantTestCase):
    def test_full_name(self):
        guest = make_guest("John", "Doe")
        self.assertEqual(guest.full_name, "John Doe")

    def test_str_representation(self):
        guest = make_guest("Jane", "Smith")
        self.assertIn("Jane", str(guest))


class BookingModelTest(CircleCoreTenantTestCase):
    def setUp(self):
        self.room = make_room()
        self.guest = make_guest()

    def test_booking_reference_generated(self):
        booking = make_booking(self.room, self.guest)
        self.assertTrue(booking.booking_reference.startswith("CC"))

    def test_booking_num_nights(self):
        booking = make_booking(self.room, self.guest, nights=3)
        self.assertEqual(booking.num_nights, 3)

    def test_booking_balance_due(self):
        booking = make_booking(self.room, self.guest, nights=2)
        expected = self.room.price_per_night * 2
        self.assertEqual(booking.balance_due, expected)

    def test_back_to_back_booking_allowed(self):
        booking = make_booking(self.room, self.guest, days_ahead=5, nights=2)
        next_guest = make_guest("Back", "ToBack", "0810000099")
        next_booking = Booking.objects.create(
            room=self.room,
            guest=next_guest,
            check_in_date=booking.check_out_date,
            check_out_date=booking.check_out_date + datetime.timedelta(days=1),
            booking_duration_type="daily",
            num_guests=1,
            rate_per_night=self.room.price_per_night,
            status="Confirmed",
            booking_source="Walk-in",
        )
        self.assertEqual(next_booking.check_in_date, booking.check_out_date)


# ── View tests ────────────────────────────────────────────────────────────────


class AuthViewTest(CircleCoreTenantTestCase):
    def test_login_page_loads(self):
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)

    def test_redirect_unauthenticated(self):
        response = self.client.get(reverse("core:home"))
        self.assertRedirects(response, f"{reverse('login')}?next={reverse('core:home')}")

    def test_login_with_valid_credentials(self):
        owner = make_owner()
        activate_trial(owner)
        response = self.client.post(
            reverse("login"),
            {"username": "owner", "password": "testpass123"},
            follow=True,
        )
        self.assertRedirects(response, reverse("core:home"))


class DashboardViewTest(CircleCoreTenantTestCase):
    def setUp(self):
        self.owner = make_owner()
        activate_trial(self.owner)
        self.client.login(username="owner", password="testpass123")

    def test_dashboard_loads(self):
        response = self.client.get(reverse("core:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dashboard")

    def test_booking_list_loads(self):
        response = self.client.get(reverse("core:booking_list"))
        self.assertEqual(response.status_code, 200)

    def test_room_list_loads(self):
        response = self.client.get(reverse("core:room_list"))
        self.assertEqual(response.status_code, 200)

    def test_guest_list_loads(self):
        response = self.client.get(reverse("core:guest_list"))
        self.assertEqual(response.status_code, 200)

    def test_payment_list_loads(self):
        response = self.client.get(reverse("core:payment_list"))
        self.assertEqual(response.status_code, 200)

    def test_cleaning_board_loads(self):
        response = self.client.get(reverse("core:cleaning"))
        self.assertEqual(response.status_code, 200)

    def test_reports_loads(self):
        response = self.client.get(reverse("core:reports"))
        self.assertEqual(response.status_code, 200)


class BookingFlowTest(CircleCoreTenantTestCase):
    def setUp(self):
        self.owner = make_owner()
        activate_trial(self.owner)
        self.client.login(username="owner", password="testpass123")
        self.room = make_room()
        self.guest = make_guest()

    def test_booking_detail_loads(self):
        booking = make_booking(self.room, self.guest)
        response = self.client.get(reverse("core:booking_detail", args=[booking.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, booking.booking_reference)

    def test_booking_add_page_loads(self):
        response = self.client.get(reverse("core:booking_add"))
        self.assertEqual(response.status_code, 200)

    def test_checkin_changes_status(self):
        booking = make_booking(self.room, self.guest)
        self.client.post(reverse("core:booking_checkin", args=[booking.pk]))
        booking.refresh_from_db()
        self.assertEqual(booking.status, "Checked In")

    def test_checkout_changes_status(self):
        booking = make_booking(self.room, self.guest)
        booking.status = "Checked In"
        booking.check_in_time = timezone.now()
        booking.save()
        self.client.post(reverse("core:booking_checkout", args=[booking.pk]))
        booking.refresh_from_db()
        self.assertEqual(booking.status, "Checked Out")

    def test_room_becomes_occupied_on_checkin(self):
        booking = make_booking(self.room, self.guest)
        self.client.post(reverse("core:booking_checkin", args=[booking.pk]))
        self.room.refresh_from_db()
        self.assertEqual(self.room.status, "Occupied")

    def test_room_set_to_cleaning_on_checkout(self):
        booking = make_booking(self.room, self.guest)
        booking.status = "Checked In"
        booking.check_in_time = timezone.now()
        booking.save()
        self.room.status = "Occupied"
        self.room.save()
        self.client.post(reverse("core:booking_checkout", args=[booking.pk]))
        self.room.refresh_from_db()
        self.assertEqual(self.room.status, "Cleaning")

    def test_daily_booking_blocks_overlapping_daily_booking(self):
        booking = make_booking(self.room, self.guest, days_ahead=1, nights=2)
        other_guest = make_guest("Second", "Guest", "0810000002")
        with self.assertRaises(ValidationError):
            Booking.objects.create(
                room=self.room,
                guest=other_guest,
                check_in_date=booking.check_in_date + datetime.timedelta(days=1),
                check_out_date=booking.check_out_date + datetime.timedelta(days=1),
                booking_duration_type="daily",
                num_guests=1,
                rate_per_night=self.room.price_per_night,
                status="Confirmed",
                booking_source="Walk-in",
            )

    def test_daily_booking_blocks_hourly_booking_inside_stay(self):
        booking = make_booking(self.room, self.guest, days_ahead=1, nights=2)
        other_guest = make_guest("Hourly", "Guest", "0810000003")
        with self.assertRaises(ValidationError):
            Booking.objects.create(
                room=self.room,
                guest=other_guest,
                check_in_date=booking.check_in_date,
                check_out_date=booking.check_in_date,
                booking_duration_type="1_hour",
                booking_start_time=datetime.time(10, 0),
                num_guests=1,
                rate_per_night=Decimal("100.00"),
                status="Confirmed",
                booking_source="Walk-in",
            )

    def test_hourly_booking_blocks_overlapping_hourly_booking(self):
        check_in = timezone.localdate() + datetime.timedelta(days=3)
        Booking.objects.create(
            room=self.room,
            guest=self.guest,
            check_in_date=check_in,
            check_out_date=check_in,
            booking_duration_type="2_hours",
            booking_start_time=datetime.time(10, 0),
            num_guests=1,
            rate_per_night=Decimal("150.00"),
            status="Confirmed",
            booking_source="Walk-in",
        )
        other_guest = make_guest("Late", "Guest", "0810000004")
        with self.assertRaises(ValidationError):
            Booking.objects.create(
                room=self.room,
                guest=other_guest,
                check_in_date=check_in,
                check_out_date=check_in,
                booking_duration_type="1_hour",
                booking_start_time=datetime.time(11, 0),
                num_guests=1,
                rate_per_night=Decimal("100.00"),
                status="Confirmed",
                booking_source="Walk-in",
            )

    def test_partial_payment_recalculates_balance(self):
        booking = make_booking(self.room, self.guest, days_ahead=6, nights=2)
        Payment.objects.create(
            booking=booking,
            amount=Decimal("250.00"),
            payment_method="EFT",
            payment_type="Payment",
        )
        booking.refresh_from_db()
        self.assertEqual(booking.balance_due, booking.total_amount - Decimal("250.00"))


class SearchViewTest(CircleCoreTenantTestCase):
    def setUp(self):
        self.owner = make_owner()
        activate_trial(self.owner)
        self.client.login(username="owner", password="testpass123")

    def test_search_empty_query(self):
        response = self.client.get(reverse("core:search"), {"q": ""})
        self.assertEqual(response.status_code, 200)

    def test_search_returns_results(self):
        guest = make_guest("Alice", "Wonder")
        response = self.client.get(reverse("core:search"), {"q": "Alice"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Alice")


class POSViewTest(CircleCoreTenantTestCase):
    def setUp(self):
        self.owner = make_owner()
        activate_trial(self.owner)
        self.client.login(username="owner", password="testpass123")

    def test_pos_terminal_loads(self):
        response = self.client.get(reverse("core:pos_terminal"))
        self.assertEqual(response.status_code, 200)

    def test_pos_sales_list_loads(self):
        response = self.client.get(reverse("core:pos_sales_list"))
        self.assertEqual(response.status_code, 200)

    def test_pos_items_list_loads(self):
        response = self.client.get(reverse("core:pos_items_list"))
        self.assertEqual(response.status_code, 200)

    def test_pos_shifts_loads(self):
        response = self.client.get(reverse("core:pos_shifts"))
        self.assertEqual(response.status_code, 200)
