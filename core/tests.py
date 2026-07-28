"""
Basic test suite for Circle Core Guest House.
Run with: python manage.py test core
"""

import base64
import datetime
import json
import re
import threading
import uuid
import zlib
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.utils import IntegrityError
from django.test import Client, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django_tenants.test.cases import TenantTestCase
from django_tenants.utils import (
    get_public_schema_name,
    get_tenant_domain_model,
    get_tenant_model,
    schema_context,
    tenant_context,
)

from tenants.models import Domain, GuestHouseTenant

from .availability import check_availability, occupancy_snapshot
from .booking_transactions import (
    cancel_multi_room_booking,
    check_in_multi_room_booking,
    check_out_multi_room_booking,
    create_individual_shared_room_booking,
    create_multi_room_booking,
    edit_multi_room_booking,
    reinstate_multi_room_booking,
)
from .forms import RoomForm
from .models import (
    AuditLog,
    Booking,
    Guest,
    GuestHouseSettings,
    Payment,
    Property,
    RatePlan,
    Room,
    RoomAllocation,
    RoomType,
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


class ConcurrencyTenantTestCase(TransactionTestCase):
    """
    Like CircleCoreTenantTestCase, but built on TransactionTestCase instead of
    TestCase. TestCase wraps every test method's writes in a savepoint that
    is rolled back at the end — never actually committed to Postgres — which
    makes them invisible to any other database connection. That's fine for
    ordinary tests, but genuine cross-thread concurrency testing needs a
    second thread's own connection to actually see the first thread's
    committed fixture data and locks. TransactionTestCase commits for real
    and resets via truncation instead, which is what real concurrency tests
    require here.
    """

    schema_name = "concurrency_test"
    domain_name = "concurrency-test.test.com"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command("migrate_schemas", schema_name=get_public_schema_name(), interactive=False, verbosity=0)
        if cls.domain_name not in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS = list(settings.ALLOWED_HOSTS) + [cls.domain_name]

        tenant_model = get_tenant_model()
        domain_model = get_tenant_domain_model()
        cls.tenant, _ = tenant_model.objects.get_or_create(
            schema_name=cls.schema_name,
            defaults=dict(
                name="Concurrency Test House",
                owner_name="Concurrency Owner",
                owner_email=f"{cls.schema_name}@example.com",
                owner_phone="0810000099",
                is_active=True,
                is_verified=True,
            ),
        )
        domain_model.objects.get_or_create(
            domain=cls.domain_name, tenant=cls.tenant, defaults={"is_primary": True}
        )
        connection.set_tenant(cls.tenant)

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        if cls.domain_name in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS.remove(cls.domain_name)
        super().tearDownClass()

    def _fixture_teardown(self):
        # Truncate this tenant schema's own tables between tests, but stay
        # inside the tenant schema — the default implementation would flush
        # relative to whatever schema is active, which is exactly this one
        # since setUpClass already switched the connection to it.
        super()._fixture_teardown()


# ── Helpers ───────────────────────────────────────────────────────────────────


def make_owner(username="owner", password="testpass123"):
    user = User.objects.create_superuser(username, f"{username}@example.com", password)
    return user


def make_other_property(name):
    """
    A second, genuinely distinct Property in the current schema. Property(pk=1)
    is auto-seeded via an explicit-pk get_or_create (core/migrations/0021), which
    leaves the id sequence behind it — a plain .create() can collide with that
    seed row. Using an explicit next-pk sidesteps the sequence gap entirely.
    """
    last = Property.objects.order_by("-pk").first()
    next_pk = (last.pk if last else 0) + 1
    return Property.objects.create(pk=next_pk, name=name, is_active=True)


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


def make_shared_room(name="Shared Room", price=180, max_guests=6):
    room = make_room(name, price=price)
    room.max_guests = max_guests
    room.booking_mode = "SHARED_CAPACITY"
    room.pricing_model = "per_person"
    room.save()
    return room


def enable_shared_capacity():
    settings_obj, _ = GuestHouseSettings.objects.get_or_create(pk=1)
    settings_obj.shared_capacity_booking_enabled = True
    settings_obj.save()
    return settings_obj


def extract_reportlab_pdf_text(pdf_bytes):
    """
    Decode every ASCII85+Flate content stream in a ReportLab-generated PDF
    and return the concatenated raw decoded bytes. ReportLab compresses its
    content streams, so drawn text is not searchable in the raw response
    body at all — a naive `marker in response.content` check would silently
    pass whether or not a leak actually occurred. Both filters used here are
    Python standard library (base64, zlib) — no new dependency required.
    """
    decoded = b""
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", pdf_bytes, re.DOTALL):
        raw = match.group(1).strip(b"\r\n")
        try:
            decoded += zlib.decompress(base64.a85decode(raw, adobe=True))
        except (zlib.error, ValueError):
            continue
    return decoded


def make_reserving_booking(room, guest, check_in, check_out, num_guests=1, status="Confirmed"):
    """A booking that reserves inventory on `room`. For SHARED_CAPACITY rooms,
    also creates the RoomAllocation the availability service actually reads."""
    booking = Booking.objects.create(
        room=room,
        guest=guest,
        check_in_date=check_in,
        check_out_date=check_out,
        booking_duration_type="daily",
        num_guests=num_guests,
        rate_per_night=room.price_per_night,
        status=status,
        booking_source="Walk-in",
    )
    if room.effective_booking_mode == "SHARED_CAPACITY":
        nights = max((check_out - check_in).days, 0)
        RoomAllocation.objects.create(
            booking=booking,
            room=room,
            allocated_guests=num_guests,
            rate_per_night=room.price_per_night,
            line_total=room.price_per_night * num_guests * nights,
        )
    return booking


def run_concurrently(schema_name, funcs):
    """
    Run each zero-arg callable in funcs on its own thread with its own DB
    connection, synchronized with a Barrier so they all reach their
    transaction at (as close as Python threading allows to) the same moment —
    a genuine race for the same locked row(s), not just sequential execution
    that happens to look like a race. Returns a list of (result, exception)
    tuples in the same order as funcs.
    """
    barrier = threading.Barrier(len(funcs))
    results = [None] * len(funcs)

    def run(index, func):
        from django.db import connection as thread_connection
        try:
            with schema_context(schema_name):
                barrier.wait(timeout=10)
                results[index] = (func(), None)
        except Exception as exc:
            results[index] = (None, exc)
        finally:
            thread_connection.close()

    threads = [threading.Thread(target=run, args=(i, f)) for i, f in enumerate(funcs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    return results


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


class SharedCapacityFeatureFlagTest(CircleCoreTenantTestCase):
    def test_new_tenant_defaults_to_feature_disabled(self):
        # A freshly created settings row simulates a brand-new tenant that has
        # never touched this setting.
        settings_obj = GuestHouseSettings.objects.create()
        self.assertFalse(settings_obj.shared_capacity_booking_enabled)

    def test_existing_tenant_defaults_to_feature_disabled(self):
        # This test's own tenant schema was migrated from scratch, like every
        # pre-existing tenant that has never opted in.
        settings_obj, _ = GuestHouseSettings.objects.get_or_create(pk=1)
        self.assertFalse(settings_obj.shared_capacity_booking_enabled)

    def test_existing_room_defaults_to_whole_room(self):
        room = make_room()
        self.assertEqual(room.booking_mode, "WHOLE_ROOM")

    def test_whole_room_effective_when_feature_disabled(self):
        settings_obj, _ = GuestHouseSettings.objects.get_or_create(pk=1)
        settings_obj.shared_capacity_booking_enabled = False
        settings_obj.save()

        room = make_room()
        room.booking_mode = "SHARED_CAPACITY"
        room.save()

        self.assertEqual(room.effective_booking_mode, "WHOLE_ROOM")

    def test_shared_capacity_effective_only_when_tenant_feature_enabled(self):
        room = make_room()
        room.booking_mode = "SHARED_CAPACITY"
        room.save()

        settings_obj, _ = GuestHouseSettings.objects.get_or_create(pk=1)
        settings_obj.shared_capacity_booking_enabled = False
        settings_obj.save()
        self.assertEqual(room.effective_booking_mode, "WHOLE_ROOM")

        settings_obj.shared_capacity_booking_enabled = True
        settings_obj.save()
        self.assertEqual(room.effective_booking_mode, "SHARED_CAPACITY")

    def test_tenant_settings_do_not_affect_other_tenant(self):
        settings_obj, _ = GuestHouseSettings.objects.get_or_create(pk=1)
        settings_obj.shared_capacity_booking_enabled = True
        settings_obj.save()

        with schema_context("public"):
            other_tenant = GuestHouseTenant(
                schema_name="shared_cap_other",
                name="Other Guest House",
                owner_name="Other Owner",
                owner_email="shared-cap-other@example.com",
                owner_phone="0830000001",
                is_active=True,
                is_verified=True,
            )
            other_tenant.save()
            Domain.objects.create(domain="shared-cap-other.test.com", tenant=other_tenant, is_primary=True)
        try:
            with tenant_context(other_tenant):
                other_settings, _ = GuestHouseSettings.objects.get_or_create(pk=1)
                self.assertFalse(other_settings.shared_capacity_booking_enabled)
        finally:
            with schema_context("public"):
                other_tenant.delete(allow_hard_delete=True)

    def test_migration_0041_is_reversible(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        def room_columns():
            with connection.cursor() as cursor:
                return [c.name for c in connection.introspection.get_table_description(cursor, "core_room")]

        self.assertIn("booking_mode", room_columns())
        try:
            MigrationExecutor(connection).migrate([("core", "0040_room_type_and_rate_plan_models")])
            self.assertNotIn("booking_mode", room_columns())
        finally:
            # Always leave the schema fully migrated again, regardless of outcome,
            # so later tests in this run never see a partially-migrated schema.
            MigrationExecutor(connection).migrate([("core", "0041_shared_capacity_feature_flag")])
        self.assertIn("booking_mode", room_columns())


class RoomFormTest(CircleCoreTenantTestCase):
    def room_data(self, name="Room 2"):
        return {
            "name": name,
            "room_type": "Double",
            "pricing_model": "per_room",
            "price_per_night": "500.00",
            "booking_types_allowed": ["Daily"],
            "max_guests": 2,
            "status": "Available",
            "cleaning_status": "Clean",
            "description": "",
            "internal_notes": "",
        }

    def test_same_name_is_allowed_at_a_different_property(self):
        first_property = Property.objects.create(pk=91001, name="First Property")
        second_property = Property.objects.create(pk=91002, name="Second Property")
        Room.objects.create(
            prop=first_property,
            name="Room 2",
            room_type="Double",
            price_per_night=Decimal("500.00"),
        )

        form = RoomForm(data=self.room_data(), prop=second_property)

        self.assertTrue(form.is_valid(), form.errors)

    def test_same_name_is_rejected_at_the_same_property(self):
        prop = Property.objects.create(pk=91003, name="Test Property")
        Room.objects.create(
            prop=prop,
            name="Room 2",
            room_type="Double",
            price_per_night=Decimal("500.00"),
        )

        form = RoomForm(data=self.room_data(name=" room 2 "), prop=prop)

        self.assertFalse(form.is_valid())
        self.assertIn("name", form.errors)


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


class RoomAllocationModelTest(CircleCoreTenantTestCase):
    def test_one_booking_with_one_allocation(self):
        room = make_room("Alloc Room 1", price=200)
        guest = make_guest(phone="0810001111")
        booking = make_booking(room, guest, nights=3)
        allocation = RoomAllocation.objects.create(
            booking=booking,
            room=room,
            allocated_guests=2,
            rate_per_night=Decimal("200.00"),
            line_total=Decimal("1200.00"),
        )
        self.assertEqual(booking.room_allocations.count(), 1)
        self.assertEqual(allocation.room, room)

    def test_one_booking_with_multiple_allocations(self):
        room_a = make_room("Alloc Room A", price=180)
        room_b = make_room("Alloc Room B", price=260)
        guest = make_guest(phone="0810002222")
        booking = make_booking(room_a, guest, nights=2)
        RoomAllocation.objects.create(
            booking=booking, room=room_a, allocated_guests=8,
            rate_per_night=Decimal("180.00"), line_total=Decimal("2880.00"),
        )
        RoomAllocation.objects.create(
            booking=booking, room=room_b, allocated_guests=6,
            rate_per_night=Decimal("260.00"), line_total=Decimal("3120.00"),
        )
        self.assertEqual(booking.room_allocations.count(), 2)
        combined = sum((a.line_total for a in booking.room_allocations.all()), Decimal("0.00"))
        self.assertEqual(combined, Decimal("6000.00"))

    def test_tenant_mismatch_rejection_cross_property(self):
        # Deterministic, same-schema analogue of "tenant mismatch": a room from
        # a different Property must be rejected. Property is the only
        # tenant-scoped boundary reachable from both Room and Booking within a
        # single schema — this app has no shared tenant_id column (see the
        # RoomAllocation model docstring for why).
        other_prop = make_other_property("Other Property")
        other_room = Room.objects.create(
            prop=other_prop, name="Other Prop Room", room_type="Double",
            max_guests=2, status="Available", cleaning_status="Clean",
            price_per_night=Decimal("300.00"),
        )
        room = make_room("Alloc Room Home", price=200)
        guest = make_guest(phone="0810003333")
        booking = make_booking(room, guest)
        allocation = RoomAllocation(
            booking=booking, room=other_room, allocated_guests=2,
            rate_per_night=Decimal("300.00"), line_total=Decimal("600.00"),
        )
        with self.assertRaises(ValidationError):
            allocation.save()

    def test_tenant_mismatch_rejection_cross_schema(self):
        # Strongest-level proof: a Room that only exists in a *different
        # tenant's schema* can never be attached here — enforced by the
        # database foreign key itself, not application code.
        room = make_room("Alloc Room Cross Schema", price=200)
        guest = make_guest(phone="0810004444")
        booking = make_booking(room, guest)

        with schema_context("public"):
            other_tenant = GuestHouseTenant(
                schema_name="alloc_other",
                name="Other Guest House",
                owner_name="Other Owner",
                owner_email="alloc-other@example.com",
                owner_phone="0830000002",
                is_active=True,
                is_verified=True,
            )
            other_tenant.save()
            Domain.objects.create(domain="alloc-other.test.com", tenant=other_tenant, is_primary=True)
        try:
            with tenant_context(other_tenant):
                other_prop = make_other_property("Other Tenant Property")
                other_room = Room.objects.create(
                    prop=other_prop, name="Other Tenant Room", room_type="Double",
                    max_guests=2, status="Available", cleaning_status="Clean",
                    price_per_night=Decimal("400.00"),
                )
                other_room_id = other_room.pk

            # The room id from the other tenant's schema simply doesn't exist
            # back in this schema — RoomAllocation.clean() fails trying to even
            # load it (Room.DoesNotExist) before the database's own foreign key
            # constraint would get a chance to reject it (IntegrityError). Both
            # outcomes prove the same thing: it is never attachable.
            with self.assertRaises((IntegrityError, Room.DoesNotExist)):
                with transaction.atomic():
                    RoomAllocation.objects.create(
                        booking=booking, room_id=other_room_id, allocated_guests=1,
                        rate_per_night=Decimal("400.00"), line_total=Decimal("400.00"),
                    )
        finally:
            with schema_context("public"):
                other_tenant.delete(allow_hard_delete=True)

    def test_zero_guests_rejected(self):
        room = make_room("Alloc Room Zero", price=200)
        guest = make_guest(phone="0810005555")
        booking = make_booking(room, guest)
        allocation = RoomAllocation(
            booking=booking, room=room, allocated_guests=0,
            rate_per_night=Decimal("200.00"), line_total=Decimal("0.00"),
        )
        with self.assertRaises(ValidationError):
            allocation.save()

    def test_negative_guests_rejected(self):
        room = make_room("Alloc Room Negative", price=200)
        guest = make_guest(phone="0810006666")
        booking = make_booking(room, guest)
        allocation = RoomAllocation(
            booking=booking, room=room, allocated_guests=-1,
            rate_per_night=Decimal("200.00"), line_total=Decimal("0.00"),
        )
        with self.assertRaises(Exception):
            allocation.save()

    def test_duplicate_allocation_same_booking_and_room_is_prevented(self):
        room = make_room("Alloc Room Dup", price=200)
        guest = make_guest(phone="0810007777")
        booking = make_booking(room, guest)
        RoomAllocation.objects.create(
            booking=booking, room=room, allocated_guests=2,
            rate_per_night=Decimal("200.00"), line_total=Decimal("400.00"),
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RoomAllocation.objects.create(
                    booking=booking, room=room, allocated_guests=3,
                    rate_per_night=Decimal("200.00"), line_total=Decimal("600.00"),
                )

    def test_historical_rate_snapshot_survives_rate_plan_change(self):
        rate_plan = RatePlan.objects.create(
            name="Snapshot Test Plan", amount=Decimal("180.00"), currency="ZAR",
            pricing_basis="per_person_per_night",
        )
        room = make_room("Alloc Room Snapshot", price=180)
        guest = make_guest(phone="0810008888")
        booking = make_booking(room, guest, nights=2)
        allocation = RoomAllocation.objects.create(
            booking=booking, room=room, allocated_guests=4, rate_plan=rate_plan,
            rate_per_night=Decimal("180.00"), line_total=Decimal("1440.00"),
        )
        # The rate plan's live amount changes later...
        rate_plan.amount = Decimal("250.00")
        rate_plan.save()

        allocation.refresh_from_db()
        # ...but the historical snapshot on the allocation must not move.
        self.assertEqual(allocation.rate_per_night, Decimal("180.00"))
        self.assertEqual(allocation.line_total, Decimal("1440.00"))

    def test_existing_single_room_booking_remains_readable_and_editable(self):
        room = make_room("Alloc Room Legacy", price=500)
        guest = make_guest(phone="0810009999")
        booking = make_booking(room, guest, nights=2)
        # No RoomAllocation row is required for this legacy path.
        self.assertEqual(booking.room_allocations.count(), 0)
        self.assertEqual(booking.room, room)
        self.assertEqual(booking.total_amount, Decimal("1000.00"))
        # Still fully editable via the normal model API.
        booking.status = "Checked In"
        booking.save()
        booking.refresh_from_db()
        self.assertEqual(booking.status, "Checked In")

    def test_migration_0043_backfill_is_reversible_and_idempotent(self):
        room = make_room("Alloc Room Backfill", price=350)
        guest = make_guest(phone="0810000001")
        booking = make_booking(room, guest, nights=2)
        self.assertEqual(booking.room_allocations.count(), 0)

        # The test schema was already fully migrated (through 0043) before this
        # booking existed, so 0043 has nothing pending yet. Unapply it first —
        # a safe no-op deletion, since nothing has been backfilled so far —
        # then reapply, which is what actually exercises the backfill against
        # this freshly created, still-unbacked booking.
        executor = MigrationExecutor(connection)
        executor.migrate([("core", "0042_room_allocation")])
        executor.loader.build_graph()

        executor = MigrationExecutor(connection)
        executor.migrate([("core", "0043_backfill_room_allocations")])
        executor.loader.build_graph()

        booking.refresh_from_db()
        self.assertEqual(booking.room_allocations.count(), 1)
        allocation = booking.room_allocations.first()
        self.assertEqual(allocation.line_total, booking.total_amount)
        self.assertEqual(allocation.allocated_guests, booking.num_guests)

        # Idempotent: re-targeting the same migration does nothing further.
        executor = MigrationExecutor(connection)
        executor.migrate([("core", "0043_backfill_room_allocations")])
        executor.loader.build_graph()
        self.assertEqual(booking.room_allocations.count(), 1)

        try:
            executor = MigrationExecutor(connection)
            executor.migrate([("core", "0042_room_allocation")])
            executor.loader.build_graph()
            self.assertEqual(RoomAllocation.objects.filter(booking=booking).count(), 0)
            # The booking itself is untouched by the rollback.
            booking.refresh_from_db()
            self.assertEqual(booking.total_amount, Decimal("700.00"))
        finally:
            # Always leave the schema fully migrated again, regardless of
            # outcome, so later tests in this run never see a partially
            # migrated schema.
            executor = MigrationExecutor(connection)
            executor.migrate([("core", "0043_backfill_room_allocations")])
            executor.loader.build_graph()


class AvailabilityServiceTest(CircleCoreTenantTestCase):
    def setUp(self):
        self.today = timezone.localdate()

    def d(self, offset):
        return self.today + datetime.timedelta(days=offset)

    def test_whole_room_no_overlap(self):
        room = make_room("WR No Overlap", price=500)
        result = check_availability(room, self.d(10), self.d(12), 1)
        self.assertTrue(result.available)
        self.assertEqual(result.effective_mode, "WHOLE_ROOM")
        self.assertEqual(result.remaining_capacity, room.max_guests)
        self.assertEqual(result.conflicting_allocations, [])

    def test_whole_room_with_overlap(self):
        room = make_room("WR Overlap", price=500)
        guest = make_guest(phone="0811110001")
        make_reserving_booking(room, guest, self.d(10), self.d(13), num_guests=1)

        result = check_availability(room, self.d(10), self.d(13), 1)
        self.assertFalse(result.available)
        self.assertEqual(result.remaining_capacity, 0)
        self.assertEqual(len(result.conflicting_allocations), 1)
        self.assertIn("already booked", result.reason)

    def test_shared_capacity_partial_occupancy(self):
        enable_shared_capacity()
        room = make_shared_room("Shared Partial", price=180, max_guests=6)
        guest = make_guest(phone="0811110002")
        make_reserving_booking(room, guest, self.d(20), self.d(22), num_guests=2)

        result = check_availability(room, self.d(20), self.d(22), 3)
        self.assertTrue(result.available)
        self.assertEqual(result.effective_mode, "SHARED_CAPACITY")
        self.assertEqual(result.occupied_capacity, 2)
        self.assertEqual(result.remaining_capacity, 4)

    def test_shared_capacity_becomes_exactly_full(self):
        enable_shared_capacity()
        room = make_shared_room("Shared Exact Full", price=180, max_guests=6)
        guest = make_guest(phone="0811110003")
        make_reserving_booking(room, guest, self.d(30), self.d(32), num_guests=4)

        result = check_availability(room, self.d(30), self.d(32), 2)
        self.assertTrue(result.available)
        self.assertEqual(result.remaining_capacity, 2)

    def test_shared_capacity_exceeding_is_rejected(self):
        enable_shared_capacity()
        room = make_shared_room("Shared Exceeding", price=180, max_guests=6)
        guest = make_guest(phone="0811110004")
        make_reserving_booking(room, guest, self.d(40), self.d(42), num_guests=4)

        result = check_availability(room, self.d(40), self.d(42), 3)
        self.assertFalse(result.available)
        self.assertEqual(result.remaining_capacity, 2)
        self.assertIn("only has 2 of 3", result.reason)

    def test_shared_capacity_several_overlapping_bookings(self):
        enable_shared_capacity()
        room = make_shared_room("Shared Several", price=180, max_guests=10)
        guest1 = make_guest(first_name="G1", phone="0811110005")
        guest2 = make_guest(first_name="G2", phone="0811110006")
        make_reserving_booking(room, guest1, self.d(50), self.d(53), num_guests=3)
        make_reserving_booking(room, guest2, self.d(51), self.d(54), num_guests=4)

        # Both bookings overlap the night at offset 51: occupied = 3 + 4 = 7, remaining = 3.
        ok = check_availability(room, self.d(51), self.d(52), 3)
        self.assertTrue(ok.available)
        self.assertEqual(ok.remaining_capacity, 3)

        blocked = check_availability(room, self.d(51), self.d(52), 4)
        self.assertFalse(blocked.available)

    def test_adjacent_checkout_checkin_allowed(self):
        room = make_room("Adjacent WR", price=400)
        guest = make_guest(phone="0811110007")
        make_reserving_booking(room, guest, self.d(60), self.d(62), num_guests=1)

        # New stay starts exactly on the prior stay's checkout date.
        result = check_availability(room, self.d(62), self.d(64), 1)
        self.assertTrue(result.available)

        enable_shared_capacity()
        shared_room = make_shared_room("Adjacent Shared", price=180, max_guests=4)
        make_reserving_booking(shared_room, guest, self.d(60), self.d(62), num_guests=4)
        shared_result = check_availability(shared_room, self.d(62), self.d(64), 4)
        self.assertTrue(shared_result.available)

    def test_multi_night_request_where_only_one_night_exceeds(self):
        enable_shared_capacity()
        room = make_shared_room("Shared One Bad Night", price=180, max_guests=6)
        guest = make_guest(phone="0811110008")
        # Occupies only the middle night of the requested 3-night window.
        make_reserving_booking(room, guest, self.d(71), self.d(72), num_guests=5)

        result = check_availability(room, self.d(70), self.d(73), 2)
        self.assertFalse(result.available)
        self.assertEqual(result.first_failing_date, self.d(71))
        self.assertEqual(result.remaining_capacity, 1)

    def test_cancelled_booking_does_not_consume_capacity(self):
        enable_shared_capacity()
        room = make_shared_room("Shared Cancelled", price=180, max_guests=6)
        guest = make_guest(phone="0811110009")
        make_reserving_booking(room, guest, self.d(80), self.d(82), num_guests=6, status="Cancelled")

        result = check_availability(room, self.d(80), self.d(82), 6)
        self.assertTrue(result.available)
        self.assertEqual(result.occupied_capacity, 0)

    def test_editing_booking_excludes_itself(self):
        room = make_room("Edit Self WR", price=400)
        guest = make_guest(phone="0811110010")
        booking = make_reserving_booking(room, guest, self.d(90), self.d(92), num_guests=1)

        result = check_availability(room, self.d(90), self.d(92), 1, exclude_booking_id=booking.pk)
        self.assertTrue(result.available)

    def test_feature_flag_disabled_forces_whole_room(self):
        # shared_capacity_booking_enabled is left at its default False.
        room = make_shared_room("Flag Off Room", price=180, max_guests=6)
        guest = make_guest(phone="0811110011")
        make_reserving_booking(room, guest, self.d(100), self.d(102), num_guests=1)

        result = check_availability(room, self.d(100), self.d(102), 1)
        self.assertEqual(result.effective_mode, "WHOLE_ROOM")
        self.assertFalse(result.available)  # blocked outright, even though 1+1 <= max_guests=6

    def test_tenant_isolation(self):
        room = make_room("Isolation Home Room", price=400)
        guest = make_guest(phone="0811110012")
        make_reserving_booking(room, guest, self.d(110), self.d(112), num_guests=1)

        with schema_context("public"):
            other_tenant = GuestHouseTenant(
                schema_name="avail_other",
                name="Other Guest House",
                owner_name="Other Owner",
                owner_email="avail-other@example.com",
                owner_phone="0830000003",
                is_active=True,
                is_verified=True,
            )
            other_tenant.save()
            Domain.objects.create(domain="avail-other.test.com", tenant=other_tenant, is_primary=True)
        try:
            with tenant_context(other_tenant):
                other_room = make_room("Isolation Other Room", price=300)
                other_guest = make_guest(phone="0811110013")
                make_reserving_booking(other_room, other_guest, self.d(110), self.d(112), num_guests=1)
                other_result = check_availability(other_room, self.d(110), self.d(112), 1)
                self.assertFalse(other_result.available)  # blocked by its own tenant's booking

            # Back in this test's own schema: unaffected by the other tenant.
            result = check_availability(room, self.d(110), self.d(112), 1)
            self.assertFalse(result.available)  # blocked by ITS OWN booking, not the other tenant's

            fresh_room = make_room("Isolation Fresh Room", price=250)
            fresh_result = check_availability(fresh_room, self.d(110), self.d(112), 1)
            self.assertTrue(fresh_result.available)
        finally:
            with schema_context("public"):
                other_tenant.delete(allow_hard_delete=True)

    def test_date_validation(self):
        room = make_room("Date Validation Room", price=300)
        missing = check_availability(room, None, None, 1)
        self.assertFalse(missing.available)
        self.assertIn("required", missing.reason)

        backwards = check_availability(room, self.d(5), self.d(2), 1)
        self.assertFalse(backwards.available)
        self.assertIn("Check-out date must be after check-in date", backwards.reason)

    def test_zero_night_or_invalid_guest_count_rejected(self):
        room = make_room("Zero Night Room", price=300)
        same_day = check_availability(room, self.d(5), self.d(5), 1)
        self.assertFalse(same_day.available)

        zero_guests = check_availability(room, self.d(5), self.d(7), 0)
        self.assertFalse(zero_guests.available)

        negative_guests = check_availability(room, self.d(5), self.d(7), -1)
        self.assertFalse(negative_guests.available)


class BookingTransactionConcurrencyTest(ConcurrencyTenantTestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.schema_name = connection.schema_name

    def d(self, offset):
        return self.today + datetime.timedelta(days=offset)

    def test_two_simultaneous_requests_for_final_shared_spaces(self):
        enable_shared_capacity()
        room = make_shared_room("Concurrency Shared", price=180, max_guests=6)
        prop = room.prop
        guest_a = make_guest(first_name="Alice", phone="0812220001")
        guest_b = make_guest(first_name="Bob", phone="0812220002")
        ci, co = self.d(200), self.d(202)

        def attempt(guest, guests):
            def _run():
                return create_multi_room_booking(
                    guest=guest, prop=prop, check_in=ci, check_out=co,
                    allocations=[{"room": room, "allocated_guests": guests}],
                    total_guests=guests, status="Confirmed",
                )
            return _run

        # Combined demand (4 + 4 = 8) exceeds the room's capacity of 6, so
        # only one of these two simultaneous attempts may succeed.
        results = run_concurrently(self.schema_name, [attempt(guest_a, 4), attempt(guest_b, 4)])

        successes = [r for r, e in results if e is None]
        failures = [e for r, e in results if e is not None]
        self.assertEqual(len(successes), 1, f"expected exactly 1 success, got results={results}")
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], ValidationError)

        with schema_context(self.schema_name):
            total_allocated = sum(
                a.allocated_guests for a in RoomAllocation.objects.filter(room=room, booking__status="Confirmed")
            )
            self.assertEqual(total_allocated, 4)  # only the winner's allocation was ever committed

    def test_only_one_request_succeeds_when_combined_demand_exceeds_capacity(self):
        enable_shared_capacity()
        room = make_shared_room("Concurrency Shared Exact", price=180, max_guests=5)
        prop = room.prop
        guest_a = make_guest(first_name="Carol", phone="0812220003")
        guest_b = make_guest(first_name="Dave", phone="0812220004")
        ci, co = self.d(210), self.d(212)

        def attempt(guest, guests):
            def _run():
                return create_multi_room_booking(
                    guest=guest, prop=prop, check_in=ci, check_out=co,
                    allocations=[{"room": room, "allocated_guests": guests}],
                    total_guests=guests, status="Confirmed",
                )
            return _run

        # 3 + 3 = 6 > capacity of 5 — still only one can fit.
        results = run_concurrently(self.schema_name, [attempt(guest_a, 3), attempt(guest_b, 3)])
        successes = [r for r, e in results if e is None]
        self.assertEqual(len(successes), 1)

    def test_two_simultaneous_whole_room_bookings(self):
        room = make_room("Concurrency Whole Room", price=500)
        prop = room.prop
        guest_a = make_guest(first_name="Erin", phone="0812220005")
        guest_b = make_guest(first_name="Frank", phone="0812220006")
        ci, co = self.d(220), self.d(222)

        def attempt(guest):
            def _run():
                return create_multi_room_booking(
                    guest=guest, prop=prop, check_in=ci, check_out=co,
                    allocations=[{"room": room, "allocated_guests": 1}],
                    total_guests=1, status="Confirmed",
                )
            return _run

        results = run_concurrently(self.schema_name, [attempt(guest_a), attempt(guest_b)])
        successes = [r for r, e in results if e is None]
        failures = [e for r, e in results if e is not None]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)

    def test_multi_room_transaction_rollback(self):
        room1 = make_shared_room("Rollback Room 1", price=180, max_guests=6)
        room2 = make_shared_room("Rollback Room 2", price=260, max_guests=3)
        enable_shared_capacity()
        prop = room1.prop
        guest = make_guest(phone="0812220007")
        ci, co = self.d(230), self.d(232)

        with self.assertRaises(ValidationError) as ctx:
            create_multi_room_booking(
                guest=guest, prop=prop, check_in=ci, check_out=co,
                allocations=[
                    {"room": room1, "allocated_guests": 5},   # fits (5 <= 6)
                    {"room": room2, "allocated_guests": 4},   # does not fit (4 > 3)
                ],
                total_guests=9, status="Confirmed",
            )
        self.assertTrue(any("Rollback Room 2" in msg or "3 of 4" in msg for msg in ctx.exception.messages))

        # All-or-nothing: room1's half must not have been committed either.
        self.assertEqual(RoomAllocation.objects.filter(room=room1).count(), 0)
        self.assertEqual(Booking.objects.filter(guest=guest).count(), 0)

    def test_booking_date_change(self):
        room = make_room("Date Change Room", price=400)
        prop = room.prop
        guest = make_guest(phone="0812220008")
        booking = create_multi_room_booking(
            guest=guest, prop=prop, check_in=self.d(240), check_out=self.d(242),
            allocations=[{"room": room, "allocated_guests": 1}], total_guests=1, status="Confirmed",
        )
        moved = edit_multi_room_booking(booking, check_in=self.d(250), check_out=self.d(253))
        self.assertEqual(moved.check_in_date, self.d(250))
        self.assertEqual(moved.check_out_date, self.d(253))
        self.assertEqual(moved.room_allocations.count(), 1)
        self.assertEqual(moved.total_amount, Decimal("400.00") * 3)

    def test_room_move(self):
        room_a = make_room("Move From Room", price=400)
        room_b = make_room("Move To Room", price=550)
        prop = room_a.prop
        guest = make_guest(phone="0812220009")
        booking = create_multi_room_booking(
            guest=guest, prop=prop, check_in=self.d(260), check_out=self.d(262),
            allocations=[{"room": room_a, "allocated_guests": 1}], total_guests=1, status="Confirmed",
        )
        moved = edit_multi_room_booking(
            booking, allocations=[{"room": room_b, "allocated_guests": 1}], total_guests=1,
        )
        self.assertEqual(moved.room_id, room_b.pk)
        allocation = moved.room_allocations.get()
        self.assertEqual(allocation.room_id, room_b.pk)
        self.assertEqual(moved.total_amount, Decimal("550.00") * 2)
        # Room A is free again for the same dates.
        self.assertTrue(check_availability(room_a, self.d(260), self.d(262), 1).available)

    def test_cancellation_releases_capacity(self):
        room = make_room("Cancel Release Room", price=400)
        prop = room.prop
        guest = make_guest(phone="0812220010")
        booking = create_multi_room_booking(
            guest=guest, prop=prop, check_in=self.d(270), check_out=self.d(272),
            allocations=[{"room": room, "allocated_guests": 1}], total_guests=1, status="Confirmed",
        )
        self.assertFalse(check_availability(room, self.d(270), self.d(272), 1).available)

        cancel_multi_room_booking(booking)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "Cancelled")
        self.assertTrue(check_availability(room, self.d(270), self.d(272), 1).available)

    def test_reinstatement_rechecks_capacity(self):
        room = make_room("Reinstate Room", price=400)
        prop = room.prop
        guest = make_guest(phone="0812220011")
        other_guest = make_guest(first_name="Other", phone="0812220012")
        booking = create_multi_room_booking(
            guest=guest, prop=prop, check_in=self.d(280), check_out=self.d(282),
            allocations=[{"room": room, "allocated_guests": 1}], total_guests=1, status="Confirmed",
        )
        cancel_multi_room_booking(booking)

        # Reinstating into a still-free room succeeds.
        reinstated = reinstate_multi_room_booking(booking, new_status="Confirmed")
        self.assertEqual(reinstated.status, "Confirmed")

        cancel_multi_room_booking(booking)
        # Someone else takes the room while it was cancelled...
        create_multi_room_booking(
            guest=other_guest, prop=prop, check_in=self.d(280), check_out=self.d(282),
            allocations=[{"room": room, "allocated_guests": 1}], total_guests=1, status="Confirmed",
        )
        # ...so reinstating the original booking must now be rejected.
        with self.assertRaises(ValidationError):
            reinstate_multi_room_booking(booking, new_status="Confirmed")

    def test_tenant_isolation(self):
        room = make_room("Tx Isolation Room", price=400)
        prop = room.prop
        guest = make_guest(phone="0812220013")
        booking = create_multi_room_booking(
            guest=guest, prop=prop, check_in=self.d(290), check_out=self.d(292),
            allocations=[{"room": room, "allocated_guests": 1}], total_guests=1, status="Confirmed",
        )

        # A random suffix keeps this schema name unique across repeated test
        # runs (this test class uses TransactionTestCase — real commits, no
        # automatic rollback — so relying solely on the `finally` cleanup
        # below for cross-run isolation would risk residue accumulating).
        suffix = uuid.uuid4().hex[:8]
        other_schema = f"tx_other_{suffix}"
        with schema_context("public"):
            other_tenant = GuestHouseTenant(
                schema_name=other_schema,
                name="Other Guest House",
                owner_name="Other Owner",
                owner_email=f"tx-other-{suffix}@example.com",
                owner_phone="0830000004",
                is_active=True,
                is_verified=True,
            )
            other_tenant.save()
            Domain.objects.create(domain=f"tx-other-{suffix}.test.com", tenant=other_tenant, is_primary=True)
        try:
            with tenant_context(other_tenant):
                # A same-named room in a completely different schema must not
                # see, conflict with, or be confused with the booking above.
                other_room = make_room("Tx Isolation Room", price=999)
                other_prop = other_room.prop
                other_guest = make_guest(phone="0812220014")
                other_booking = create_multi_room_booking(
                    guest=other_guest, prop=other_prop, check_in=self.d(290), check_out=self.d(292),
                    allocations=[{"room": other_room, "allocated_guests": 1}], total_guests=1, status="Confirmed",
                )
                self.assertEqual(Booking.objects.filter(pk=other_booking.pk).count(), 1)
                self.assertEqual(other_booking.room_id, other_room.pk)

            # Back in this test's own schema: unaffected, still exactly 1 booking.
            self.assertEqual(Booking.objects.filter(pk=booking.pk).count(), 1)
        finally:
            with schema_context("public"):
                other_tenant.delete(allow_hard_delete=True)


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

    def test_offline_snapshot_includes_checkout_timestamp_for_local_reminders(self):
        guest = make_guest("Offline", "Reminder", "0840000010")
        booking = make_booking(self.room, guest, days_ahead=0, nights=1)
        booking.status = "Checked In"
        booking.check_in_time = timezone.now()
        booking.save()

        response = self.client.get(reverse("core:offline_bootstrap"), {"device_id": self.device_id})
        row = next(item for item in response.json()["bookings"] if item["id"] == booking.pk)

        self.assertIsNotNone(row["checkout_at"])
        self.assertEqual(row["status"], "Checked In")
        self.assertEqual(row["room_id"], self.room.pk)

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


class GroupBookingUITest(CircleCoreTenantTestCase):
    def setUp(self):
        self.owner = make_owner()
        activate_trial(self.owner)
        self.client.login(username="owner", password="testpass123")
        self.today = timezone.localdate()

    def d(self, offset):
        return self.today + datetime.timedelta(days=offset)

    def test_tenant_feature_disabled_hides_group_booking(self):
        # Flag left at its default False.
        make_room("Disabled Room")
        response = self.client.get(reverse("core:group_booking_add"))
        self.assertEqual(response.status_code, 404)

        list_response = self.client.get(reverse("core:booking_list"))
        self.assertNotContains(list_response, "New Group Booking")
        self.assertNotContains(list_response, reverse("core:group_booking_add"))

    def test_tenant_feature_enabled_shows_group_booking(self):
        enable_shared_capacity()
        response = self.client.get(reverse("core:group_booking_add"))
        self.assertEqual(response.status_code, 200)

        list_response = self.client.get(reverse("core:booking_list"))
        self.assertContains(list_response, "New Group Booking")
        self.assertContains(list_response, reverse("core:group_booking_add"))

    def test_whole_room_form_display(self):
        enable_shared_capacity()
        room = make_room("Family Suite 18", price=260)
        room.max_guests = 8
        room.save()
        response = self.client.get(reverse("core:group_booking_add"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Family Suite 18")
        self.assertContains(response, "Maximum guests: 8")
        self.assertContains(response, "Booking mode: Whole room")
        self.assertContains(response, "R 260.00")

    def test_shared_room_form_display(self):
        enable_shared_capacity()
        room = make_shared_room("Ladies Dorm 1", price=180, max_guests=8)
        guest = make_guest(phone="0813330001")
        make_reserving_booking(room, guest, self.d(10), self.d(12), num_guests=3)

        response = self.client.get(reverse("core:group_booking_add"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ladies Dorm 1")
        self.assertContains(response, "Maximum capacity")
        self.assertContains(response, "Already allocated")
        self.assertContains(response, "Available spaces")
        self.assertContains(response, "PPPN")
        self.assertContains(response, "Guests to allocate")

        # The live capacity endpoint reflects the existing occupancy for the
        # same dates (this is what the page's JS calls to populate the numbers).
        availability_response = self.client.get(
            reverse("core:room_availability_json", args=[room.pk]),
            {"check_in": self.d(10).isoformat(), "check_out": self.d(12).isoformat(), "guests": 4},
        )
        payload = json.loads(availability_response.content)
        self.assertEqual(payload["maximum_capacity"], 8)
        self.assertEqual(payload["occupied_capacity"], 3)
        self.assertEqual(payload["remaining_capacity"], 5)
        self.assertTrue(payload["available"])

    def test_multi_room_booking_submission(self):
        enable_shared_capacity()
        room1 = make_shared_room("Room 1", price=180, max_guests=8)
        room18 = make_room("Room 18", price=260)
        guest = make_guest(phone="0813330002")

        response = self.client.post(reverse("core:group_booking_add"), {
            "identity_mode": "guest",
            "guest": str(guest.pk),
            "check_in_date": self.d(20).isoformat(),
            "check_out_date": self.d(22).isoformat(),
            "room_id": [str(room1.pk), str(room18.pk)],
            "allocated_guests": ["5", "4"],
            "total_guests": "9",
            "discount": "0.00",
            "booking_source": "Walk-in",
            "status": "Confirmed",
            "notes": "",
        })
        self.assertEqual(response.status_code, 302)
        booking = Booking.objects.get(guest=guest)
        self.assertEqual(booking.room_allocations.count(), 2)
        self.assertEqual(booking.num_guests, 9)
        # 5 guests x 2 nights x R180 + 4 guests(whole room, guest_multiplier=1) x 2 nights x R260
        self.assertEqual(booking.total_amount, Decimal("180.00") * 5 * 2 + Decimal("260.00") * 2)

    def test_stale_client_availability_then_server_rejection(self):
        enable_shared_capacity()
        room = make_shared_room("Stale Check Room", price=180, max_guests=6)
        guest_a = make_guest(phone="0813330003")
        guest_b = make_guest(phone="0813330004")
        ci, co = self.d(30), self.d(32)

        # Simulate: browser loaded the page when 2 spaces were free, but
        # someone else books the room before this request is submitted.
        make_reserving_booking(room, guest_a, ci, co, num_guests=5)  # only 1 space left now

        response = self.client.post(reverse("core:group_booking_add"), {
            "identity_mode": "guest",
            "guest": str(guest_b.pk),
            "check_in_date": ci.isoformat(),
            "check_out_date": co.isoformat(),
            "room_id": [str(room.pk)],
            "allocated_guests": ["2"],  # stale: client thought 2 spaces were free
            "total_guests": "2",
            "discount": "0.00",
            "booking_source": "Walk-in",
            "status": "Confirmed",
            "notes": "",
        })
        self.assertEqual(response.status_code, 200)  # re-rendered with errors, not redirected
        self.assertContains(response, "only has 1 of 2")
        self.assertEqual(Booking.objects.filter(guest=guest_b).count(), 0)

    def test_cross_tenant_room_injection_rejected(self):
        enable_shared_capacity()
        make_room("Home Room")
        guest = make_guest(phone="0813330005")

        with schema_context("public"):
            other_tenant = GuestHouseTenant(
                schema_name="ui_other",
                name="Other Guest House",
                owner_name="Other Owner",
                owner_email="ui-other@example.com",
                owner_phone="0830000005",
                is_active=True,
                is_verified=True,
            )
            other_tenant.save()
            Domain.objects.create(domain="ui-other.test.com", tenant=other_tenant, is_primary=True)
        try:
            with tenant_context(other_tenant):
                # Each tenant schema has its own independent pk sequence, so
                # the very first room created there would coincidentally get
                # the same pk as "Home Room" above — which would make this
                # test pass for the wrong reason (resolving to a real local
                # room, not actually proving cross-tenant rejection). Burn
                # through a few pks first so the foreign room's id is
                # guaranteed not to exist locally.
                for _ in range(5):
                    make_room(f"Filler {_}")
                foreign_room = make_room("Foreign Room")
                foreign_room_id = foreign_room.pk

            self.assertFalse(
                Room.objects.filter(pk=foreign_room_id).exists(),
                "test setup invalid: foreign room id coincidentally exists locally too",
            )

            response = self.client.post(reverse("core:group_booking_add"), {
                "identity_mode": "guest",
                "guest": str(guest.pk),
                "check_in_date": self.d(40).isoformat(),
                "check_out_date": self.d(42).isoformat(),
                "room_id": [str(foreign_room_id)],
                "allocated_guests": ["1"],
                "total_guests": "1",
                "discount": "0.00",
                "booking_source": "Walk-in",
                "status": "Confirmed",
                "notes": "",
            })
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "belongs to another property")
            self.assertEqual(Booking.objects.filter(guest=guest).count(), 0)
        finally:
            with schema_context("public"):
                other_tenant.delete(allow_hard_delete=True)

    def test_staff_only_warning_notes_not_in_guest_facing_pdf(self):
        room = make_room("Notes Leak Check Room", price=300)
        marker = "CAPACITY-PENDING-INTERNAL-ONLY-MARKER"
        room.internal_notes = marker
        room.save()
        guest = make_guest(phone="0813330006")
        booking = make_booking(room, guest, nights=2)

        response = self.client.get(reverse("core:booking_invoice_pdf", args=[booking.pk]))
        self.assertEqual(response.status_code, 200)
        decoded_text = extract_reportlab_pdf_text(response.content)
        # Canary: the room name IS drawn as real text on the PDF, proving the
        # decode actually worked and this test can detect a real leak.
        self.assertIn(room.name.encode(), decoded_text)
        self.assertNotIn(marker.encode(), decoded_text)

    def test_room_prices_json_backward_compatible(self):
        room = make_room("Compat Room", price=260)
        response = self.client.get(reverse("core:booking_add"))
        self.assertEqual(response.status_code, 200)
        prices = json.loads(response.context["room_prices_json"])
        entry = prices[str(room.pk)]
        # Every pre-existing key must still be present and correctly typed.
        for key in ("daily", "24_hours", "weekly", "1_hour", "2_hours", "3_hours",
                    "5_hours", "pricing_model", "max_guests"):
            self.assertIn(key, entry)
        self.assertEqual(entry["max_guests"], room.max_guests)
        self.assertEqual(entry["pricing_model"], room.pricing_model)
        # New keys are additive.
        self.assertIn("booking_mode", entry)
        self.assertIn("maximum_capacity", entry)
        self.assertIn("pppn_rate", entry)
        self.assertEqual(entry["booking_mode"], "WHOLE_ROOM")


class SharedCapacityDisplayTest(CircleCoreTenantTestCase):
    def setUp(self):
        self.owner = make_owner()
        activate_trial(self.owner)
        self.client.login(username="owner", password="testpass123")
        self.today = timezone.localdate()

    def d(self, offset):
        return self.today + datetime.timedelta(days=offset)

    def test_partially_occupied_shared_room_status(self):
        enable_shared_capacity()
        room = make_shared_room("Room 1", price=180, max_guests=8)
        guest = make_guest(phone="0814440001")
        make_reserving_booking(room, guest, self.today, self.today + datetime.timedelta(days=2), num_guests=3)

        response = self.client.get(reverse("core:room_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "3 / 8 occupied")

    def test_full_shared_room_status(self):
        enable_shared_capacity()
        room = make_shared_room("Room 15", price=180, max_guests=6)
        guest = make_guest(phone="0814440002")
        make_reserving_booking(room, guest, self.today, self.today + datetime.timedelta(days=2), num_guests=6)

        response = self.client.get(reverse("core:room_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "6 / 6 full")

    def test_whole_room_status_unchanged(self):
        # Feature flag left disabled — this is every tenant's current experience.
        room = make_room("Whole Room 18", price=260)
        response = self.client.get(reverse("core:room_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Available")
        self.assertNotContains(response, "occupied")
        self.assertNotContains(response, "Partially Occupied")

    def test_calendar_occupancy_totals(self):
        enable_shared_capacity()
        room = make_shared_room("Room 17A", price=180, max_guests=10)
        guest1 = make_guest(first_name="Alpha", phone="0814440003")
        guest2 = make_guest(first_name="Bravo", phone="0814440004")
        # Both overlap tomorrow: 2 + 2 = 4 occupied, not just the first booking.
        make_reserving_booking(room, guest1, self.d(1), self.d(3), num_guests=2)
        make_reserving_booking(room, guest2, self.d(1), self.d(4), num_guests=2)

        response = self.client.get(reverse("core:room_calendar"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "4 / 10")
        # Staff can inspect both contributing bookings, not just one blocking the room.
        self.assertContains(response, "Alpha")
        self.assertContains(response, "Bravo")

        # Performance: query count for the calendar grid must not scale with
        # the number of shared rooms (one bulk query, not one per room).
        with CaptureQueriesContext(connection) as small:
            self.client.get(reverse("core:room_calendar"))
        for i in range(10):
            extra_room = make_shared_room(f"Bulk Room {i}", price=180, max_guests=6)
            make_reserving_booking(extra_room, guest1, self.d(1), self.d(3), num_guests=1)
        with CaptureQueriesContext(connection) as large:
            self.client.get(reverse("core:room_calendar"))
        self.assertLess(
            len(large.captured_queries) - len(small.captured_queries), 5,
            "calendar query count scaled with room count — occupancy is no longer a bulk query",
        )

    def test_availability_search_extended_fields(self):
        enable_shared_capacity()
        shared_room = make_shared_room("Ladies Dorm", price=180, max_guests=8)
        rate_plan = RatePlan.objects.create(
            name="Test PPPN", amount=Decimal("180.00"), currency="ZAR", pricing_basis="per_person_per_night",
        )
        room_type = RoomType.objects.create(
            name="Ladies Dormitory Type", bathroom_type="communal", gender_restriction="ladies",
            default_rate_plan=rate_plan,
        )
        shared_room.room_category = room_type
        shared_room.room_type = "Dormitory"
        shared_room.save()

        response = self.client.get(reverse("core:availability"), {
            "check_in": self.d(5).isoformat(), "check_out": self.d(7).isoformat(),
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ladies Dorm")
        self.assertContains(response, "Dormitory")  # room_type
        self.assertContains(response, "Shared capacity")  # booking mode
        self.assertContains(response, "8 / 8")  # remaining capacity (nothing booked yet)
        self.assertContains(response, "PPPN")
        self.assertContains(response, "Communal")  # bathroom type display
        self.assertContains(response, "Ladies Only")  # gender restriction display

    def test_multi_day_remaining_capacity(self):
        enable_shared_capacity()
        room = make_shared_room("Multi Night Room", price=180, max_guests=6)
        guest = make_guest(phone="0814440005")
        # Occupies only the middle night of a 3-night search window.
        make_reserving_booking(room, guest, self.d(11), self.d(12), num_guests=5)

        response = self.client.get(reverse("core:availability"), {
            "check_in": self.d(10).isoformat(), "check_out": self.d(13).isoformat(),
        })
        self.assertEqual(response.status_code, 200)
        # The binding constraint is the middle night: 6 - 5 = 1 remaining,
        # even though the other two nights have 6 free.
        self.assertContains(response, "1 / 6")

    def test_tenant_feature_disabled_whole_room_everywhere(self):
        # Room is configured SHARED_CAPACITY at the room level, but the
        # tenant flag is left at its default False — must behave exactly
        # like a whole room everywhere.
        room = make_shared_room("Flag Off Room", price=180, max_guests=6)
        # enable_shared_capacity() deliberately NOT called.
        response = self.client.get(reverse("core:room_list"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "occupied")
        self.assertNotContains(response, "Partially Occupied")

        calendar_response = self.client.get(reverse("core:room_calendar"))
        self.assertEqual(calendar_response.status_code, 200)

        availability_response = self.client.get(reverse("core:availability"), {
            "check_in": self.d(5).isoformat(), "check_out": self.d(7).isoformat(),
        })
        self.assertContains(availability_response, "Whole room")
        self.assertNotContains(availability_response, "Shared capacity")

    def test_tenant_isolation(self):
        enable_shared_capacity()
        room = make_shared_room("Isolation Room", price=180, max_guests=8)
        guest = make_guest(phone="0814440006")
        make_reserving_booking(room, guest, self.today, self.today + datetime.timedelta(days=2), num_guests=3)

        with schema_context("public"):
            other_tenant = GuestHouseTenant(
                schema_name="display_other",
                name="Other Guest House",
                owner_name="Other Owner",
                owner_email="display-other@example.com",
                owner_phone="0830000006",
                is_active=True,
                is_verified=True,
            )
            other_tenant.save()
            Domain.objects.create(domain="display-other.test.com", tenant=other_tenant, is_primary=True)
        # TenantMainMiddleware only routes requests whose Host header is both
        # a known Domain and present in ALLOWED_HOSTS.
        settings.ALLOWED_HOSTS = list(settings.ALLOWED_HOSTS) + ["display-other.test.com"]
        try:
            with tenant_context(other_tenant):
                enable_shared_capacity()
                other_room = make_shared_room("Isolation Room", price=180, max_guests=8)
                other_guest = make_guest(phone="0814440007")
                make_reserving_booking(
                    other_room, other_guest, self.today, self.today + datetime.timedelta(days=2), num_guests=7,
                )
                other_owner = make_owner(username="other_owner")
                activate_trial(other_owner)
                # TenantClient defaults HTTP_HOST to this test's own tenant
                # domain — the real TenantMainMiddleware resolves schema from
                # the Host header, independent of the tenant_context() used
                # above, so the other tenant's own domain must be set explicitly.
                other_client = TenantClient(HTTP_HOST="display-other.test.com")
                other_client.login(username="other_owner", password="testpass123")
                other_response = other_client.get(reverse("core:room_list"))
                self.assertContains(other_response, "7 / 8 occupied")

            # Back in this test's own schema, still shows its own 3/8, unaffected.
            response = self.client.get(reverse("core:room_list"))
            self.assertContains(response, "3 / 8 occupied")
            self.assertNotContains(response, "7 / 8")
        finally:
            settings.ALLOWED_HOSTS.remove("display-other.test.com")
            with schema_context("public"):
                other_tenant.delete(allow_hard_delete=True)

    def test_operational_status_overrides_capacity(self):
        enable_shared_capacity()
        room = make_shared_room("Maintenance Shared Room", price=180, max_guests=8)
        guest = make_guest(phone="0814440008")
        make_reserving_booking(room, guest, self.today, self.today + datetime.timedelta(days=2), num_guests=3)
        room.status = "Maintenance"
        room.save()

        response = self.client.get(reverse("core:room_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Out of Service")
        self.assertNotContains(response, "3 / 8 occupied")


class SharedCapacitySecurityAuditTest(CircleCoreTenantTestCase):
    """
    Targeted security checks for the production-readiness audit. Several
    attack scenarios are already covered elsewhere in this file (cited below
    rather than duplicated); this class covers the remaining ones plus
    explicit HTTP-level coverage for a couple that previously only had
    model/service-level tests.

    Already covered elsewhere:
      - cross-tenant room ID injection:
          RoomAllocationModelTest.test_tenant_mismatch_rejection_cross_schema
          GroupBookingUITest.test_cross_tenant_room_injection_rejected
      - stale client availability at creation time:
          GroupBookingUITest.test_stale_client_availability_then_server_rejection
      - zero/negative allocated guests:
          RoomAllocationModelTest.test_zero_guests_rejected /
          test_negative_guests_rejected
      - duplicate allocation rows at the model layer (unique_together):
          RoomAllocationModelTest.test_duplicate_allocation_same_booking_and_room_is_prevented
    """

    def setUp(self):
        self.owner = make_owner()
        activate_trial(self.owner)
        self.client.login(username="owner", password="testpass123")
        self.today = timezone.localdate()

    def d(self, offset):
        return self.today + datetime.timedelta(days=offset)

    def test_enable_feature_without_permission_is_blocked(self):
        # The tenant-facing settings form is the only self-service surface an
        # authenticated Owner has — shared_capacity_booking_enabled is
        # deliberately absent from GuestHouseSettingsForm.Meta.fields, so even
        # attempting to smuggle it into the POST body must have no effect.
        settings_obj = GuestHouseSettings.objects.get_or_create(pk=1)[0]
        self.assertFalse(settings_obj.shared_capacity_booking_enabled)

        response = self.client.post(reverse("core:settings"), {
            "guest_house_name": "Test House",
            "currency": "R",
            "vat_rate": "15.00",
            "check_in_time": "14:00",
            "check_out_time": "10:00",
            "shared_capacity_booking_enabled": "true",  # attempted smuggling
        })
        self.assertIn(response.status_code, (200, 302))
        settings_obj.refresh_from_db()
        self.assertFalse(settings_obj.shared_capacity_booking_enabled)

    def test_bypass_capacity_via_direct_api_request(self):
        # Simulates a client that skips the UI/JS entirely and POSTs straight
        # to the endpoint with a guest count that outright exceeds capacity
        # (not merely stale due to a competing booking).
        enable_shared_capacity()
        room = make_shared_room("Direct API Room", price=180, max_guests=6)
        guest = make_guest(phone="0815550001")

        response = self.client.post(reverse("core:group_booking_add"), {
            "identity_mode": "guest",
            "guest": str(guest.pk),
            "check_in_date": self.d(50).isoformat(),
            "check_out_date": self.d(52).isoformat(),
            "room_id": [str(room.pk)],
            "allocated_guests": ["999"],
            "total_guests": "999",
            "discount": "0.00",
            "booking_source": "Walk-in",
            "status": "Confirmed",
            "notes": "",
        })
        self.assertEqual(response.status_code, 200)  # re-rendered with errors
        self.assertContains(response, "only has 6 of 999")
        self.assertEqual(Booking.objects.filter(guest=guest).count(), 0)

    def test_duplicate_allocation_lines_rejected_via_api(self):
        enable_shared_capacity()
        room = make_shared_room("Dup Line Room", price=180, max_guests=8)
        guest = make_guest(phone="0815550002")

        response = self.client.post(reverse("core:group_booking_add"), {
            "identity_mode": "guest",
            "guest": str(guest.pk),
            "check_in_date": self.d(55).isoformat(),
            "check_out_date": self.d(57).isoformat(),
            "room_id": [str(room.pk), str(room.pk)],  # same room twice
            "allocated_guests": ["3", "3"],
            "total_guests": "6",
            "discount": "0.00",
            "booking_source": "Walk-in",
            "status": "Confirmed",
            "notes": "",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "listed more than once")
        self.assertEqual(Booking.objects.filter(guest=guest).count(), 0)

    def test_update_booking_after_capacity_becomes_stale(self):
        enable_shared_capacity()
        room = make_shared_room("Edit Stale Room", price=180, max_guests=6)
        prop = room.prop
        guest_a = make_guest(phone="0815550003")
        guest_b = make_guest(phone="0815550004")
        ci, co = self.d(60), self.d(62)

        booking_a = create_multi_room_booking(
            guest=guest_a, prop=prop, check_in=ci, check_out=co,
            allocations=[{"room": room, "allocated_guests": 2}], total_guests=2, status="Confirmed",
        )
        # Someone else takes the remaining 4 spaces after booking_a was created.
        create_multi_room_booking(
            guest=guest_b, prop=prop, check_in=ci, check_out=co,
            allocations=[{"room": room, "allocated_guests": 4}], total_guests=4, status="Confirmed",
        )
        # booking_a now tries to grow from 2 to 5 guests — only 0 remain.
        with self.assertRaises(ValidationError) as ctx:
            edit_multi_room_booking(
                booking_a, allocations=[{"room": room, "allocated_guests": 5}], total_guests=5,
            )
        self.assertTrue(any("only has" in msg for msg in ctx.exception.messages))
        booking_a.refresh_from_db()
        self.assertEqual(booking_a.room_allocations.get().allocated_guests, 2)  # untouched


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

    def test_occupied_room_card_shows_current_guest(self):
        booking = make_booking(self.room, self.guest, days_ahead=1)
        booking.status = "Checked In"
        booking.save()
        self.room.status = "Occupied"
        self.room.save(update_fields=["status"])

        response = self.client.get(reverse("core:room_list"))

        self.assertContains(response, "Currently staying")
        self.assertContains(response, self.guest.full_name)
        self.assertContains(response, reverse("core:booking_detail", args=[booking.pk]))

    def test_occupied_room_card_shows_number_plate_identity(self):
        vehicle_guest = Guest.get_or_create_for_vehicle("CA 555-555")
        booking = make_booking(self.room, vehicle_guest, days_ahead=1)
        booking.vehicle_registration = vehicle_guest.vehicle_registration
        booking.status = "Checked In"
        booking.save()
        self.room.status = "Occupied"
        self.room.save(update_fields=["status"])

        response = self.client.get(reverse("core:room_list"))

        self.assertContains(response, "Currently staying")
        self.assertContains(response, "CA 555-555")

    def test_notification_feed_uses_clean_text_and_semantic_type(self):
        today = timezone.localdate()
        booking = Booking.objects.create(
            room=self.room,
            guest=self.guest,
            check_in_date=today,
            check_out_date=today + datetime.timedelta(days=1),
            booking_duration_type="daily",
            num_guests=1,
            rate_per_night=self.room.price_per_night,
            status="Confirmed",
            booking_source="Walk-in",
        )

        response = self.client.get(reverse("core:notifications_feed"))

        self.assertEqual(response.status_code, 200)
        alert = next(item for item in response.json()["alerts"] if item["id"] == f"booking-created-{booking.pk}")
        self.assertEqual(alert["type"], "booking_created")
        self.assertEqual(alert["body"], f"{self.guest.full_name} · {self.room.name} — {booking.booking_reference}")
        self.assertNotIn("icon", alert)
        self.assertNotIn("Â", alert["body"])

    def test_checkin_alert_does_not_trigger_checkout_reminder_early(self):
        booking = make_booking(self.room, self.guest, days_ahead=1)
        local_now = timezone.localtime()
        checkout_at = local_now + datetime.timedelta(minutes=30)
        Booking.objects.filter(pk=booking.pk).update(
            status="Checked In",
            check_in_date=local_now.date(),
            check_out_date=local_now.date(),
            booking_duration_type="1_hour",
            booking_start_time=local_now.time().replace(second=0, microsecond=0),
            booking_end_time=checkout_at.time().replace(second=0, microsecond=0),
            check_in_time=timezone.now(),
        )

        alerts = self.client.get(reverse("core:notifications_feed")).json()["alerts"]

        self.assertTrue(any(item["type"] == "checked_in" for item in alerts))
        self.assertFalse(any(item["type"] == "checkout_reminder" for item in alerts))

    def test_checkout_reminder_triggers_inside_five_minute_window(self):
        booking = make_booking(self.room, self.guest, days_ahead=1)
        local_now = timezone.localtime()
        checkout_at = local_now + datetime.timedelta(minutes=4)
        checkin_at = local_now - datetime.timedelta(hours=1)
        Booking.objects.filter(pk=booking.pk).update(
            status="Checked In",
            check_in_date=local_now.date(),
            check_out_date=local_now.date(),
            booking_duration_type="1_hour",
            booking_start_time=checkin_at.time().replace(second=0, microsecond=0),
            booking_end_time=checkout_at.time().replace(second=0, microsecond=0),
            check_in_time=timezone.now() - datetime.timedelta(hours=1),
        )

        alerts = self.client.get(reverse("core:notifications_feed")).json()["alerts"]
        reminder = next(item for item in alerts if item["type"] == "checkout_reminder")

        self.assertEqual(reminder["title"], "Checkout in 5 Minutes")
        self.assertIn(self.room.name, reminder["body"])

    def test_room_needs_cleaning_notification_is_in_feed(self):
        self.room.cleaning_status = "Needs Cleaning"
        self.room.status = "Cleaning"
        self.room.save(update_fields=["cleaning_status", "status"])

        alerts = self.client.get(reverse("core:notifications_feed")).json()["alerts"]

        cleaning_alert = next(item for item in alerts if item["type"] == "needs_cleaning")
        self.assertEqual(cleaning_alert["title"], "Room Needs Cleaning")
        self.assertIn(self.room.name, cleaning_alert["body"])

    def test_booking_add_has_one_guest_field_and_one_submit_action(self):
        response = self.client.get(reverse("core:booking_add"))
        content = response.content.decode()
        self.assertEqual(content.count('id="id_guest"'), 1)
        self.assertEqual(content.count('id="form-submit-btn"'), 1)
        self.assertNotIn('id="book-now-btn"', content)
        self.assertIn('id="booking-form"', content)
        self.assertFalse(response.context["form"].fields["guest"].queryset.filter(is_generic=True).exists())
        self.assertEqual(response.context["form"]["identity_mode"].value(), "walk_in")
        self.assertEqual(response.context["form"]["booking_duration_type"].value(), "1_hour")
        self.assertIn("Walk-in guest selected", content)
        self.assertIn('id="rate-picker-button"', content)
        self.assertIn('role="listbox"', content)

    def test_quick_identity_modal_is_accessible_and_updates_main_modes(self):
        response = self.client.get(reverse("core:booking_add"))
        content = response.content.decode()
        self.assertIn('role="dialog"', content)
        self.assertIn('aria-modal="true"', content)
        self.assertIn("window.setIdentityMode('guest')", content)
        self.assertIn("window.setIdentityMode('plate')", content)
        self.assertIn("plateInput.dispatchEvent(new Event('input'))", content)

    def test_editing_vehicle_booking_opens_in_number_plate_mode(self):
        booking = make_booking(self.room, Guest.get_generic())
        booking.vehicle_registration = "YSR 142 GP"
        booking.save(update_fields=["vehicle_registration"])
        response = self.client.get(reverse("core:booking_edit", args=[booking.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"]["identity_mode"].value(), "plate")

    def test_editing_walk_in_booking_opens_in_walk_in_mode(self):
        booking = make_booking(self.room, Guest.get_generic())
        response = self.client.get(reverse("core:booking_edit", args=[booking.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"]["identity_mode"].value(), "walk_in")

    def test_default_walk_in_booking_requires_no_guest_or_vehicle(self):
        check_in = timezone.localdate() + datetime.timedelta(days=1)
        response = self.client.post(
            reverse("core:booking_add"),
            {
                "identity_mode": "walk_in",
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
        self.assertEqual(response.status_code, 302)
        booking = Booking.objects.get(room=self.room)
        self.assertTrue(booking.guest.is_generic)
        self.assertEqual(booking.vehicle_registration, "")

    def test_number_plate_booking_creates_persistent_vehicle_profile(self):
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
        self.assertFalse(booking.guest.is_generic)
        self.assertTrue(booking.guest.is_vehicle_profile)
        self.assertEqual(booking.guest.vehicle_registration, "YSR 142 GP")
        self.assertEqual(booking.vehicle_registration, "YSR 142 GP")

        detail = self.client.get(reverse("core:guest_detail", args=[booking.guest_id]))
        self.assertContains(detail, "YSR 142 GP")
        self.assertContains(detail, booking.booking_reference)

    def test_number_plate_bookings_reuse_profile_and_share_history(self):
        plate = "CA 123-456"
        profile = Guest.get_or_create_for_vehicle(plate.lower())
        same_profile = Guest.get_or_create_for_vehicle("  CA   123-456  ")
        self.assertEqual(profile.pk, same_profile.pk)

        first = make_booking(self.room, profile)
        first.vehicle_registration = plate
        first.save(update_fields=["vehicle_registration"])
        second_room = Room.objects.create(
            prop=self.room.prop,
            name="Room History",
            room_type="Single",
            price_per_night=Decimal("350.00"),
        )
        second = make_booking(second_room, same_profile, days_ahead=4)
        second.vehicle_registration = plate
        second.save(update_fields=["vehicle_registration"])

        detail = self.client.get(reverse("core:guest_detail", args=[profile.pk]))
        self.assertContains(detail, first.booking_reference)
        self.assertContains(detail, second.booking_reference)
        self.assertEqual(detail.context["stay_count"], 2)

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
        self.assertEqual(response.context["form"]["booking_duration_type"].value(), "daily")
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

    def test_single_payment_workflow_remains_available(self):
        booking = make_booking(self.room, self.guest, days_ahead=6, nights=2)
        response = self.client.post(
            reverse("core:payment_add", args=[booking.pk]),
            {
                "payment_mode": "single",
                "amount": "600.00",
                "payment_date": timezone.localdate().isoformat(),
                "payment_method": "EFT",
                "reference": "EFT-ONE",
                "notes": "Single partial payment",
            },
        )

        self.assertRedirects(response, reverse("core:booking_detail", args=[booking.pk]))
        payment = booking.payments.get()
        self.assertEqual(payment.payment_method, "EFT")
        self.assertEqual(payment.notes, "Single partial payment")
        booking.refresh_from_db()
        self.assertEqual(booking.balance_due, Decimal("400.00"))

    def test_booking_detail_links_to_preselected_deposit_payment(self):
        booking = make_booking(self.room, self.guest, days_ahead=10, nights=2)
        booking.deposit_required = Decimal("300.00")
        booking.save()

        detail = self.client.get(reverse("core:booking_detail", args=[booking.pk]))
        self.assertContains(detail, "Pay Deposit · R 300.00")
        self.assertContains(detail, f'{reverse("core:payment_add", args=[booking.pk])}?intent=deposit')

        payment_page = self.client.get(reverse("core:payment_add", args=[booking.pk]), {"intent": "deposit"})
        self.assertEqual(payment_page.context["payment_intent"], "deposit")
        self.assertEqual(payment_page.context["form"]["amount"].value(), Decimal("300.00"))
        self.assertContains(payment_page, "Pay booking deposit")

    def test_booking_without_deposit_can_set_and_continue_to_payment(self):
        booking = make_booking(self.room, self.guest, days_ahead=12, nights=2)
        detail = self.client.get(reverse("core:booking_detail", args=[booking.pk]))
        self.assertContains(detail, "Set &amp; Pay", html=False)
        self.assertContains(detail, reverse("core:booking_set_deposit", args=[booking.pk]))

        response = self.client.post(
            reverse("core:booking_set_deposit", args=[booking.pk]),
            {"deposit_amount": "250.00"},
        )

        self.assertRedirects(
            response,
            f'{reverse("core:payment_add", args=[booking.pk])}?intent=deposit',
            fetch_redirect_response=False,
        )
        booking.refresh_from_db()
        self.assertEqual(booking.deposit_required, Decimal("250.00"))

    def test_deposit_requirement_cannot_exceed_booking_total(self):
        booking = make_booking(self.room, self.guest, days_ahead=13, nights=2)

        response = self.client.post(
            reverse("core:booking_set_deposit", args=[booking.pk]),
            {"deposit_amount": "1000.01"},
            follow=True,
        )

        self.assertContains(response, "Deposit cannot exceed the booking total")
        booking.refresh_from_db()
        self.assertEqual(booking.deposit_required, Decimal("0.00"))

    def test_partial_deposit_payments_remain_classified_as_deposits(self):
        booking = make_booking(self.room, self.guest, days_ahead=11, nights=2)
        booking.deposit_required = Decimal("300.00")
        booking.save()

        for amount, reference in (("100.00", "DEP-ONE"), ("200.00", "DEP-TWO")):
            response = self.client.post(
                reverse("core:payment_add", args=[booking.pk]),
                {
                    "payment_mode": "single",
                    "payment_intent": "deposit",
                    "amount": amount,
                    "payment_date": timezone.localdate().isoformat(),
                    "payment_method": "EFT",
                    "reference": reference,
                    "notes": "Deposit instalment",
                },
            )
            self.assertRedirects(response, reverse("core:booking_detail", args=[booking.pk]))

        self.assertEqual(booking.payments.filter(payment_type="Deposit").count(), 2)
        detail = self.client.get(reverse("core:booking_detail", args=[booking.pk]))
        self.assertEqual(detail.context["deposit_outstanding"], Decimal("0.00"))
        self.assertNotContains(detail, "Pay Deposit ·")

    def split_payment_data(self, booking, first_amount="400.00", second_amount="600.00", second_method="Card"):
        return {
            "payment_mode": "split",
            "split-payment_date": timezone.localdate().isoformat(),
            "split-notes": "Guest requested split tender",
            "tender-TOTAL_FORMS": "3",
            "tender-INITIAL_FORMS": "0",
            "tender-MIN_NUM_FORMS": "2",
            "tender-MAX_NUM_FORMS": "3",
            "tender-0-payment_method": "Cash",
            "tender-0-amount": first_amount,
            "tender-0-reference": "CASH-PORTION",
            "tender-1-payment_method": second_method,
            "tender-1-amount": second_amount,
            "tender-1-reference": "SECOND-PORTION",
            "tender-2-payment_method": "",
            "tender-2-amount": "",
            "tender-2-reference": "",
        }

    def test_split_payment_records_each_method_atomically(self):
        booking = make_booking(self.room, self.guest, days_ahead=7, nights=2)

        response = self.client.post(
            reverse("core:payment_add", args=[booking.pk]),
            self.split_payment_data(booking),
        )

        self.assertRedirects(response, reverse("core:booking_detail", args=[booking.pk]))
        payments = booking.payments.order_by("payment_method")
        self.assertEqual(payments.count(), 2)
        self.assertEqual(
            set(payments.values_list("payment_method", "amount")),
            {("Cash", Decimal("400.00")), ("Card", Decimal("600.00"))},
        )
        booking.refresh_from_db()
        self.assertEqual(booking.balance_due, Decimal("0.00"))

    def test_invalid_split_payment_records_nothing(self):
        booking = make_booking(self.room, self.guest, days_ahead=8, nights=2)
        data = self.split_payment_data(booking, first_amount="600.00", second_amount="600.00")

        response = self.client.post(reverse("core:payment_add", args=[booking.pk]), data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot exceed the outstanding balance")
        self.assertFalse(booking.payments.exists())

    def test_split_payment_rejects_duplicate_methods(self):
        booking = make_booking(self.room, self.guest, days_ahead=9, nights=2)
        data = self.split_payment_data(booking, first_amount="500.00", second_amount="500.00", second_method="Cash")

        response = self.client.post(reverse("core:payment_add", args=[booking.pk]), data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Use each payment method only once")
        self.assertFalse(booking.payments.exists())


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


class IndividualSharedRoomBookingTest(CircleCoreTenantTestCase):
    """
    create_individual_shared_room_booking(): one independently paying guest
    (or a small family/group sharing one payment account, when
    allocated_guests > 1) added into an already-partially-occupied
    SHARED_CAPACITY room, without ever touching any other guest's booking.
    """

    def setUp(self):
        self.owner = make_owner()
        activate_trial(self.owner)
        self.client.login(username="owner", password="testpass123")
        self.today = timezone.localdate()

    def d(self, offset):
        return self.today + datetime.timedelta(days=offset)

    def test_add_one_guest_to_empty_shared_room(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest = make_guest(first_name="Ada", phone="0814440001")
        ci, co = self.d(10), self.d(13)

        booking = create_individual_shared_room_booking(
            guest=guest, room=room, check_in=ci, check_out=co,
            allocated_guests=1, staff_user=self.owner,
        )

        self.assertEqual(booking.room_allocations.count(), 1)
        allocation = booking.room_allocations.get()
        self.assertEqual(allocation.allocated_guests, 1)
        self.assertEqual(allocation.room_id, room.pk)
        self.assertEqual(occupancy_snapshot(room, ci), (1, 7, 6))
        self.assertEqual(booking.total_amount, Decimal("180.00") * 1 * 3)

    def test_add_another_guest_to_partially_occupied_room(self):
        # The exact Room 02 example from the business requirement:
        # capacity 7, existing occupancy 1, +1 new guest -> 2 / 7 occupied.
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest_a = make_guest(first_name="Ada", phone="0814440002")
        guest_b = make_guest(first_name="Ben", phone="0814440003")
        ci, co = self.d(20), self.d(23)

        booking_a = create_individual_shared_room_booking(
            guest=guest_a, room=room, check_in=ci, check_out=co,
            allocated_guests=1, staff_user=self.owner,
        )
        self.assertEqual(occupancy_snapshot(room, ci), (1, 7, 6))

        booking_b = create_individual_shared_room_booking(
            guest=guest_b, room=room, check_in=ci, check_out=co,
            allocated_guests=1, staff_user=self.owner,
        )

        self.assertEqual(occupancy_snapshot(room, ci), (2, 7, 5))
        self.assertNotEqual(booking_a.pk, booking_b.pk)
        self.assertEqual(Booking.objects.filter(room_allocations__room=room).distinct().count(), 2)

    def test_existing_booking_remains_unchanged(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest_a = make_guest(first_name="Ada", phone="0814440004")
        guest_b = make_guest(first_name="Ben", phone="0814440005")
        ci, co = self.d(30), self.d(33)

        booking_a = create_individual_shared_room_booking(
            guest=guest_a, room=room, check_in=ci, check_out=co,
            allocated_guests=1, staff_user=self.owner,
        )
        before = _model_snapshot_for_test(booking_a)

        create_individual_shared_room_booking(
            guest=guest_b, room=room, check_in=ci, check_out=co,
            allocated_guests=1, staff_user=self.owner,
        )

        booking_a.refresh_from_db()
        self.assertEqual(before, _model_snapshot_for_test(booking_a))
        self.assertEqual(booking_a.room_allocations.count(), 1)
        self.assertEqual(booking_a.room_allocations.get().allocated_guests, 1)

    def test_separate_booking_reference_generated(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest_a = make_guest(first_name="Ada", phone="0814440006")
        guest_b = make_guest(first_name="Ben", phone="0814440007")
        ci, co = self.d(40), self.d(42)

        booking_a = create_individual_shared_room_booking(
            guest=guest_a, room=room, check_in=ci, check_out=co, allocated_guests=1, staff_user=self.owner,
        )
        booking_b = create_individual_shared_room_booking(
            guest=guest_b, room=room, check_in=ci, check_out=co, allocated_guests=1, staff_user=self.owner,
        )

        self.assertTrue(booking_a.booking_reference)
        self.assertTrue(booking_b.booking_reference)
        self.assertNotEqual(booking_a.booking_reference, booking_b.booking_reference)

    def test_separate_invoice_generated(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest_a = make_guest(first_name="Ada", phone="0814440008")
        guest_b = make_guest(first_name="Ben", phone="0814440009")
        ci, co = self.d(50), self.d(53)

        booking_a = create_individual_shared_room_booking(
            guest=guest_a, room=room, check_in=ci, check_out=co, allocated_guests=1, staff_user=self.owner,
        )
        booking_b = create_individual_shared_room_booking(
            guest=guest_b, room=room, check_in=ci, check_out=co, allocated_guests=1, staff_user=self.owner,
        )

        resp_a = self.client.get(reverse("core:booking_invoice_pdf", args=[booking_a.pk]))
        resp_b = self.client.get(reverse("core:booking_invoice_pdf", args=[booking_b.pk]))
        self.assertEqual(resp_a.status_code, 200)
        self.assertEqual(resp_b.status_code, 200)

        text_a = extract_reportlab_pdf_text(resp_a.content)
        text_b = extract_reportlab_pdf_text(resp_b.content)
        self.assertIn(booking_a.booking_reference.encode(), text_a)
        self.assertIn(booking_b.booking_reference.encode(), text_b)
        self.assertNotIn(booking_b.booking_reference.encode(), text_a)
        self.assertNotIn(booking_a.booking_reference.encode(), text_b)

    def test_separate_payment_balance_maintained(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest_a = make_guest(first_name="Ada", phone="0814440010")
        guest_b = make_guest(first_name="Ben", phone="0814440011")
        ci, co = self.d(60), self.d(63)  # 3 nights, matches the R540 pricing example

        booking_a = create_individual_shared_room_booking(
            guest=guest_a, room=room, check_in=ci, check_out=co, allocated_guests=1, staff_user=self.owner,
            payment_info={"amount": Decimal("540.00"), "payment_method": "Cash", "payment_type": "Payment"},
        )
        booking_b = create_individual_shared_room_booking(
            guest=guest_b, room=room, check_in=ci, check_out=co, allocated_guests=1, staff_user=self.owner,
        )

        self.assertEqual(booking_a.total_amount, Decimal("540.00"))
        self.assertEqual(booking_a.payments.count(), 1)
        self.assertEqual(booking_b.payments.count(), 0)
        self.assertEqual(booking_a.balance_due, Decimal("0.00"))
        self.assertEqual(booking_b.balance_due, Decimal("540.00"))

    def test_exact_final_capacity_succeeds(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest_a = make_guest(first_name="Ada", phone="0814440012")
        guest_b = make_guest(first_name="Ben", phone="0814440013")
        ci, co = self.d(70), self.d(72)

        create_individual_shared_room_booking(
            guest=guest_a, room=room, check_in=ci, check_out=co, allocated_guests=6, staff_user=self.owner,
        )
        booking_b = create_individual_shared_room_booking(
            guest=guest_b, room=room, check_in=ci, check_out=co, allocated_guests=1, staff_user=self.owner,
        )

        self.assertEqual(occupancy_snapshot(room, ci), (7, 7, 0))
        self.assertIsNotNone(booking_b.pk)

    def test_capacity_plus_one_fails(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest_a = make_guest(first_name="Ada", phone="0814440014")
        guest_b = make_guest(first_name="Ben", phone="0814440015")
        ci, co = self.d(80), self.d(82)

        create_individual_shared_room_booking(
            guest=guest_a, room=room, check_in=ci, check_out=co, allocated_guests=7, staff_user=self.owner,
        )

        with self.assertRaises(ValidationError) as ctx:
            create_individual_shared_room_booking(
                guest=guest_b, room=room, check_in=ci, check_out=co, allocated_guests=1, staff_user=self.owner,
            )
        self.assertTrue(any("only has 0 of 1" in msg for msg in ctx.exception.messages), ctx.exception.messages)
        self.assertEqual(Booking.objects.filter(guest=guest_b).count(), 0)
        self.assertEqual(occupancy_snapshot(room, ci), (7, 7, 0))

    def test_cross_tenant_room_selection_fails(self):
        enable_shared_capacity()
        make_room("Home Filler")
        guest = make_guest(phone="0814440098")

        with schema_context("public"):
            other_tenant = GuestHouseTenant(
                schema_name="individual_booking_other",
                name="Other Guest House",
                owner_name="Other Owner",
                owner_email="individual-other@example.com",
                owner_phone="0830000098",
                is_active=True,
                is_verified=True,
            )
            other_tenant.save()
            Domain.objects.create(domain="individual-booking-other.test.com", tenant=other_tenant, is_primary=True)
        try:
            with tenant_context(other_tenant):
                # Burn through a few pks so the foreign room's id is guaranteed
                # not to coincidentally exist in this (local) tenant's schema.
                for _ in range(5):
                    make_room(f"Filler {_}")
                foreign_room = make_shared_room("Foreign Shared Room", price=180, max_guests=7)
                foreign_room_id = foreign_room.pk

            self.assertFalse(
                Room.objects.filter(pk=foreign_room_id).exists(),
                "test setup invalid: foreign room id coincidentally exists locally too",
            )

            with self.assertRaises(ValidationError) as ctx:
                create_individual_shared_room_booking(
                    guest=guest, room=foreign_room,
                    check_in=self.d(90), check_out=self.d(92),
                    allocated_guests=1, staff_user=self.owner,
                )
            self.assertTrue(any("could not be found" in msg for msg in ctx.exception.messages), ctx.exception.messages)
            self.assertEqual(Booking.objects.filter(guest=guest).count(), 0)
        finally:
            with schema_context("public"):
                other_tenant.delete(allow_hard_delete=True)

    def test_feature_disabled_tenant_retains_old_behaviour(self):
        # Flag left at its default False, even though the room itself is
        # already marked SHARED_CAPACITY (e.g. mid-configuration) — the
        # tenant-level flag is the master kill-switch.
        room = make_room("Room 02", price=180)
        room.booking_mode = "SHARED_CAPACITY"
        room.max_guests = 7
        room.pricing_model = "per_person"
        room.save()
        guest = make_guest(phone="0814440099")

        with self.assertRaises(ValidationError) as ctx:
            create_individual_shared_room_booking(
                guest=guest, room=room, check_in=self.d(95), check_out=self.d(97),
                allocated_guests=1, staff_user=self.owner,
            )
        self.assertIn("not enabled", ctx.exception.messages[0])
        self.assertEqual(Booking.objects.filter(guest=guest).count(), 0)

        # The add-guest screen doesn't exist at all for this tenant.
        response = self.client.get(reverse("core:room_add_shared_guest", args=[room.pk]))
        self.assertEqual(response.status_code, 404)

        # The room list keeps its plain whole-room quick action untouched.
        list_response = self.client.get(reverse("core:room_list"))
        self.assertContains(list_response, "Check In")
        self.assertNotContains(list_response, "Add Guest")

    def test_audit_log_records_staff_action(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest = make_guest(first_name="Ada", phone="0814440100")
        ci, co = self.d(100), self.d(103)

        booking = create_individual_shared_room_booking(
            guest=guest, room=room, check_in=ci, check_out=co,
            allocated_guests=1, staff_user=self.owner,
        )

        log = AuditLog.objects.filter(object_type="Booking", object_id=str(booking.pk)).order_by("-created_at").first()
        self.assertIsNotNone(log)
        self.assertEqual(log.actor, self.owner)
        self.assertEqual(log.after["room"], room.name)
        self.assertEqual(log.after["allocated_guests"], 1)
        self.assertEqual(log.after["booking_reference"], booking.booking_reference)
        self.assertEqual(log.after["calculated_amount"], str(booking.room_allocations.get().line_total))


def _model_snapshot_for_test(booking):
    return {
        "check_in_date": booking.check_in_date,
        "check_out_date": booking.check_out_date,
        "num_guests": booking.num_guests,
        "total_amount": booking.total_amount,
        "balance_due": booking.balance_due,
        "status": booking.status,
        "booking_reference": booking.booking_reference,
        "rate_per_night": booking.rate_per_night,
    }


class IndividualSharedRoomBookingConcurrencyTest(ConcurrencyTenantTestCase):
    """Two staff members must not be able to sell the same final shared-room
    space — mirrors BookingTransactionConcurrencyTest's pattern exactly, but
    exercises create_individual_shared_room_booking() directly."""

    def setUp(self):
        self.today = timezone.localdate()
        self.schema_name = connection.schema_name

    def d(self, offset):
        return self.today + datetime.timedelta(days=offset)

    def test_simultaneous_final_space_requests_cannot_overbook(self):
        enable_shared_capacity()
        room = make_shared_room("Concurrency Individual Room", price=180, max_guests=7)
        # Pre-fill 6 of 7 spaces so exactly one more single-guest booking fits.
        create_individual_shared_room_booking(
            guest=make_guest(first_name="Filler", phone="0814550000"),
            room=room, check_in=self.d(100), check_out=self.d(102), allocated_guests=6,
        )
        guest_a = make_guest(first_name="Ada", phone="0814550001")
        guest_b = make_guest(first_name="Ben", phone="0814550002")
        ci, co = self.d(100), self.d(102)

        def attempt(guest):
            def _run():
                return create_individual_shared_room_booking(
                    guest=guest, room=room, check_in=ci, check_out=co, allocated_guests=1,
                )
            return _run

        results = run_concurrently(self.schema_name, [attempt(guest_a), attempt(guest_b)])
        successes = [r for r, e in results if e is None]
        failures = [e for r, e in results if e is not None]
        self.assertEqual(len(successes), 1, f"expected exactly 1 success, got results={results}")
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], ValidationError)

        with schema_context(self.schema_name):
            self.assertEqual(occupancy_snapshot(room, ci), (7, 7, 0))


class RoomListSharedCapacityInterfaceTest(CircleCoreTenantTestCase):
    """
    /rooms/ interface for tenant-gated shared-capacity rooms: the room card
    must let staff book/check in another guest without ever touching the
    guest already staying in the room. Covers every card state (empty,
    partially occupied, full, out-of-service) plus the feature-disabled
    fallback to the plain whole-room card every other tenant still gets.
    """

    def setUp(self):
        self.owner = make_owner()
        activate_trial(self.owner)
        self.client.login(username="owner", password="testpass123")

    def test_empty_shared_room_shows_check_in_guest(self):
        enable_shared_capacity()
        make_shared_room("Room 02", price=180, max_guests=7)

        response = self.client.get(reverse("core:room_list"))

        self.assertContains(response, "Check In Guest")
        self.assertNotContains(response, "Add Another Guest")
        self.assertNotContains(response, "View Occupants")
        self.assertContains(response, "0 / 7 occupied")
        self.assertContains(response, "7 spaces remaining")
        self.assertContains(response, "per person per night")
        self.assertContains(response, "Available")

    def test_partially_occupied_room_shows_add_another_guest(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest = make_guest(first_name="Ada", phone="0815550001")
        today = timezone.localdate()
        create_individual_shared_room_booking(
            guest=guest, room=room,
            check_in=today, check_out=today + datetime.timedelta(days=2),
            allocated_guests=1, staff_user=self.owner,
        )

        response = self.client.get(reverse("core:room_list"))

        self.assertContains(response, "Add Another Guest")
        self.assertContains(response, "View Occupants")
        self.assertContains(response, "View Room")
        self.assertContains(response, "1 / 7 occupied")
        self.assertContains(response, "6 spaces remaining")
        self.assertNotContains(response, "Check In Guest")

    def test_full_shared_room_disables_add_another_guest(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest = make_guest(first_name="Ada", phone="0815550002")
        today = timezone.localdate()
        create_individual_shared_room_booking(
            guest=guest, room=room,
            check_in=today, check_out=today + datetime.timedelta(days=2),
            allocated_guests=7, staff_user=self.owner,
        )

        response = self.client.get(reverse("core:room_list"))

        self.assertContains(response, "Full")
        self.assertContains(response, "View Occupants")
        self.assertNotContains(response, "Add Another Guest")
        self.assertNotContains(response, "Check In Guest")
        # The disabled action must be a real, non-clickable button — not just
        # a plain link relying on colour to look inactive.
        self.assertContains(response, "disabled")

    def test_out_of_service_shared_room_disables_regardless_of_capacity(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        room.status = "Maintenance"
        room.save()

        response = self.client.get(reverse("core:room_list"))

        self.assertContains(response, "Unavailable")
        self.assertNotContains(response, "Check In Guest")
        self.assertNotContains(response, "Add Another Guest")

    def test_feature_disabled_tenant_retains_existing_card(self):
        # Flag left at its default False — the room-card interface, wording
        # and quick action must be byte-for-byte what every other tenant
        # already had, even though this room is internally marked
        # SHARED_CAPACITY (e.g. mid-configuration).
        room = make_room("Room 02", price=180)
        room.booking_mode = "SHARED_CAPACITY"
        room.max_guests = 7
        room.pricing_model = "per_person"
        room.save()

        response = self.client.get(reverse("core:room_list"))

        self.assertContains(response, "Check In</a>")
        self.assertNotContains(response, "Check In Guest")
        self.assertNotContains(response, "Add Another Guest")
        self.assertNotContains(response, "View Occupants")
        self.assertNotContains(response, "Partially Occupied")
        self.assertNotContains(response, "spaces remaining")

    def test_add_another_guest_link_has_correct_room_context(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest = make_guest(first_name="Ada", phone="0815550003")
        today = timezone.localdate()
        create_individual_shared_room_booking(
            guest=guest, room=room,
            check_in=today, check_out=today + datetime.timedelta(days=2),
            allocated_guests=1, staff_user=self.owner,
        )

        response = self.client.get(reverse("core:room_list"))

        expected_url = reverse("core:room_add_shared_guest", args=[room.pk])
        self.assertContains(response, f'href="{expected_url}"')

    def test_existing_occupant_not_altered_by_viewing_the_interface(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest = make_guest(first_name="Ada", phone="0815550004")
        today = timezone.localdate()
        booking = create_individual_shared_room_booking(
            guest=guest, room=room,
            check_in=today, check_out=today + datetime.timedelta(days=2),
            allocated_guests=1, staff_user=self.owner,
        )
        before = _model_snapshot_for_test(booking)

        # Merely viewing the room list, room detail and the add-guest form
        # must never mutate the existing occupant's booking.
        self.client.get(reverse("core:room_list"))
        self.client.get(reverse("core:room_detail", args=[room.pk]))
        self.client.get(reverse("core:room_add_shared_guest", args=[room.pk]))

        booking.refresh_from_db()
        self.assertEqual(before, _model_snapshot_for_test(booking))
        self.assertEqual(booking.room_allocations.count(), 1)
        self.assertEqual(booking.room_allocations.get().allocated_guests, 1)
