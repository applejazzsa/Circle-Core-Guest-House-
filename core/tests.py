"""
Basic test suite for Circle Core Guest House.
Run with: python manage.py test core
"""

import datetime
import json
import uuid
from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import schema_context, tenant_context

from tenants.models import Domain, GuestHouseTenant

from .models import (
    Booking,
    Guest,
    GuestHouseSettings,
    Payment,
    Property,
    Room,
    OfflineConflict,
    OfflineDevice,
    OfflineOperation,
    Subscription,
    SubscriptionPlan,
    StaffProfile,
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
    GuestHouseSettings.objects.update_or_create(
        pk=1,
        defaults={"guest_house_name": "Test House", "onboarding_complete": True},
    )


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


class PhonePinAuthTest(CircleCoreTenantTestCase):
    def setUp(self):
        self.owner = make_owner()
        activate_trial(self.owner)
        self.staff = User.objects.create_user(
            username="reception1",
            email="reception@example.com",
            password="staff-password-123",
        )
        self.staff.groups.add(Group.objects.get_or_create(name="Reception")[0])
        self.profile, _ = StaffProfile.objects.get_or_create(user=self.staff)
        self.profile.phone_number = StaffProfile.normalize_phone("082 123 4567")
        self.profile.role = "Reception"
        self.profile.pin_enabled = True
        self.profile.set_pin("2468")
        self.profile.save()

    def pin_login(self, pin="2468", phone="082 123 4567"):
        return self.client.post(
            reverse("login"),
            {"login_method": "pin", "phone_number": phone, "pin": pin},
        )

    def test_valid_phone_pin_login(self):
        response = self.pin_login()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("core:home"))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.staff.pk)

    def test_pin_login_does_not_bypass_reception_permissions(self):
        self.pin_login()
        response = self.client.get(reverse("core:payment_list"))
        self.assertRedirects(response, reverse("core:home"), fetch_redirect_response=False)

    def test_staff_cannot_change_own_role(self):
        self.pin_login()
        response = self.client.get(reverse("core:staff_edit", args=[self.staff.pk]))
        self.assertRedirects(response, reverse("core:home"), fetch_redirect_response=False)

    def test_wrong_pin_uses_generic_error_and_tracks_failure(self):
        response = self.pin_login("1111")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Unable to sign in with those details.")
        self.assertNotIn("_auth_user_id", self.client.session)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.pin_failed_attempts, 1)

    def test_pin_login_locks_after_five_failed_attempts(self):
        for _ in range(5):
            self.pin_login("1111")
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.pin_failed_attempts, 5)
        self.assertGreater(self.profile.pin_locked_until, timezone.now())

    def test_successful_pin_login_clears_failed_attempts(self):
        self.profile.pin_failed_attempts = 3
        self.profile.save(update_fields=["pin_failed_attempts"])
        response = self.pin_login("2468")
        self.assertEqual(response.status_code, 302)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.pin_failed_attempts, 0)
        self.assertIsNone(self.profile.pin_locked_until)

    def test_locked_user_cannot_login_with_correct_pin(self):
        self.profile.pin_failed_attempts = 5
        self.profile.pin_locked_until = timezone.now() + datetime.timedelta(minutes=15)
        self.profile.save()
        response = self.pin_login("2468")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_pin_disabled_user_cannot_login(self):
        self.profile.pin_enabled = False
        self.profile.save(update_fields=["pin_enabled"])
        response = self.pin_login()
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_owner_can_reset_and_disable_staff_pin(self):
        self.client.login(username="owner", password="testpass123")
        edit_url = reverse("core:staff_edit", args=[self.staff.pk])
        self.assertEqual(self.client.get(edit_url).status_code, 200)
        response = self.client.post(
            edit_url,
            {
                "first_name": "Front",
                "last_name": "Desk",
                "email": self.staff.email,
                "role": "Reception",
                "is_active": "on",
                "phone_number": "082 123 4567",
                "pin_enabled": "on",
                "pin": "9876",
            },
        )
        self.assertRedirects(response, reverse("core:staff_list"))
        self.profile.refresh_from_db()
        self.assertTrue(self.profile.pin_enabled)
        self.assertTrue(self.profile.check_pin("9876"))
        self.assertFalse(self.profile.check_pin("2468"))

        response = self.client.post(
            edit_url,
            {
                "first_name": "Front",
                "last_name": "Desk",
                "email": self.staff.email,
                "role": "Reception",
                "is_active": "on",
                "phone_number": "082 123 4567",
            },
        )
        self.assertRedirects(response, reverse("core:staff_list"))
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.pin_enabled)
        self.assertEqual(self.profile.pin_hash, "")

    def test_email_password_login_still_works_with_email(self):
        response = self.client.post(
            reverse("login"),
            {
                "login_method": "email",
                "username": self.owner.email,
                "password": "testpass123",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.owner.pk)

    def test_other_tenant_phone_is_not_visible(self):
        with schema_context("public"):
            other_tenant = GuestHouseTenant(
                schema_name="pin_other",
                name="Other Guest House",
                owner_name="Other Owner",
                owner_email="pin-other@example.com",
                owner_phone="0830000000",
                is_active=True,
                is_verified=True,
            )
            other_tenant.save()
            Domain.objects.create(domain="pin-other.test.com", tenant=other_tenant, is_primary=True)
        try:
            with tenant_context(other_tenant):
                other_user = User.objects.create_user(username="otherstaff", password="password-123")
                other_profile, _ = StaffProfile.objects.get_or_create(user=other_user)
                other_profile.phone_number = StaffProfile.normalize_phone("083 555 0199")
                other_profile.pin_enabled = True
                other_profile.set_pin("1357")
                other_profile.save()

            response = self.pin_login(pin="1357", phone="083 555 0199")
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("_auth_user_id", self.client.session)
        finally:
            with schema_context("public"):
                other_tenant.delete(allow_hard_delete=True)


class OfflineSyncTest(CircleCoreTenantTestCase):
    def setUp(self):
        self.owner = make_owner()
        activate_trial(self.owner)
        self.client.login(username="owner", password="testpass123")
        self.room = make_room(price=500)
        self.device_id = str(uuid.uuid4())
        self.client.post(
            reverse("core:offline_enroll"),
            data=json.dumps({"client_id": self.device_id, "label": "Reception"}),
            content_type="application/json",
        )
        bootstrap = self.client.get(reverse("core:offline_bootstrap"), {"device_id": self.device_id})
        self.bootstrap = bootstrap.json()

    def operation(self, operation_id=None, created_at=None):
        return {
            "id": operation_id or str(uuid.uuid4()),
            "type": "walk_in",
            "created_at": created_at or timezone.now().isoformat(),
            "payload": {"room_id": self.room.pk, "duration": "daily", "rate": "500.00", "num_guests": 1, "identity_mode": "walk_in"},
        }

    def sync(self, operations, lease=None, device_id=None):
        return self.client.post(
            reverse("core:offline_sync"),
            data=json.dumps({"device_id": device_id or self.device_id, "lease": lease or self.bootstrap["lease"], "operations": operations}),
            content_type="application/json",
        )

    def test_duplicate_sync_is_idempotent(self):
        operation = self.operation()
        first = self.sync([operation])
        second = self.sync([operation])
        self.assertEqual(first.json()["results"][0]["status"], "applied")
        self.assertEqual(second.json()["results"][0]["status"], "applied")
        self.assertEqual(Booking.objects.filter(room=self.room).count(), 1)
        self.assertEqual(OfflineOperation.objects.count(), 1)

    def test_room_collision_creates_owner_conflict(self):
        self.room.status = "Occupied"
        self.room.save(update_fields=["status"])
        response = self.sync([self.operation()])
        self.assertEqual(response.json()["results"][0]["status"], "conflict")
        self.assertEqual(OfflineConflict.objects.filter(is_resolved=False).count(), 1)
        self.assertEqual(Booking.objects.count(), 0)

    def test_future_device_clock_is_rejected_as_conflict(self):
        future = timezone.now() + datetime.timedelta(minutes=20)
        response = self.sync([self.operation(created_at=future.isoformat())])
        self.assertEqual(response.json()["results"][0]["status"], "conflict")
        self.assertIn("clock", response.json()["results"][0]["error"].lower())

    def test_action_after_offline_lease_expiry_is_conflict(self):
        expired_action_time = timezone.now() + datetime.timedelta(hours=73)
        response = self.sync([self.operation(created_at=expired_action_time.isoformat())])
        self.assertEqual(response.json()["results"][0]["status"], "conflict")
        self.assertIn("expired", response.json()["results"][0]["error"].lower())

    def test_revoked_device_cannot_sync(self):
        device = OfflineDevice.objects.get(client_id=self.device_id)
        device.is_active = False
        device.revoked_at = timezone.now()
        device.save(update_fields=["is_active", "revoked_at"])
        response = self.sync([self.operation()])
        self.assertEqual(response.status_code, 403)

    def test_operational_actions_sync_in_order(self):
        guest = make_guest("Offline", "Guest", "0840000001")
        booking = Booking.objects.create(
            room=self.room, guest=guest, check_in_date=timezone.localdate(),
            check_out_date=timezone.localdate() + datetime.timedelta(days=1),
            booking_duration_type="daily", num_guests=1, rate_per_night=Decimal("500.00"),
            status="Checked In", booking_source="Walk-in", check_in_time=timezone.now(),
        )
        self.room.status = "Occupied"
        self.room.save(update_fields=["status"])
        now = timezone.now().isoformat()
        operations = [
            {"id": str(uuid.uuid4()), "type": "cash_payment", "created_at": now, "payload": {"booking_id": booking.pk, "amount": "200.00"}},
            {"id": str(uuid.uuid4()), "type": "check_out", "created_at": now, "payload": {"booking_id": booking.pk}},
            {"id": str(uuid.uuid4()), "type": "cleaning", "created_at": now, "payload": {"room_id": self.room.pk, "status": "Clean"}},
            {"id": str(uuid.uuid4()), "type": "maintenance", "created_at": now, "payload": {"room_id": self.room.pk, "title": "Loose tap", "description": "Bathroom tap", "category": "plumbing", "priority": "medium"}},
        ]
        response = self.sync(operations)
        self.assertEqual([row["status"] for row in response.json()["results"]], ["applied"] * 4)
        booking.refresh_from_db(); self.room.refresh_from_db()
        self.assertEqual(booking.status, "Checked Out")
        self.assertEqual(self.room.status, "Available")
        self.assertEqual(Payment.objects.filter(booking=booking, payment_method="Cash").count(), 1)
        self.assertEqual(self.room.maintenance_requests.filter(title="Loose tap").count(), 1)

    def test_second_device_assigning_same_room_gets_conflict(self):
        second_id = str(uuid.uuid4())
        second = OfflineDevice.objects.create(client_id=second_id, prop=self.room.prop, user=self.owner, label="Second device", is_active=True, approved_by=self.owner)
        second_bootstrap = self.client.get(reverse("core:offline_bootstrap"), {"device_id": second_id}).json()
        first_operation = self.operation()
        self.assertEqual(self.sync([first_operation]).json()["results"][0]["status"], "applied")
        second_operation = self.operation()
        response = self.sync([second_operation], lease=second_bootstrap["lease"], device_id=second_id)
        self.assertEqual(response.json()["results"][0]["status"], "conflict")
        self.assertEqual(Booking.objects.filter(room=self.room).count(), 1)


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

    def test_booking_add_has_one_guest_field_and_one_submit_action(self):
        response = self.client.get(reverse("core:booking_add"))
        content = response.content.decode()
        self.assertEqual(content.count('id="id_guest"'), 1)
        self.assertEqual(content.count('id="form-submit-btn"'), 1)
        self.assertNotIn('id="book-now-btn"', content)
        self.assertIn('id="booking-form"', content)

    def test_number_plate_booking_uses_walk_in_guest_without_guest_selection(self):
        check_in = timezone.localdate() + datetime.timedelta(days=1)
        response = self.client.post(
            reverse("core:booking_add"),
            {
                "identity_mode": "plate",
                "guest": "",
                "room": self.room.pk,
                "check_in_date": check_in.isoformat(),
                "check_out_date": (check_in + datetime.timedelta(days=1)).isoformat(),
                "num_guests": 1,
                "booking_duration_type": "daily",
                "booking_start_time": "",
                "booking_end_time": "",
                "rate_per_night": self.room.price_per_night,
                "discount": "0.00",
                "deposit_required": "0.00",
                "booking_source": "Walk-in",
                "status": "Confirmed",
                "vehicle_registration": "ysr 142 gp",
                "notes": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        booking = Booking.objects.get(room=self.room)
        self.assertTrue(booking.guest.is_generic)
        self.assertEqual(booking.vehicle_registration, "YSR 142 GP")

    def test_number_plate_mode_requires_a_plate(self):
        check_in = timezone.localdate() + datetime.timedelta(days=1)
        response = self.client.post(
            reverse("core:booking_add"),
            {
                "identity_mode": "plate",
                "guest": "",
                "room": self.room.pk,
                "check_in_date": check_in.isoformat(),
                "check_out_date": (check_in + datetime.timedelta(days=1)).isoformat(),
                "num_guests": 1,
                "booking_duration_type": "daily",
                "rate_per_night": self.room.price_per_night,
                "discount": "0.00",
                "deposit_required": "0.00",
                "booking_source": "Walk-in",
                "status": "Confirmed",
                "vehicle_registration": "",
                "notes": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter the vehicle number plate.")
        self.assertContains(response, 'value="plate"', html=False)
        self.assertFalse(Booking.objects.filter(room=self.room).exists())

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
