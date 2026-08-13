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
    get_tenant_domain_model,
    get_tenant_model,
    schema_context,
    tenant_context,
)

from tenants.models import Domain, GuestHouseTenant

from .availability import check_availability, occupancy_snapshot, shared_room_status_label
from .roles import assign_role
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
    BookingRefund,
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

    @classmethod
    def tearDownClass(cls):
        try:
            super().tearDownClass()
        finally:
            # Secondary tenant schemas are created by isolation tests inside
            # TestCase atomics. PostgreSQL cannot drop them until those atomics
            # have closed, so remove only the known test-schema patterns here.
            connection.set_schema_to_public()
            with connection.cursor() as cursor:
                cursor.execute("""
                    SELECT nspname FROM pg_namespace
                    WHERE nspname LIKE %s ESCAPE '\\'
                       OR nspname LIKE %s ESCAPE '\\'
                """, [r'%\_other', r'tx\_other\_%'])
                for (schema_name,) in cursor.fetchall():
                    cursor.execute(
                        f'DROP SCHEMA IF EXISTS {connection.ops.quote_name(schema_name)} CASCADE'
                    )
            connection.set_schema_to_public()


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
        try:
            tenant = get_tenant_model().objects.filter(pk=getattr(cls.tenant, 'pk', None)).first()
            if tenant is not None:
                tenant.delete(force_drop=True)
            # Guarantee removal even if a failed concurrent connection left the
            # django-tenants model row/schema lifecycle partially completed.
            with connection.cursor() as cursor:
                cursor.execute(
                    f'DROP SCHEMA IF EXISTS {connection.ops.quote_name(cls.schema_name)} CASCADE'
                )
        finally:
            # TenantMixin schema deletion may change the search path; the next
            # TransactionTestCase must always begin and flush in the public schema.
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
                    pk=2147480000,
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
                other_tenant.delete(force_drop=True)


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


class WalkInSharedRoomBookingWorkflowTest(CircleCoreTenantTestCase):
    """
    The Walk-in-style /rooms/<pk>/add-guest/ workflow: an Individual Guest or
    a Group Booking (one payer, several spaces) added into a partially
    occupied SHARED_CAPACITY room, each always as its own separate booking.
    """

    def setUp(self):
        self.owner = make_owner()
        activate_trial(self.owner)
        self.client.login(username="owner", password="testpass123")
        self.today = timezone.localdate()

    def d(self, offset):
        return self.today + datetime.timedelta(days=offset)

    def base_payload(self, room, **overrides):
        payload = {
            "booking_purpose": "individual",
            "identity_mode": "new_guest",
            "guest": "",
            "new_first_name": "Nomvula",
            "new_last_name": "Dube",
            "new_phone": "0821110001",
            "new_email": "",
            "new_id_number": "",
            "vehicle_registration": "",
            "check_in_date": self.d(10).isoformat(),
            "check_out_date": self.d(13).isoformat(),
            "allocated_guests": "1",
            "booking_source": "Walk-in",
            "notes": "",
            "collect_payment": "",
            "payment_amount": "",
            "payment_method": "Cash",
            "payment_type": "Payment",
            "payment_reference": "",
        }
        payload.update(overrides)
        return payload

    def test_valid_individual_booking(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)

        response = self.client.post(
            reverse("core:room_add_shared_guest", args=[room.pk]),
            self.base_payload(room),
        )

        booking = Booking.objects.get(guest__first_name="Nomvula")
        self.assertRedirects(
            response, reverse("core:room_add_shared_guest_success", args=[booking.pk])
        )
        self.assertEqual(booking.room_allocations.count(), 1)
        allocation = booking.room_allocations.get()
        self.assertEqual(allocation.allocated_guests, 1)
        self.assertEqual(allocation.room_id, room.pk)
        self.assertEqual(booking.total_amount, Decimal("180.00") * 1 * 3)
        self.assertEqual(booking.guest.phone, "0821110001")

    def test_valid_group_booking(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)

        response = self.client.post(
            reverse("core:room_add_shared_guest", args=[room.pk]),
            self.base_payload(
                room,
                booking_purpose="group",
                new_first_name="Sipho",
                new_last_name="Payer",
                allocated_guests="3",
            ),
        )

        booking = Booking.objects.get(guest__first_name="Sipho")
        self.assertRedirects(
            response, reverse("core:room_add_shared_guest_success", args=[booking.pk])
        )
        # One payer, one booking, one invoice, several allocated spaces.
        self.assertEqual(booking.room_allocations.count(), 1)
        self.assertEqual(booking.room_allocations.get().allocated_guests, 3)
        self.assertEqual(booking.num_guests, 3)
        self.assertEqual(Booking.objects.filter(guest__first_name="Sipho").count(), 1)
        self.assertEqual(booking.total_amount, Decimal("180.00") * 3 * 3)

    def test_separately_paying_guest_creates_separate_invoice(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)

        # Guest A, already staying, pays their own balance in full.
        resp_a = self.client.post(
            reverse("core:room_add_shared_guest", args=[room.pk]),
            self.base_payload(
                room, new_first_name="Ada", new_last_name="First",
                collect_payment="on", payment_amount="540.00",
            ),
        )
        booking_a = Booking.objects.get(guest__first_name="Ada")
        self.assertRedirects(resp_a, reverse("core:room_add_shared_guest_success", args=[booking_a.pk]))

        # Guest B books independently into the same room/dates, pays nothing yet.
        resp_b = self.client.post(
            reverse("core:room_add_shared_guest", args=[room.pk]),
            self.base_payload(room, new_first_name="Ben", new_last_name="Second"),
        )
        booking_b = Booking.objects.get(guest__first_name="Ben")
        self.assertRedirects(resp_b, reverse("core:room_add_shared_guest_success", args=[booking_b.pk]))

        self.assertNotEqual(booking_a.pk, booking_b.pk)
        self.assertNotEqual(booking_a.booking_reference, booking_b.booking_reference)
        self.assertEqual(booking_a.balance_due, Decimal("0.00"))
        self.assertEqual(booking_b.balance_due, Decimal("540.00"))
        self.assertEqual(booking_a.payments.count(), 1)
        self.assertEqual(booking_b.payments.count(), 0)

        resp_invoice_a = self.client.get(reverse("core:booking_invoice_pdf", args=[booking_a.pk]))
        resp_invoice_b = self.client.get(reverse("core:booking_invoice_pdf", args=[booking_b.pk]))
        text_a = extract_reportlab_pdf_text(resp_invoice_a.content)
        text_b = extract_reportlab_pdf_text(resp_invoice_b.content)
        self.assertIn(booking_a.booking_reference.encode(), text_a)
        self.assertIn(booking_b.booking_reference.encode(), text_b)
        self.assertNotIn(booking_b.booking_reference.encode(), text_a)
        self.assertNotIn(booking_a.booking_reference.encode(), text_b)

    def test_capacity_changed_before_submission(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        # Another staff member fills the room to capacity between when this
        # guest's form was opened and when it's actually submitted.
        create_individual_shared_room_booking(
            guest=make_guest(first_name="Filler", phone="0821110099"),
            room=room, check_in=self.d(10), check_out=self.d(13), allocated_guests=7,
        )

        response = self.client.post(
            reverse("core:room_add_shared_guest", args=[room.pk]),
            self.base_payload(room, new_first_name="Latecomer"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "only has 0 of 1")
        self.assertFalse(Booking.objects.filter(guest__first_name="Latecomer").exists())
        # Entered guest details must survive the validation error.
        self.assertContains(response, "Latecomer")

    def test_invalid_dates_rejected_and_form_preserved(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)

        response = self.client.post(
            reverse("core:room_add_shared_guest", args=[room.pk]),
            self.base_payload(
                room, new_first_name="Baduser",
                check_in_date=self.d(13).isoformat(), check_out_date=self.d(10).isoformat(),
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Check-out date must be after check-in date.")
        self.assertFalse(Booking.objects.filter(guest__first_name="Baduser").exists())
        self.assertContains(response, "Baduser")

    def test_payment_validation_rejects_invalid_amount(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)

        response = self.client.post(
            reverse("core:room_add_shared_guest", args=[room.pk]),
            self.base_payload(
                room, new_first_name="Payless",
                collect_payment="on", payment_amount="-50.00",
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter a valid payment amount")
        self.assertFalse(Booking.objects.filter(guest__first_name="Payless").exists())
        self.assertContains(response, "Payless")

    def test_cross_tenant_room_injection_returns_404(self):
        enable_shared_capacity()
        make_room("Home Filler")

        with schema_context("public"):
            other_tenant = GuestHouseTenant(
                schema_name="walkin_shared_other",
                name="Other Guest House",
                owner_name="Other Owner",
                owner_email="walkin-shared-other@example.com",
                owner_phone="0830000097",
                is_active=True,
                is_verified=True,
            )
            other_tenant.save()
            Domain.objects.create(domain="walkin-shared-other.test.com", tenant=other_tenant, is_primary=True)
        try:
            with tenant_context(other_tenant):
                for _ in range(5):
                    make_room(f"Filler {_}")
                foreign_room = make_shared_room("Foreign Shared Room", price=180, max_guests=7)
                foreign_room_id = foreign_room.pk

            self.assertFalse(
                Room.objects.filter(pk=foreign_room_id).exists(),
                "test setup invalid: foreign room id coincidentally exists locally too",
            )

            response = self.client.post(
                reverse("core:room_add_shared_guest", args=[foreign_room_id]),
                self.base_payload_for_missing_room(),
            )
            self.assertEqual(response.status_code, 404)
            self.assertEqual(Booking.objects.count(), 0)
        finally:
            with schema_context("public"):
                other_tenant.delete(allow_hard_delete=True)

    def base_payload_for_missing_room(self):
        return {
            "booking_purpose": "individual",
            "identity_mode": "new_guest",
            "new_first_name": "Intruder",
            "new_last_name": "Guest",
            "new_phone": "0821110098",
            "check_in_date": self.d(10).isoformat(),
            "check_out_date": self.d(13).isoformat(),
            "allocated_guests": "1",
            "booking_source": "Walk-in",
        }

    def test_feature_disabled_returns_404(self):
        # Flag left at its default False.
        room = make_room("Room 02", price=180)
        room.booking_mode = "SHARED_CAPACITY"
        room.max_guests = 7
        room.pricing_model = "per_person"
        room.save()

        get_response = self.client.get(reverse("core:room_add_shared_guest", args=[room.pk]))
        self.assertEqual(get_response.status_code, 404)

        post_response = self.client.post(
            reverse("core:room_add_shared_guest", args=[room.pk]),
            self.base_payload(room, new_first_name="Blocked"),
        )
        self.assertEqual(post_response.status_code, 404)
        self.assertFalse(Booking.objects.filter(guest__first_name="Blocked").exists())

    def test_success_screen_shows_required_fields_and_actions(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)

        response = self.client.post(
            reverse("core:room_add_shared_guest", args=[room.pk]),
            self.base_payload(room, new_first_name="Successful", new_last_name="Guest"),
            follow=True,
        )

        booking = Booking.objects.get(guest__first_name="Successful")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, booking.booking_reference)
        self.assertContains(response, "Successful Guest")
        self.assertContains(response, room.name)
        self.assertContains(response, "View Booking")
        self.assertContains(response, "Record Payment")
        self.assertContains(response, "Print Invoice")
        self.assertContains(response, "Check In Guest")
        self.assertContains(response, "Return to Room")


class SharedRoomFinancialIsolationTest(CircleCoreTenantTestCase):
    """
    Every independently paying guest in a shared-capacity room must have a
    fully independent financial account: own invoice, own payment history,
    own balance — never combined with, or leaked to, another occupant.
    """

    def setUp(self):
        self.owner = make_owner()
        activate_trial(self.owner)
        self.client.login(username="owner", password="testpass123")
        self.today = timezone.localdate()

    def d(self, offset):
        return self.today + datetime.timedelta(days=offset)

    def test_two_guests_one_room_separate_invoices(self):
        # Exactly the example from the spec: Guest B, 1 guest x 2 nights x
        # R180 PPPN = R360, added alongside an already-present Guest A.
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest_a = make_guest(first_name="GuestA", last_name="Existing", phone="0816660001")
        booking_a = create_individual_shared_room_booking(
            guest=guest_a, room=room, check_in=self.d(20), check_out=self.d(23), allocated_guests=1,
        )
        guest_b = make_guest(first_name="GuestB", last_name="New", phone="0816660002")
        booking_b = create_individual_shared_room_booking(
            guest=guest_b, room=room, check_in=self.d(20), check_out=self.d(22), allocated_guests=1,
        )

        self.assertEqual(booking_b.total_amount, Decimal("360.00"))
        self.assertNotEqual(booking_a.booking_reference, booking_b.booking_reference)
        self.assertEqual(Booking.objects.filter(room_allocations__room=room).distinct().count(), 2)

        resp_b = self.client.get(reverse("core:booking_invoice_pdf", args=[booking_b.pk]))
        self.assertEqual(resp_b.status_code, 200)
        text_b = extract_reportlab_pdf_text(resp_b.content).decode("latin-1")
        self.assertIn(f"INV-{booking_b.booking_reference}", text_b)
        self.assertIn("1 guest", text_b)
        self.assertIn("2 nights", text_b)
        self.assertIn("R 180.00 PPPN", text_b)
        self.assertIn("360.00", text_b)

        # Guest A's own invoice, payments and balance are untouched by
        # Guest B's booking ever having been created.
        booking_a.refresh_from_db()
        self.assertEqual(booking_a.total_amount, Decimal("180.00") * 3)
        self.assertEqual(booking_a.balance_due, Decimal("180.00") * 3)

    def test_payment_on_one_invoice_does_not_affect_the_other(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest_a = make_guest(first_name="Ada", phone="0816660003")
        guest_b = make_guest(first_name="Ben", phone="0816660004")
        ci, co = self.d(30), self.d(32)
        booking_a = create_individual_shared_room_booking(guest=guest_a, room=room, check_in=ci, check_out=co, allocated_guests=1)
        booking_b = create_individual_shared_room_booking(guest=guest_b, room=room, check_in=ci, check_out=co, allocated_guests=1)

        Payment.objects.create(booking=booking_b, amount=Decimal("360.00"), payment_method="Cash", payment_type="Payment")

        booking_a.refresh_from_db()
        booking_b.refresh_from_db()
        self.assertEqual(booking_a.payments.count(), 0)
        self.assertEqual(booking_a.balance_due, Decimal("360.00"))
        self.assertEqual(booking_b.payments.count(), 1)
        self.assertEqual(booking_b.balance_due, Decimal("0.00"))

    def test_partial_payments(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest = make_guest(first_name="Partial", phone="0816660005")
        booking = create_individual_shared_room_booking(
            guest=guest, room=room, check_in=self.d(40), check_out=self.d(42), allocated_guests=1,
        )
        self.assertEqual(booking.total_amount, Decimal("360.00"))
        self.assertEqual(booking.balance_due, Decimal("360.00"))

        Payment.objects.create(booking=booking, amount=Decimal("100.00"), payment_method="Cash", payment_type="Payment")
        booking.refresh_from_db()
        self.assertEqual(booking.balance_due, Decimal("260.00"))

        Payment.objects.create(booking=booking, amount=Decimal("260.00"), payment_method="Card", payment_type="Payment")
        booking.refresh_from_db()
        self.assertEqual(booking.balance_due, Decimal("0.00"))
        self.assertEqual(booking.payments.count(), 2)
        self.assertEqual(
            set(booking.payments.values_list("payment_method", flat=True)), {"Cash", "Card"},
        )

    def test_unpaid_booking_has_full_balance(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest = make_guest(first_name="Unpaid", phone="0816660006")
        booking = create_individual_shared_room_booking(
            guest=guest, room=room, check_in=self.d(50), check_out=self.d(52), allocated_guests=1,
        )
        self.assertEqual(booking.payments.count(), 0)
        self.assertEqual(booking.balance_due, booking.total_amount)

    def test_historical_rate_preservation(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest = make_guest(first_name="Historical", phone="0816660007")
        booking = create_individual_shared_room_booking(
            guest=guest, room=room, check_in=self.d(60), check_out=self.d(63), allocated_guests=1,
        )
        self.assertEqual(booking.rate_per_night, Decimal("180.00"))
        original_total = booking.total_amount

        # The room's configured rate changes after the booking exists.
        room.price_per_night = Decimal("220.00")
        room.save()

        booking.refresh_from_db()
        self.assertEqual(booking.rate_per_night, Decimal("180.00"))
        self.assertEqual(booking.total_amount, original_total)
        self.assertEqual(booking.room_allocations.get().rate_per_night, Decimal("180.00"))

        response = self.client.get(reverse("core:booking_invoice_pdf", args=[booking.pk]))
        text = extract_reportlab_pdf_text(response.content).decode("latin-1")
        self.assertIn("180.00", text)
        self.assertNotIn("220.00", text)

    def test_invoice_privacy_no_leakage_of_other_occupants_or_internal_warnings(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        room.internal_notes = "Capacity Pending Confirmation - awaiting final headcount from owner"
        room_type = RoomType.objects.create(name="Pending Bathroom Type", bathroom_type="pending")
        room.room_category = room_type
        room.save()

        guest_a = make_guest(first_name="Confidential", last_name="OccupantA", phone="0816660008")
        booking_a = create_individual_shared_room_booking(
            guest=guest_a, room=room, check_in=self.d(70), check_out=self.d(72), allocated_guests=1,
        )
        guest_b = make_guest(first_name="Requesting", last_name="OccupantB", phone="0816660009")
        booking_b = create_individual_shared_room_booking(
            guest=guest_b, room=room, check_in=self.d(70), check_out=self.d(72), allocated_guests=1,
        )

        response = self.client.get(reverse("core:booking_invoice_pdf", args=[booking_b.pk]))
        self.assertEqual(response.status_code, 200)
        text = extract_reportlab_pdf_text(response.content).decode("latin-1")

        self.assertIn(booking_b.booking_reference, text)
        self.assertIn("Requesting", text)

        # Nothing about the other occupant, the room's total capacity, or any
        # internal staff-only warning may appear on a guest-facing invoice.
        self.assertNotIn("Confidential", text)
        self.assertNotIn(booking_a.booking_reference, text)
        self.assertNotIn("Capacity Pending", text)
        self.assertNotIn("Bathroom", text)
        self.assertNotIn("occupied", text.lower())
        self.assertNotIn("capacity", text.lower())

    def test_revenue_totals_and_occupancy_do_not_double_count_shared_room(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest_a = make_guest(first_name="Rev", last_name="GuestA", phone="0816660010")
        guest_b = make_guest(first_name="Rev", last_name="GuestB", phone="0816660011")
        # Same 2 nights, same room, two independent guests, both checked in
        # (checked-in/out bookings are what the reports revenue query counts).
        ci, co = self.today, self.today + datetime.timedelta(days=2)
        booking_a = create_individual_shared_room_booking(guest=guest_a, room=room, check_in=ci, check_out=co, allocated_guests=1, status="Checked In")
        booking_b = create_individual_shared_room_booking(guest=guest_b, room=room, check_in=ci, check_out=co, allocated_guests=1, status="Checked In")
        Payment.objects.create(booking=booking_a, amount=Decimal("360.00"), payment_method="Cash", payment_type="Payment", payment_date=self.today)
        Payment.objects.create(booking=booking_b, amount=Decimal("200.00"), payment_method="Card", payment_type="Payment", payment_date=self.today)

        response = self.client.get(reverse("core:reports"))
        self.assertEqual(response.status_code, 200)

        # Revenue sums every individual booking's payments normally.
        self.assertEqual(response.context["total_revenue"], Decimal("560.00"))

        # Occupancy must count 2 physically-occupied room-nights (one room,
        # two nights) — NOT 4, which a naive per-booking sum would produce
        # by mistaking two simultaneous bookings for two separate rooms.
        self.assertEqual(response.context["room_nights_sold"], 2)

    def test_cancellation_and_refund_isolated_per_guest(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest_a = make_guest(first_name="Stays", phone="0816660012")
        guest_b = make_guest(first_name="Cancels", phone="0816660013")
        ci, co = self.d(80), self.d(82)
        booking_a = create_individual_shared_room_booking(guest=guest_a, room=room, check_in=ci, check_out=co, allocated_guests=1)
        booking_b = create_individual_shared_room_booking(guest=guest_b, room=room, check_in=ci, check_out=co, allocated_guests=1)
        Payment.objects.create(booking=booking_a, amount=Decimal("360.00"), payment_method="Cash", payment_type="Payment")
        Payment.objects.create(booking=booking_b, amount=Decimal("360.00"), payment_method="Cash", payment_type="Payment")

        # Cancel guest B's booking — guest A's booking/payment/balance must
        # be completely unaffected.
        cancel_multi_room_booking(booking_b)
        booking_a.refresh_from_db()
        booking_b.refresh_from_db()
        self.assertEqual(booking_a.status, "Confirmed")
        self.assertEqual(booking_a.balance_due, Decimal("0.00"))
        self.assertEqual(booking_a.payments.count(), 1)
        self.assertEqual(booking_b.status, "Cancelled")

        # Refund guest B's payment — guest A's balance/payments untouched.
        BookingRefund.objects.create(
            booking=booking_b, amount=Decimal("360.00"), refund_method="Cash",
            reason="Guest cancelled", recorded_by=self.owner,
        )
        booking_a.refresh_from_db()
        booking_b.refresh_from_db()
        self.assertEqual(booking_a.balance_due, Decimal("0.00"))
        self.assertEqual(booking_a.payments.count(), 1)
        self.assertEqual(booking_a.refunds.count(), 0)
        self.assertEqual(booking_b.balance_due, Decimal("360.00"))
        self.assertEqual(booking_b.refunds.count(), 1)


class RoomDetailMultiOccupantInterfaceTest(CircleCoreTenantTestCase):
    """
    /rooms/<pk>/ for a SHARED_CAPACITY room: staff must be able to see and
    manage every currently active occupant independently, with future
    reservations kept in a clearly separate section.
    """

    def setUp(self):
        self.owner = make_owner()
        activate_trial(self.owner)
        self.client.login(username="owner", password="testpass123")
        self.today = timezone.localdate()

    def d(self, offset):
        return self.today + datetime.timedelta(days=offset)

    def test_one_active_occupant(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest = make_guest(first_name="Solo", phone="0817770001")
        booking = create_individual_shared_room_booking(
            guest=guest, room=room, check_in=self.d(10), check_out=self.d(12),
            allocated_guests=1, status="Checked In",
        )

        response = self.client.get(reverse("core:room_detail", args=[room.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Current Occupants")
        self.assertContains(response, "Solo")
        self.assertContains(response, booking.booking_reference)
        self.assertContains(response, "Check Out")
        self.assertContains(response, "View Booking")
        self.assertContains(response, "Record Payment")
        self.assertContains(response, "Print Invoice")
        self.assertContains(response, "Extend Stay")
        self.assertContains(response, "Move Room")

    def test_several_active_occupants(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest_a = make_guest(first_name="First", phone="0817770002")
        guest_b = make_guest(first_name="Second", phone="0817770003")
        guest_c = make_guest(first_name="Third", phone="0817770004")
        ci, co = self.today, self.d(2)
        booking_a = create_individual_shared_room_booking(guest=guest_a, room=room, check_in=ci, check_out=co, allocated_guests=1, status="Checked In")
        booking_b = create_individual_shared_room_booking(guest=guest_b, room=room, check_in=ci, check_out=co, allocated_guests=2, status="Checked In")
        booking_c = create_individual_shared_room_booking(guest=guest_c, room=room, check_in=ci, check_out=co, allocated_guests=1, status="Checked In")

        response = self.client.get(reverse("core:room_detail", args=[room.pk]))

        self.assertEqual(response.status_code, 200)
        for name in ("First", "Second", "Third"):
            self.assertContains(response, name)
        for booking in (booking_a, booking_b, booking_c):
            self.assertContains(response, booking.booking_reference)
        self.assertContains(response, "4 / 7")  # shared_occupied total across all three
        self.assertEqual(response.content.count(b"Check Out"), 3)

    def test_occupants_with_different_checkout_dates(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest_a = make_guest(first_name="ShortStay", phone="0817770005")
        guest_b = make_guest(first_name="LongStay", phone="0817770006")
        ci = self.d(30)
        booking_a = create_individual_shared_room_booking(guest=guest_a, room=room, check_in=ci, check_out=ci + datetime.timedelta(days=1), allocated_guests=1, status="Checked In")
        booking_b = create_individual_shared_room_booking(guest=guest_b, room=room, check_in=ci, check_out=ci + datetime.timedelta(days=5), allocated_guests=1, status="Checked In")

        response = self.client.get(reverse("core:room_detail", args=[room.pk]))

        self.assertContains(response, booking_a.check_out_date.strftime("%d %b %Y"))
        self.assertContains(response, booking_b.check_out_date.strftime("%d %b %Y"))
        self.assertNotEqual(booking_a.check_out_date, booking_b.check_out_date)

    def test_separate_balances_displayed(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest_a = make_guest(first_name="PaidUp", phone="0817770007")
        guest_b = make_guest(first_name="Owing", phone="0817770008")
        ci, co = self.d(40), self.d(42)
        booking_a = create_individual_shared_room_booking(
            guest=guest_a, room=room, check_in=ci, check_out=co, allocated_guests=1, status="Checked In",
            payment_info={"amount": Decimal("360.00"), "payment_method": "Cash", "payment_type": "Payment"},
        )
        booking_b = create_individual_shared_room_booking(
            guest=guest_b, room=room, check_in=ci, check_out=co, allocated_guests=1, status="Checked In",
        )

        response = self.client.get(reverse("core:room_detail", args=[room.pk]))
        content = response.content.decode()

        self.assertIn("PaidUp", content)
        self.assertIn("Owing", content)
        # Guest A's own balance is 0.00, Guest B's own balance is the full 360.00 —
        # both figures appear, each tied to its own occupant, never combined.
        self.assertEqual(booking_a.balance_due, Decimal("0.00"))
        self.assertEqual(booking_b.balance_due, Decimal("360.00"))
        self.assertIn("360.00", content)
        self.assertIn("0.00", content)

    def test_full_room_shows_full(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest = make_guest(first_name="FillsRoom", phone="0817770009")
        create_individual_shared_room_booking(
            guest=guest, room=room, check_in=self.today, check_out=self.d(2), allocated_guests=7, status="Checked In",
        )

        response = self.client.get(reverse("core:room_detail", args=[room.pk]))

        self.assertContains(response, "Full")
        self.assertContains(response, "7 / 7")
        self.assertNotContains(response, "Check In Guest")

    def test_partially_occupied_room(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest = make_guest(first_name="PartialOccupant", phone="0817770010")
        create_individual_shared_room_booking(
            guest=guest, room=room, check_in=self.today, check_out=self.d(2), allocated_guests=1, status="Checked In",
        )

        response = self.client.get(reverse("core:room_detail", args=[room.pk]))

        self.assertContains(response, "Add Another Guest")
        self.assertContains(response, "1 of 7 spaces occupied")
        self.assertContains(response, "6 spaces remaining")
        self.assertNotContains(response, "Room Full")

    def test_future_bookings_shown_separately_from_current_occupants(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        current_guest = make_guest(first_name="HereNow", phone="0817770011")
        create_individual_shared_room_booking(
            guest=current_guest, room=room, check_in=self.today, check_out=self.d(2), allocated_guests=1, status="Checked In",
        )
        future_guest = make_guest(first_name="ArrivesLater", phone="0817770012")
        future_booking = create_individual_shared_room_booking(
            guest=future_guest, room=room, check_in=self.d(30), check_out=self.d(33), allocated_guests=1, status="Confirmed",
        )

        response = self.client.get(reverse("core:room_detail", args=[room.pk]))
        content = response.content.decode()

        self.assertContains(response, "Future Bookings")
        self.assertIn("HereNow", content)
        self.assertIn("ArrivesLater", content)
        self.assertIn(future_booking.booking_reference, content)
        # The future reservation must not appear inside the Current Occupants
        # roster — it hasn't checked in, so it must not show a Check Out action.
        occupants_section = content.split("Future Bookings")[0]
        self.assertNotIn("ArrivesLater", occupants_section)

    def test_feature_disabled_tenant_no_multi_occupant_interface(self):
        # Flag left at its default False, even though the room is internally
        # marked SHARED_CAPACITY — must present exactly like a whole room.
        room = make_room("Room 02", price=180)
        room.booking_mode = "SHARED_CAPACITY"
        room.max_guests = 7
        room.pricing_model = "per_person"
        room.save()
        guest = make_guest(first_name="Whole", phone="0817770013")
        booking = make_booking(room, guest)
        booking.status = "Checked In"
        booking.check_in_time = timezone.now()
        booking.save()

        response = self.client.get(reverse("core:room_detail", args=[room.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Current Occupants")
        self.assertNotContains(response, "Future Bookings")
        self.assertNotContains(response, "Add Another Guest")
        self.assertContains(response, "Guest Checked In")

    def test_whole_room_inventory_retains_existing_presentation(self):
        # No shared-capacity involvement at all: existing single-occupant
        # presentation must be completely unchanged.
        enable_shared_capacity()  # tenant flag on, but this specific room is WHOLE_ROOM
        room = make_room("Whole Room 18", price=260)
        guest = make_guest(first_name="Classic", phone="0817770014")
        booking = make_booking(room, guest)
        booking.status = "Checked In"
        booking.check_in_time = timezone.now()
        booking.save()
        room.status = "Occupied"
        room.save()

        response = self.client.get(reverse("core:room_detail", args=[room.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Guest Checked In")
        self.assertContains(response, "Classic")
        self.assertContains(response, booking.booking_reference)
        self.assertNotContains(response, "Current Occupants")
        self.assertNotContains(response, "Future Bookings")
        self.assertNotContains(response, "Add Another Guest")
        self.assertNotContains(response, "Extend Stay")
        self.assertNotContains(response, "Move Room")

    def test_extend_stay_changes_only_this_occupant(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest_a = make_guest(first_name="Extends", phone="0817770015")
        guest_b = make_guest(first_name="Unaffected", phone="0817770016")
        ci, co = self.today, self.d(2)
        booking_a = create_individual_shared_room_booking(guest=guest_a, room=room, check_in=ci, check_out=co, allocated_guests=1, status="Checked In")
        booking_b = create_individual_shared_room_booking(guest=guest_b, room=room, check_in=ci, check_out=co, allocated_guests=1, status="Checked In")

        new_check_out = self.d(5)
        response = self.client.post(
            reverse("core:booking_extend_stay", args=[booking_a.pk]),
            {"check_out_date": new_check_out.isoformat()},
        )

        self.assertRedirects(response, reverse("core:room_detail", args=[room.pk]))
        booking_a.refresh_from_db()
        booking_b.refresh_from_db()
        self.assertEqual(booking_a.check_out_date, new_check_out)
        self.assertEqual(booking_b.check_out_date, co)  # untouched

    def test_move_room_relocates_only_this_occupant(self):
        enable_shared_capacity()
        room_a = make_shared_room("Room 02", price=180, max_guests=7)
        room_b = make_shared_room("Room 03", price=180, max_guests=7)
        guest_moving = make_guest(first_name="Moving", phone="0817770017")
        guest_staying = make_guest(first_name="Staying", phone="0817770018")
        ci, co = self.today, self.d(2)
        booking_moving = create_individual_shared_room_booking(guest=guest_moving, room=room_a, check_in=ci, check_out=co, allocated_guests=1, status="Checked In")
        booking_staying = create_individual_shared_room_booking(guest=guest_staying, room=room_a, check_in=ci, check_out=co, allocated_guests=1, status="Checked In")

        response = self.client.post(
            reverse("core:booking_move_room", args=[booking_moving.pk]),
            {"new_room_id": str(room_b.pk)},
        )

        self.assertRedirects(response, reverse("core:room_detail", args=[room_b.pk]))
        booking_moving.refresh_from_db()
        booking_staying.refresh_from_db()
        self.assertEqual(booking_moving.room_allocations.get().room_id, room_b.pk)
        # The guest who stayed behind is completely unaffected.
        self.assertEqual(booking_staying.room_allocations.get().room_id, room_a.pk)
        self.assertEqual(booking_staying.status, "Checked In")
        # Room A now has only one occupant remaining, room B has one new occupant.
        self.assertEqual(occupancy_snapshot(room_a, self.today), (1, 7, 6))
        self.assertEqual(occupancy_snapshot(room_b, self.today), (1, 7, 6))


class SharedRoomCheckInCheckoutBehaviorTest(CircleCoreTenantTestCase):
    """
    Check-in/check-out/extend/move/cancel for one occupant of a
    SHARED_CAPACITY room must never disturb any other occupant's booking,
    invoice, payments, or the room's own in-service status.
    """

    def setUp(self):
        self.owner = make_owner()
        activate_trial(self.owner)
        self.client.login(username="owner", password="testpass123")
        self.today = timezone.localdate()

    def d(self, offset):
        return self.today + datetime.timedelta(days=offset)

    def test_independent_check_in(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest_a = make_guest(first_name="Alpha", phone="0818880001")
        guest_b = make_guest(first_name="Beta", phone="0818880002")
        ci, co = self.today, self.d(2)

        booking_a = create_individual_shared_room_booking(guest=guest_a, room=room, check_in=ci, check_out=co, allocated_guests=1, status="Checked In")
        booking_b = create_individual_shared_room_booking(guest=guest_b, room=room, check_in=ci, check_out=co, allocated_guests=1, status="Confirmed")
        before_a = _model_snapshot_for_test(booking_a)

        updated_b = check_in_multi_room_booking(booking_b)

        self.assertEqual(updated_b.status, "Checked In")
        booking_a.refresh_from_db()
        self.assertEqual(_model_snapshot_for_test(booking_a), before_a)
        self.assertEqual(booking_a.payments.count(), 0)

        # Occupied capacity reflects both guests' spaces (Confirmed already
        # counted before check-in — check-in changes this booking's status
        # and the room's own display status, not the capacity math).
        self.assertEqual(occupancy_snapshot(room, self.today), (2, 7, 5))

        room.refresh_from_db()
        self.assertEqual(room.status, "Occupied")
        # The room must not be "exclusively occupied" — a third, independent
        # guest can still book into the remaining spaces.
        guest_c = make_guest(first_name="Gamma", phone="0818880003")
        booking_c = create_individual_shared_room_booking(guest=guest_c, room=room, check_in=ci, check_out=co, allocated_guests=1)
        self.assertIsNotNone(booking_c.pk)

    def test_independent_checkout_matches_worked_example(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest_a = make_guest(first_name="Alpha", phone="0818880004")
        guest_b = make_guest(first_name="Beta", phone="0818880005")
        ci, co = self.today, self.d(2)
        booking_a = create_individual_shared_room_booking(guest=guest_a, room=room, check_in=ci, check_out=co, allocated_guests=1, status="Checked In")
        booking_b = create_individual_shared_room_booking(guest=guest_b, room=room, check_in=ci, check_out=co, allocated_guests=1, status="Checked In")
        self.assertEqual(occupancy_snapshot(room, self.today), (2, 7, 5))
        before_b = _model_snapshot_for_test(booking_b)

        updated_a = check_out_multi_room_booking(booking_a, staff_user=self.owner)

        self.assertEqual(updated_a.status, "Checked Out")
        booking_b.refresh_from_db()
        self.assertEqual(booking_b.status, "Checked In")
        self.assertEqual(_model_snapshot_for_test(booking_b), before_b)

        occupied, cap, remaining = occupancy_snapshot(room, self.today)
        self.assertEqual((occupied, cap, remaining), (1, 7, 6))
        room.refresh_from_db()
        self.assertEqual(shared_room_status_label(occupied, cap, room.status), "Partially Occupied")

    def test_other_occupants_remain_room_stays_in_service_with_housekeeping_note(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest_a = make_guest(first_name="Alpha", phone="0818880006")
        guest_b = make_guest(first_name="Beta", phone="0818880007")
        ci, co = self.today, self.d(2)
        booking_a = create_individual_shared_room_booking(guest=guest_a, room=room, check_in=ci, check_out=co, allocated_guests=1, status="Confirmed")
        booking_b = create_individual_shared_room_booking(guest=guest_b, room=room, check_in=ci, check_out=co, allocated_guests=1, status="Confirmed")
        # Go through the real check-in transition for both, so the room's
        # own status is genuinely "Occupied" before either of them leaves.
        check_in_multi_room_booking(booking_a)
        check_in_multi_room_booking(booking_b)
        room.refresh_from_db()
        room_status_before = room.status
        self.assertEqual(room_status_before, "Occupied")
        log_count_before = AuditLog.objects.count()

        check_out_multi_room_booking(booking_a, staff_user=self.owner)

        room.refresh_from_db()
        # Not taken out of inventory: never flipped to Available or Cleaning
        # just because one of several occupants left.
        self.assertNotEqual(room.status, "Available")
        self.assertNotEqual(room.status, "Cleaning")
        self.assertEqual(room.status, room_status_before)
        self.assertEqual(room.cleaning_status, "Clean")

        # A staff-only housekeeping note was recorded instead, since this app
        # has no per-bed/per-space cleaning model.
        self.assertEqual(AuditLog.objects.count(), log_count_before + 1)
        log = AuditLog.objects.filter(object_type="Room", object_id=str(room.pk)).order_by("-created_at").first()
        self.assertIsNotNone(log)
        self.assertIn("partial", log.reason.lower())
        self.assertIn("turnover", log.reason.lower())
        self.assertEqual(log.after["remaining_occupants"], 1)

    def test_final_checkout_triggers_existing_cleaning_behaviour(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest = make_guest(first_name="LastOne", phone="0818880008")
        booking = create_individual_shared_room_booking(guest=guest, room=room, check_in=self.today, check_out=self.d(2), allocated_guests=1, status="Checked In")

        check_out_multi_room_booking(booking, staff_user=self.owner)

        room.refresh_from_db()
        self.assertEqual(room.status, "Cleaning")
        self.assertEqual(room.cleaning_status, "Needs Cleaning")
        self.assertEqual(occupancy_snapshot(room, self.today), (0, 7, 7))

    def test_extension_succeeds_without_altering_other_guests(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest_a = make_guest(first_name="Extending", phone="0818880009")
        guest_b = make_guest(first_name="Bystander", phone="0818880010")
        ci, co = self.today, self.d(2)
        booking_a = create_individual_shared_room_booking(guest=guest_a, room=room, check_in=ci, check_out=co, allocated_guests=1, status="Checked In")
        booking_b = create_individual_shared_room_booking(guest=guest_b, room=room, check_in=ci, check_out=co, allocated_guests=1, status="Checked In")
        before_b = _model_snapshot_for_test(booking_b)

        new_check_out = self.d(5)
        updated_a = edit_multi_room_booking(booking_a, check_out=new_check_out)

        self.assertEqual(updated_a.check_out_date, new_check_out)
        booking_b.refresh_from_db()
        self.assertEqual(_model_snapshot_for_test(booking_b), before_b)

    def test_extension_fails_due_to_future_capacity(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest_a = make_guest(first_name="WantsExtend", phone="0818880011")
        ci, co = self.today, self.d(2)
        booking_a = create_individual_shared_room_booking(guest=guest_a, room=room, check_in=ci, check_out=co, allocated_guests=1, status="Checked In")

        # Someone else fills the room completely for the nights the
        # extension would need — booking_a's own current allocation is
        # excluded from this capacity check, so it must be genuinely full
        # (7 of 7), not just nearly full, to actually block the extension.
        guest_filler = make_guest(first_name="FutureFiller", phone="0818880012")
        create_individual_shared_room_booking(guest=guest_filler, room=room, check_in=self.d(2), check_out=self.d(5), allocated_guests=7, status="Confirmed")

        with self.assertRaises(ValidationError) as ctx:
            edit_multi_room_booking(booking_a, check_out=self.d(4))
        self.assertTrue(any("only has" in msg for msg in ctx.exception.messages), ctx.exception.messages)

        booking_a.refresh_from_db()
        self.assertEqual(booking_a.check_out_date, co)  # unchanged

    def test_room_move_preserves_booking_and_financial_history(self):
        enable_shared_capacity()
        room_a = make_shared_room("Room 02", price=180, max_guests=7)
        room_b = make_shared_room("Room 03", price=180, max_guests=7)
        guest = make_guest(first_name="Mover", phone="0818880013")
        ci, co = self.today, self.d(2)
        booking = create_individual_shared_room_booking(guest=guest, room=room_a, check_in=ci, check_out=co, allocated_guests=1, status="Checked In")
        Payment.objects.create(booking=booking, amount=Decimal("100.00"), payment_method="Cash", payment_type="Payment")
        original_reference = booking.booking_reference
        original_payment_count = booking.payments.count()

        updated = edit_multi_room_booking(booking, allocations=[{"room": room_b, "allocated_guests": booking.num_guests}])

        self.assertEqual(updated.pk, booking.pk)
        self.assertEqual(updated.booking_reference, original_reference)
        self.assertEqual(updated.payments.count(), original_payment_count)
        self.assertEqual(updated.room_allocations.get().room_id, room_b.pk)
        self.assertEqual(occupancy_snapshot(room_a, self.today), (0, 7, 7))
        self.assertEqual(occupancy_snapshot(room_b, self.today), (1, 7, 6))

    def test_cancellation_releases_only_that_bookings_allocation(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest_a = make_guest(first_name="Cancels", phone="0818880014")
        guest_b = make_guest(first_name="Remains", phone="0818880015")
        ci, co = self.today, self.d(2)
        booking_a = create_individual_shared_room_booking(guest=guest_a, room=room, check_in=ci, check_out=co, allocated_guests=1, status="Checked In")
        booking_b = create_individual_shared_room_booking(guest=guest_b, room=room, check_in=ci, check_out=co, allocated_guests=1, status="Checked In")
        before_b = _model_snapshot_for_test(booking_b)

        cancel_multi_room_booking(booking_a)

        booking_a.refresh_from_db()
        booking_b.refresh_from_db()
        self.assertEqual(booking_a.status, "Cancelled")
        self.assertEqual(booking_b.status, "Checked In")
        self.assertEqual(_model_snapshot_for_test(booking_b), before_b)
        self.assertEqual(occupancy_snapshot(room, self.today), (1, 7, 6))

    def test_no_cross_tenant_effects(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest_a = make_guest(first_name="Local", phone="0818880016")
        guest_b = make_guest(first_name="LocalTwo", phone="0818880017")
        ci, co = self.today, self.d(2)
        booking_a = create_individual_shared_room_booking(guest=guest_a, room=room, check_in=ci, check_out=co, allocated_guests=1, status="Checked In")
        booking_b = create_individual_shared_room_booking(guest=guest_b, room=room, check_in=ci, check_out=co, allocated_guests=1, status="Checked In")

        with schema_context("public"):
            other_tenant = GuestHouseTenant(
                schema_name="checkin_checkout_other",
                name="Other Guest House",
                owner_name="Other Owner",
                owner_email="checkin-checkout-other@example.com",
                owner_phone="0830000096",
                is_active=True,
                is_verified=True,
            )
            other_tenant.save()
            Domain.objects.create(domain="checkin-checkout-other.test.com", tenant=other_tenant, is_primary=True)
        try:
            with tenant_context(other_tenant):
                enable_shared_capacity()
                other_room = make_shared_room("Room 02", price=180, max_guests=7)
                other_guest = make_guest(first_name="Foreign", phone="0818880018")
                other_booking = create_individual_shared_room_booking(
                    guest=other_guest, room=other_room, check_in=ci, check_out=co, allocated_guests=1, status="Checked In",
                )
                other_before = _model_snapshot_for_test(other_booking)

            # All local operations against the local tenant's room/bookings.
            check_out_multi_room_booking(booking_a, staff_user=self.owner)
            edit_multi_room_booking(booking_b, check_out=self.d(5))

            with tenant_context(other_tenant):
                other_booking.refresh_from_db()
                self.assertEqual(_model_snapshot_for_test(other_booking), other_before)
                self.assertEqual(occupancy_snapshot(other_room, self.today), (1, 7, 6))
        finally:
            with schema_context("public"):
                other_tenant.delete(allow_hard_delete=True)


class SharedRoomAvailabilityCalendarDisplayTest(CircleCoreTenantTestCase):
    """
    /availability/ and /calendar/ for SHARED_CAPACITY rooms: occupancy must
    be shown as capacity used, never as one exclusive booking block, and a
    room must only ever be unavailable for a real operational reason — never
    merely because someone is already booked into it.
    """

    def setUp(self):
        self.owner = make_owner()
        activate_trial(self.owner)
        self.client.login(username="owner", password="testpass123")
        self.today = timezone.localdate()

    def d(self, offset):
        return self.today + datetime.timedelta(days=offset)

    def test_one_occupant_shown_as_capacity_used_not_exclusive_block(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest = make_guest(first_name="Alpha", phone="0819990001")
        make_reserving_booking(room, guest, self.d(1), self.d(3), num_guests=2)

        response = self.client.get(reverse("core:room_calendar"))
        self.assertContains(response, "2 / 7 occupied")

        avail = self.client.get(reverse("core:availability"), {
            "check_in": self.d(1).isoformat(), "check_out": self.d(3).isoformat(),
        })
        # One guest already booked must not make the room unavailable —
        # it still has 5 of 7 spaces free.
        self.assertContains(avail, "Room 02")
        self.assertContains(avail, "5 space")
        self.assertNotContains(avail, "Unavailable Rooms")

    def test_multiple_occupants_all_contribute_to_total(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest_a = make_guest(first_name="Alpha", phone="0819990002")
        guest_b = make_guest(first_name="Bravo", phone="0819990003")
        guest_c = make_guest(first_name="Charlie", phone="0819990004")
        make_reserving_booking(room, guest_a, self.d(1), self.d(3), num_guests=2)
        make_reserving_booking(room, guest_b, self.d(1), self.d(3), num_guests=1)
        make_reserving_booking(room, guest_c, self.d(1), self.d(3), num_guests=1)

        response = self.client.get(reverse("core:room_calendar"))
        self.assertContains(response, "4 / 7 occupied")
        # Staff must be able to inspect every contributing booking, not just one.
        self.assertContains(response, "Alpha")
        self.assertContains(response, "Bravo")
        self.assertContains(response, "Charlie")

    def test_full_capacity_shown_as_full(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest = make_guest(first_name="FillsIt", phone="0819990005")
        make_reserving_booking(room, guest, self.d(1), self.d(3), num_guests=7)

        response = self.client.get(reverse("core:room_calendar"))
        self.assertContains(response, "7 / 7 full")

        avail = self.client.get(reverse("core:availability"), {
            "check_in": self.d(1).isoformat(), "check_out": self.d(3).isoformat(),
        })
        self.assertContains(avail, "Unavailable Rooms")
        self.assertContains(avail, "Room 02")

    def test_varying_occupancy_by_night(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest_a = make_guest(first_name="Short", phone="0819990006")
        guest_b = make_guest(first_name="Long", phone="0819990007")
        # 2 spaces on day 1 only, 4 more spaces days 1-2 -> day1=6, day2=4.
        make_reserving_booking(room, guest_a, self.d(1), self.d(2), num_guests=2)
        make_reserving_booking(room, guest_b, self.d(1), self.d(3), num_guests=4)

        response = self.client.get(reverse("core:room_calendar"))
        content = response.content.decode()
        self.assertIn("6 / 7 occupied", content)
        self.assertIn("4 / 7 occupied", content)

        # The availability search over both nights is bound by the busier
        # night (day 1: 6/7 occupied, 1 remaining) — not the quieter one.
        avail = self.client.get(reverse("core:availability"), {
            "check_in": self.d(1).isoformat(), "check_out": self.d(3).isoformat(),
        })
        self.assertContains(avail, "1 space available")

    def test_adjacent_checkout_check_in_no_gap_or_double_count(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest_leaving = make_guest(first_name="Leaving", phone="0819990008")
        guest_arriving = make_guest(first_name="Arriving", phone="0819990009")
        # Leaving guest's stay ends exactly when the arriving guest's stay
        # starts (checkout day == check-in day) — the room must not be
        # double-booked, and remaining capacity on the changeover day must
        # only reflect whoever is actually still there that night.
        make_reserving_booking(room, guest_leaving, self.d(1), self.d(3), num_guests=3, status="Checked In")
        booking_arriving = create_individual_shared_room_booking(
            guest=guest_arriving, room=room, check_in=self.d(3), check_out=self.d(5), allocated_guests=2,
        )

        self.assertIsNotNone(booking_arriving.pk)
        # On the changeover night (day 3), only the arriving guest occupies
        # the room — the leaving guest's stay ended that morning.
        self.assertEqual(occupancy_snapshot(room, self.d(3)), (2, 7, 5))
        # On day 1-2, only the leaving guest's 3 spaces count.
        self.assertEqual(occupancy_snapshot(room, self.d(1)), (3, 7, 4))

    def test_out_of_service_room_shown_unavailable_regardless_of_capacity(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        room.status = "Maintenance"
        room.save()
        # No bookings at all — plenty of raw capacity — but the room is
        # still out of service and must not be offered.

        avail = self.client.get(reverse("core:availability"), {
            "check_in": self.d(1).isoformat(), "check_out": self.d(3).isoformat(),
        })
        self.assertContains(avail, "Unavailable Rooms")
        content = avail.content.decode()
        self.assertIn("Room 02", content)
        self.assertIn("maintenance", content.lower())

    def test_tenant_isolation_on_availability_and_calendar(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest = make_guest(first_name="Local", phone="0819990010")
        make_reserving_booking(room, guest, self.d(1), self.d(3), num_guests=3)

        with schema_context("public"):
            other_tenant = GuestHouseTenant(
                schema_name="avail_calendar_other",
                name="Other Guest House",
                owner_name="Other Owner",
                owner_email="avail-calendar-other@example.com",
                owner_phone="0830000095",
                is_active=True,
                is_verified=True,
            )
            other_tenant.save()
            Domain.objects.create(domain="avail-calendar-other.test.com", tenant=other_tenant, is_primary=True)
        settings.ALLOWED_HOSTS = list(settings.ALLOWED_HOSTS) + ["avail-calendar-other.test.com"]
        try:
            with tenant_context(other_tenant):
                enable_shared_capacity()
                other_room = make_shared_room("Room 02", price=180, max_guests=7)
                other_guest = make_guest(first_name="Foreign", phone="0819990011")
                make_reserving_booking(other_room, other_guest, self.d(1), self.d(3), num_guests=7)  # full
                other_owner = make_owner(username="other_owner_avail")
                activate_trial(other_owner)
                other_client = TenantClient(HTTP_HOST="avail-calendar-other.test.com")
                other_client.login(username="other_owner_avail", password="testpass123")
                other_response = other_client.get(reverse("core:room_calendar"))
                self.assertContains(other_response, "7 / 7 full")

            # Back in this tenant's own schema, its room is unaffected —
            # still only 3/7 occupied, not full.
            response = self.client.get(reverse("core:room_calendar"))
            self.assertContains(response, "3 / 7 occupied")
            self.assertNotContains(response, "7 / 7 full")
        finally:
            settings.ALLOWED_HOSTS.remove("avail-calendar-other.test.com")
            with schema_context("public"):
                other_tenant.delete(allow_hard_delete=True)

    def test_feature_disabled_tenant_shows_plain_whole_room_screens(self):
        # Flag left at its default False, even though the room is internally
        # marked SHARED_CAPACITY.
        room = make_room("Room 02", price=180)
        room.booking_mode = "SHARED_CAPACITY"
        room.max_guests = 7
        room.pricing_model = "per_person"
        room.save()
        guest = make_guest(first_name="Solo", phone="0819990012")
        make_reserving_booking(room, guest, self.d(1), self.d(3), num_guests=1)

        calendar_response = self.client.get(reverse("core:room_calendar"))
        self.assertEqual(calendar_response.status_code, 200)
        self.assertNotContains(calendar_response, "occupied")
        self.assertNotContains(calendar_response, "/ 7")

        avail = self.client.get(reverse("core:availability"), {
            "check_in": self.d(1).isoformat(), "check_out": self.d(3).isoformat(),
        })
        self.assertContains(avail, "Whole room")
        self.assertNotContains(avail, "Shared capacity")
        self.assertNotContains(avail, "spaces available")

    def test_guest_facing_confirmation_does_not_expose_other_occupants(self):
        # CALENDAR PRIVACY: a guest-facing document must never reveal who
        # else is staying in the same shared room.
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest_a = make_guest(first_name="Confidential", last_name="Neighbour", phone="0819990013")
        booking_a = create_individual_shared_room_booking(guest=guest_a, room=room, check_in=self.d(1), check_out=self.d(3), allocated_guests=1)
        guest_b = make_guest(first_name="Requesting", last_name="Party", phone="0819990014")
        booking_b = create_individual_shared_room_booking(guest=guest_b, room=room, check_in=self.d(1), check_out=self.d(3), allocated_guests=1)

        response = self.client.get(reverse("core:booking_confirmation_pdf", args=[booking_b.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Confidential", response.content)
        self.assertNotIn(booking_a.booking_reference.encode(), response.content)


class IndividualGuestBillingRegressionSecurityConcurrencyTest(CircleCoreTenantTestCase):
    """
    End-to-end regression + security + concurrency verification for
    tenant-gated shared-capacity individual guest billing, scripted exactly
    against the Room 02 / capacity 7 scenario used to sign off this feature.
    """

    def setUp(self):
        self.owner = make_owner()
        activate_trial(self.owner)
        self.client.login(username="owner", password="testpass123")
        self.today = timezone.localdate()
        self.schema_name = connection.schema_name

    def d(self, offset):
        return self.today + datetime.timedelta(days=offset)

    # ---- REQUIRED SCENARIO: fill Room 02 to 7/7, then reject an 8th ----

    def test_full_scenario_fill_room_to_capacity_and_reject_overflow(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        ci, co = self.today, self.d(2)  # 2 nights

        # 1. Guest A: 1 space, 2 nights, separate booking + invoice.
        guest_a = make_guest(first_name="GuestA", phone="0820000001")
        booking_a = create_individual_shared_room_booking(guest=guest_a, room=room, check_in=ci, check_out=co, allocated_guests=1, staff_user=self.owner)
        self.assertEqual(booking_a.total_amount, Decimal("360.00"))
        self.assertEqual(occupancy_snapshot(room, ci), (1, 7, 6))

        # 2. Guest B: same room, overlapping dates, 1 space, separate booking + invoice.
        guest_b = make_guest(first_name="GuestB", phone="0820000002")
        booking_b = create_individual_shared_room_booking(guest=guest_b, room=room, check_in=ci, check_out=co, allocated_guests=1, staff_user=self.owner)
        self.assertNotEqual(booking_a.pk, booking_b.pk)
        self.assertNotEqual(booking_a.booking_reference, booking_b.booking_reference)

        # Confirm: occupancy 2/7, both active, A unchanged, B has its own invoice, payments separate.
        self.assertEqual(occupancy_snapshot(room, ci), (2, 7, 5))
        booking_a.refresh_from_db()
        self.assertEqual(booking_a.status, "Confirmed")
        self.assertEqual(booking_b.status, "Confirmed")
        self.assertEqual(booking_a.total_amount, Decimal("360.00"))  # unchanged
        resp_a = self.client.get(reverse("core:booking_invoice_pdf", args=[booking_a.pk]))
        resp_b = self.client.get(reverse("core:booking_invoice_pdf", args=[booking_b.pk]))
        text_a = extract_reportlab_pdf_text(resp_a.content)
        text_b = extract_reportlab_pdf_text(resp_b.content)
        self.assertIn(booking_a.booking_reference.encode(), text_a)
        self.assertIn(booking_b.booking_reference.encode(), text_b)
        self.assertNotIn(booking_b.booking_reference.encode(), text_a)
        self.assertNotIn(booking_a.booking_reference.encode(), text_b)
        self.assertEqual(booking_a.payments.count(), 0)
        self.assertEqual(booking_b.payments.count(), 0)

        # 3. Continue creating one-space bookings until 7/7.
        bookings = [booking_a, booking_b]
        for i in range(3, 8):  # guests C..G fill spaces 3..7
            guest = make_guest(first_name=f"Guest{i}", phone=f"082000000{i}")
            booking = create_individual_shared_room_booking(guest=guest, room=room, check_in=ci, check_out=co, allocated_guests=1, staff_user=self.owner)
            bookings.append(booking)

        self.assertEqual(len(bookings), 7)
        self.assertEqual(len({b.pk for b in bookings}), 7)  # every booking is distinct
        self.assertEqual(len({b.booking_reference for b in bookings}), 7)  # every reference is distinct
        occupied, cap, remaining = occupancy_snapshot(room, ci)
        self.assertEqual((occupied, cap, remaining), (7, 7, 0))
        room.refresh_from_db()
        self.assertEqual(shared_room_status_label(occupied, cap, room.status), "Full")

        # Every guest has its own invoice.
        for booking in bookings:
            resp = self.client.get(reverse("core:booking_invoice_pdf", args=[booking.pk]))
            self.assertEqual(resp.status_code, 200)
            self.assertIn(booking.booking_reference.encode(), extract_reportlab_pdf_text(resp.content))

        # 4. Attempt an 8th overlapping one-space booking — must be rejected,
        # and must create nothing at all (no booking, no allocation, no payment).
        booking_count_before = Booking.objects.count()
        allocation_count_before = RoomAllocation.objects.count()
        payment_count_before = Payment.objects.count()
        guest_h = make_guest(first_name="GuestH", phone="0820000008")
        with self.assertRaises(ValidationError) as ctx:
            create_individual_shared_room_booking(
                guest=guest_h, room=room, check_in=ci, check_out=co, allocated_guests=1,
                payment_info={"amount": Decimal("360.00"), "payment_method": "Cash", "payment_type": "Payment"},
                staff_user=self.owner,
            )
        self.assertTrue(any("only has 0 of 1" in msg for msg in ctx.exception.messages), ctx.exception.messages)
        self.assertEqual(Booking.objects.count(), booking_count_before)
        self.assertEqual(RoomAllocation.objects.count(), allocation_count_before)
        self.assertEqual(Payment.objects.count(), payment_count_before)
        self.assertFalse(Booking.objects.filter(guest=guest_h).exists())

        return bookings  # handed to other test methods via helper below

    def _fill_room_to_capacity(self, room, ci, co):
        bookings = []
        for i in range(1, 8):
            guest = make_guest(first_name=f"Fill{i}", phone=f"082111100{i}")
            bookings.append(create_individual_shared_room_booking(guest=guest, room=room, check_in=ci, check_out=co, allocated_guests=1, staff_user=self.owner))
        return bookings

    # ---- Different occupancy on different nights ----

    def test_room_may_have_different_occupancy_on_different_nights(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest_a = make_guest(first_name="Short", phone="0820000101")
        guest_b = make_guest(first_name="Long", phone="0820000102")
        create_individual_shared_room_booking(guest=guest_a, room=room, check_in=self.d(1), check_out=self.d(2), allocated_guests=3, staff_user=self.owner)
        create_individual_shared_room_booking(guest=guest_b, room=room, check_in=self.d(1), check_out=self.d(4), allocated_guests=2, staff_user=self.owner)

        self.assertEqual(occupancy_snapshot(room, self.d(1)), (5, 7, 2))  # both present
        self.assertEqual(occupancy_snapshot(room, self.d(2)), (2, 7, 5))  # only the longer stay
        self.assertEqual(occupancy_snapshot(room, self.d(3)), (2, 7, 5))

    # ---- Checkout releases exactly one space, others remain ----

    def test_checkout_releases_one_space_others_remain_active(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        ci, co = self.today, self.d(2)
        bookings = []
        for i in range(1, 8):
            guest = make_guest(first_name=f"Full{i}", phone=f"082222200{i}")
            booking = create_individual_shared_room_booking(guest=guest, room=room, check_in=ci, check_out=co, allocated_guests=1, status="Confirmed", staff_user=self.owner)
            bookings.append(check_in_multi_room_booking(booking))

        self.assertEqual(occupancy_snapshot(room, self.today), (7, 7, 0))
        room.refresh_from_db()
        self.assertEqual(room.status, "Occupied")

        checking_out = bookings[0]
        remaining_bookings = bookings[1:]
        check_out_multi_room_booking(checking_out, staff_user=self.owner)

        occupied, cap, remaining = occupancy_snapshot(room, self.today)
        self.assertEqual((occupied, cap, remaining), (6, 7, 1))
        # One new space is bookable now.
        new_guest = make_guest(first_name="NewArrival", phone="0820000199")
        new_booking = create_individual_shared_room_booking(guest=new_guest, room=room, check_in=ci, check_out=co, allocated_guests=1, staff_user=self.owner)
        self.assertIsNotNone(new_booking.pk)
        self.assertEqual(occupancy_snapshot(room, self.today), (7, 7, 0))
        # All the other original guests are still active, untouched.
        for booking in remaining_bookings:
            booking.refresh_from_db()
            self.assertEqual(booking.status, "Checked In")

    # ---- Payments remain independent ----

    def test_payments_remain_independent_full_partial_unpaid(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        ci, co = self.today, self.d(2)
        guest_a = make_guest(first_name="FullyPaid", phone="0820000201")
        guest_b = make_guest(first_name="PartlyPaid", phone="0820000202")
        guest_c = make_guest(first_name="Unpaid", phone="0820000203")
        booking_a = create_individual_shared_room_booking(guest=guest_a, room=room, check_in=ci, check_out=co, allocated_guests=1, staff_user=self.owner)
        booking_b = create_individual_shared_room_booking(guest=guest_b, room=room, check_in=ci, check_out=co, allocated_guests=1, staff_user=self.owner)
        booking_c = create_individual_shared_room_booking(guest=guest_c, room=room, check_in=ci, check_out=co, allocated_guests=1, staff_user=self.owner)

        Payment.objects.create(booking=booking_a, amount=Decimal("360.00"), payment_method="Cash", payment_type="Payment")
        Payment.objects.create(booking=booking_b, amount=Decimal("150.00"), payment_method="Card", payment_type="Payment")

        booking_a.refresh_from_db()
        booking_b.refresh_from_db()
        booking_c.refresh_from_db()
        self.assertEqual(booking_a.balance_due, Decimal("0.00"))
        self.assertEqual(booking_b.balance_due, Decimal("210.00"))
        self.assertEqual(booking_c.balance_due, Decimal("360.00"))
        self.assertEqual(booking_a.payments.count(), 1)
        self.assertEqual(booking_b.payments.count(), 1)
        self.assertEqual(booking_c.payments.count(), 0)

    # ---- SECURITY TESTS ----

    def test_security_cross_tenant_room_submission(self):
        enable_shared_capacity()
        make_room("Home Filler")
        guest = make_guest(phone="0820000301")

        with schema_context("public"):
            other_tenant = GuestHouseTenant(
                schema_name="regression_sec_other", name="Other Guest House", owner_name="Other Owner",
                owner_email="regression-sec-other@example.com", owner_phone="0830000094",
                is_active=True, is_verified=True,
            )
            other_tenant.save()
            Domain.objects.create(domain="regression-sec-other.test.com", tenant=other_tenant, is_primary=True)
        try:
            with tenant_context(other_tenant):
                for _ in range(5):
                    make_room(f"Filler {_}")
                foreign_room = make_shared_room("Foreign Room", price=180, max_guests=7)
                foreign_room_id = foreign_room.pk
            self.assertFalse(Room.objects.filter(pk=foreign_room_id).exists())

            with self.assertRaises(ValidationError):
                create_individual_shared_room_booking(
                    guest=guest, room=foreign_room, check_in=self.d(10), check_out=self.d(12),
                    allocated_guests=1, staff_user=self.owner,
                )
            self.assertEqual(Booking.objects.filter(guest=guest).count(), 0)
        finally:
            with schema_context("public"):
                other_tenant.delete(allow_hard_delete=True)

    def test_security_manual_guest_count_manipulation(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest = make_guest(phone="0820000302")

        for bad_value in (0, -1, -999):
            with self.assertRaises(ValidationError):
                create_individual_shared_room_booking(
                    guest=guest, room=room, check_in=self.d(10), check_out=self.d(12),
                    allocated_guests=bad_value, staff_user=self.owner,
                )
        # A grossly inflated allocation (beyond max_guests) must still be
        # rejected as a normal capacity failure, not silently truncated.
        with self.assertRaises(ValidationError) as ctx:
            create_individual_shared_room_booking(
                guest=guest, room=room, check_in=self.d(10), check_out=self.d(12),
                allocated_guests=999, staff_user=self.owner,
            )
        self.assertTrue(any("only has" in msg for msg in ctx.exception.messages), ctx.exception.messages)
        self.assertEqual(Booking.objects.filter(guest=guest).count(), 0)

    def test_security_stale_availability_submission_rejected_fresh(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        ci, co = self.d(20), self.d(22)
        create_individual_shared_room_booking(guest=make_guest(phone="0820000303"), room=room, check_in=ci, check_out=co, allocated_guests=7, staff_user=self.owner)

        # A staff member's browser loaded the add-guest form when it still
        # had capacity, but submits after the room silently filled up.
        response = self.client.post(
            reverse("core:room_add_shared_guest", args=[room.pk]),
            {
                "booking_purpose": "individual", "identity_mode": "new_guest",
                "new_first_name": "Stale", "new_last_name": "Client", "new_phone": "0820000304",
                "check_in_date": ci.isoformat(), "check_out_date": co.isoformat(),
                "allocated_guests": "1", "booking_source": "Walk-in",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "only has 0 of 1")
        self.assertFalse(Booking.objects.filter(guest__first_name="Stale").exists())

    def test_security_duplicate_allocation_submission_rejected(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        guest = make_guest(phone="0820000305")

        response = self.client.post(reverse("core:group_booking_add"), {
            "identity_mode": "guest",
            "guest": str(guest.pk),
            "check_in_date": self.d(30).isoformat(),
            "check_out_date": self.d(32).isoformat(),
            "room_id": [str(room.pk), str(room.pk)],  # same room submitted twice
            "allocated_guests": ["1", "1"],
            "total_guests": "2",
            "discount": "0.00",
            "booking_source": "Walk-in",
            "status": "Confirmed",
            "notes": "",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "listed more than once")
        self.assertEqual(Booking.objects.filter(guest=guest).count(), 0)

    def test_security_direct_service_overbooking_rejected(self):
        # "Direct API" in this codebase means calling the booking service
        # directly, bypassing every UI form — the authoritative boundary
        # must hold even then.
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        create_individual_shared_room_booking(guest=make_guest(phone="0820000306"), room=room, check_in=self.d(40), check_out=self.d(42), allocated_guests=7, staff_user=self.owner)

        with self.assertRaises(ValidationError):
            create_individual_shared_room_booking(
                guest=make_guest(phone="0820000307"), room=room, check_in=self.d(40), check_out=self.d(42),
                allocated_guests=1, staff_user=self.owner,
            )
        self.assertEqual(occupancy_snapshot(room, self.d(40)), (7, 7, 0))

    def test_security_unauthorised_room_mode_change_blocked_for_anonymous_and_cleaner(self):
        enable_shared_capacity()
        room = make_room("Room 02", price=180)

        anon_client = TenantClient()
        anon_response = anon_client.post(reverse("core:room_edit", args=[room.pk]), {"booking_mode": "SHARED_CAPACITY"})
        self.assertIn(anon_response.status_code, (302, 403))
        room.refresh_from_db()
        self.assertEqual(room.booking_mode, "WHOLE_ROOM")

        cleaner = make_owner(username="cleaner_user")
        activate_trial(cleaner)
        assign_role(cleaner, "Cleaner")
        cleaner_client = TenantClient()
        cleaner_client.login(username="cleaner_user", password="testpass123")
        cleaner_response = cleaner_client.post(reverse("core:room_edit", args=[room.pk]), {"booking_mode": "SHARED_CAPACITY"})
        room.refresh_from_db()
        self.assertEqual(room.booking_mode, "WHOLE_ROOM")
        self.assertNotEqual(cleaner_response.status_code, 200)

    def test_security_unauthorised_feature_enablement_has_no_effect(self):
        # shared_capacity_booking_enabled is deliberately absent from
        # GuestHouseSettingsForm — there is no web form field for it at all,
        # so even a forged POST field is silently dropped by Django's
        # ModelForm (only listed fields are ever bound to the instance).
        settings_obj, _ = GuestHouseSettings.objects.get_or_create(pk=1)
        self.assertFalse(settings_obj.shared_capacity_booking_enabled)

        response = self.client.post(reverse("core:settings"), {
            "guest_house_name": "My Guest House",
            "currency": "R",
            "check_in_time": "14:00",
            "check_out_time": "10:00",
            "shared_capacity_booking_enabled": "true",  # forged/unexpected field
        })
        self.assertIn(response.status_code, (200, 302))
        settings_obj.refresh_from_db()
        self.assertFalse(settings_obj.shared_capacity_booking_enabled)

    # ---- CONCURRENCY ----
    # (Also see IndividualSharedRoomBookingConcurrencyTest /
    # BookingTransactionConcurrencyTest for the multi-threaded harness this
    # reuses; kept here too so this class stands alone as the sign-off suite.)

    # (Concurrency is verified in IndividualGuestBillingConcurrencySignOffTest
    # below — it needs a TransactionTestCase so a second thread's own DB
    # connection can actually see committed state, which a plain TestCase's
    # savepoint-wrapped writes never expose across threads.)

    # ---- REGRESSION ----

    def test_regression_whole_room_tenant_unaffected_blocks_after_one_booking(self):
        enable_shared_capacity()  # tenant flag on, but this room is WHOLE_ROOM
        room = make_room("Whole Room 18", price=260)
        guest_a = make_guest(first_name="First", phone="0820000501")
        guest_b = make_guest(first_name="Second", phone="0820000502")
        make_booking(room, guest_a, days_ahead=60, nights=2)

        with self.assertRaises(ValidationError):
            from core.booking_transactions import create_multi_room_booking
            create_multi_room_booking(
                guest=guest_b, prop=room.prop,
                check_in=self.d(60), check_out=self.d(62),
                allocations=[{"room": room, "allocated_guests": 1}], total_guests=1, status="Confirmed",
            )
        self.assertEqual(Booking.objects.filter(guest=guest_b).count(), 0)

    def test_regression_existing_invoices_and_bookings_remain_accessible(self):
        room = make_room("Classic Room", price=300)
        guest = make_guest(first_name="Preexisting", phone="0820000503")
        booking = make_booking(room, guest, days_ahead=70, nights=2)

        response = self.client.get(reverse("core:booking_detail", args=[booking.pk]))
        self.assertEqual(response.status_code, 200)
        invoice_response = self.client.get(reverse("core:booking_invoice_pdf", args=[booking.pk]))
        self.assertEqual(invoice_response.status_code, 200)

    def test_regression_reports_remain_accurate_with_shared_and_whole_rooms_mixed(self):
        enable_shared_capacity()
        shared_room = make_shared_room("Room 02", price=180, max_guests=7)
        whole_room = make_room("Whole Room 07", price=260)
        g1 = make_guest(first_name="Rep1", phone="0820000601")
        g2 = make_guest(first_name="Rep2", phone="0820000602")
        b1 = create_individual_shared_room_booking(guest=g1, room=shared_room, check_in=self.today, check_out=self.d(2), allocated_guests=1, status="Checked In", staff_user=self.owner)
        b2 = create_individual_shared_room_booking(guest=g2, room=shared_room, check_in=self.today, check_out=self.d(2), allocated_guests=1, status="Checked In", staff_user=self.owner)
        whole_booking = make_booking(whole_room, make_guest(first_name="Rep3", phone="0820000603"), days_ahead=0, nights=2)
        whole_booking.status = "Checked In"
        whole_booking.check_in_time = timezone.now()
        whole_booking.save()
        Payment.objects.create(booking=b1, amount=Decimal("360.00"), payment_method="Cash", payment_type="Payment", payment_date=self.today)
        Payment.objects.create(booking=b2, amount=Decimal("180.00"), payment_method="Card", payment_type="Payment", payment_date=self.today)
        Payment.objects.create(booking=whole_booking, amount=Decimal("520.00"), payment_method="Cash", payment_type="Payment", payment_date=self.today)

        response = self.client.get(reverse("core:reports"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_revenue"], Decimal("1060.00"))
        # 2 guests on the same shared-room nights = 2 physical room-nights,
        # plus the whole room's own 2 nights = 4 total, never inflated to 6.
        self.assertEqual(response.context["room_nights_sold"], 4)

    def test_regression_internal_notes_remain_staff_only(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        marker = "STAFF-ONLY-INTERNAL-MARKER-42"
        room.internal_notes = marker
        room.save()
        guest = make_guest(phone="0820000701")
        booking = create_individual_shared_room_booking(guest=guest, room=room, check_in=self.d(80), check_out=self.d(82), allocated_guests=1, staff_user=self.owner)

        # Staff-facing screens may reference internal_notes (already covered
        # elsewhere); the guest-facing invoice/confirmation must never.
        invoice_response = self.client.get(reverse("core:booking_invoice_pdf", args=[booking.pk]))
        confirmation_response = self.client.get(reverse("core:booking_confirmation_pdf", args=[booking.pk]))
        self.assertNotIn(marker.encode(), invoice_response.content)
        invoice_text = extract_reportlab_pdf_text(invoice_response.content)
        self.assertNotIn(marker.encode(), invoice_text)
        self.assertNotIn(marker.encode(), confirmation_response.content)


class IndividualGuestBillingConcurrencySignOffTest(ConcurrencyTenantTestCase):
    """Sign-off concurrency check: two staff members racing for the final
    available space in Room 02 — only one may ever win."""

    def setUp(self):
        self.today = timezone.localdate()
        self.schema_name = connection.schema_name

    def d(self, offset):
        return self.today + datetime.timedelta(days=offset)

    def test_concurrency_two_requests_for_final_space_only_one_succeeds(self):
        enable_shared_capacity()
        room = make_shared_room("Room 02", price=180, max_guests=7)
        create_individual_shared_room_booking(
            guest=make_guest(first_name="Filler", phone="0820000401"),
            room=room, check_in=self.d(50), check_out=self.d(52), allocated_guests=6,
        )
        guest_x = make_guest(first_name="RequestX", phone="0820000402")
        guest_y = make_guest(first_name="RequestY", phone="0820000403")
        ci, co = self.d(50), self.d(52)

        def attempt(guest):
            def _run():
                return create_individual_shared_room_booking(guest=guest, room=room, check_in=ci, check_out=co, allocated_guests=1)
            return _run

        results = run_concurrently(self.schema_name, [attempt(guest_x), attempt(guest_y)])
        successes = [r for r, e in results if e is None]
        failures = [e for r, e in results if e is not None]
        self.assertEqual(len(successes), 1, f"expected exactly 1 success, got {results}")
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], ValidationError)
        with schema_context(self.schema_name):
            self.assertEqual(occupancy_snapshot(room, ci), (7, 7, 0))
