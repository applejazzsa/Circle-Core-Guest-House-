"""
Regression coverage for the removal of GET-triggered booking mutations.

Previously, _expire_elapsed_bookings() ran real, unaudited database writes
from inside eight page views (home, room_list, room_detail, cleaning_board,
availability, booking_list, booking_detail, housekeeping_mobile) every time
staff merely browsed the app. That function is now gone entirely: pages only
compute a read-only "overdue" indicator (core.availability.is_booking_overdue),
and the only thing allowed to actually transition an elapsed booking is the
new `expire_elapsed_bookings` management command, run hourly from cron.

This file covers:
  - GET safety: loading every one of the eight affected pages must not change
    any Booking/Room/invoice/payment field.
  - The command itself: dry-run vs apply, idempotency, audit trail, future/
    already-terminal bookings left alone, shared-capacity-aware checkout,
    no-show processing, tenant iteration (public schema excluded, one
    tenant's failure isolated from another), and concurrency safety.
  - The related outstanding-balance reporting fix in home()/reports(): a
    departed guest's genuine unpaid debt must remain visible, and a page GET
    must never make it disappear.
"""

import datetime
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.urls import reverse
from django.utils import timezone
from django_tenants.utils import schema_context

import core.management.commands.expire_elapsed_bookings as expire_cmd
from core.availability import is_booking_overdue, occupancy_snapshot
from core.models import AuditLog, Booking, GuestHouseSettings, Payment, RoomAllocation
from core.tests import (
    CircleCoreTenantTestCase,
    ConcurrencyTenantTestCase,
    activate_trial,
    enable_shared_capacity,
    make_guest,
    make_owner,
    make_room,
    make_shared_room,
    run_concurrently,
)
from tenants.models import GuestHouseTenant


def _make_elapsed_booking(room, guest, status, num_guests=1, days_overdue=1):
    """A booking whose stay ended `days_overdue` day(s) ago — always overdue
    under the default GuestHouseSettings.check_out_time (10:00)."""
    check_out_date = timezone.localdate() - datetime.timedelta(days=days_overdue)
    check_in_date = check_out_date - datetime.timedelta(days=2)
    nights = 2
    guest_multiplier = num_guests if room.pricing_model == "per_person" else 1
    amount = room.price_per_night * guest_multiplier * nights
    booking = Booking.objects.create(
        room=room,
        guest=guest,
        check_in_date=check_in_date,
        check_out_date=check_out_date,
        booking_duration_type="daily",
        num_guests=num_guests,
        rate_per_night=room.price_per_night,
        status=status,
        booking_source="Walk-in",
        balance_due=amount,
        total_amount=amount,
    )
    if status == "Checked In":
        booking.check_in_time = timezone.now() - datetime.timedelta(days=days_overdue + 2)
        booking.save(update_fields=["check_in_time"])
    if room.effective_booking_mode == "SHARED_CAPACITY":
        RoomAllocation.objects.create(
            booking=booking,
            room=room,
            allocated_guests=num_guests,
            rate_per_night=room.price_per_night,
            line_total=amount,
        )
    return booking


def _make_future_booking(room, guest, status="Confirmed"):
    check_in_date = timezone.localdate() + datetime.timedelta(days=10)
    check_out_date = check_in_date + datetime.timedelta(days=2)
    return Booking.objects.create(
        room=room,
        guest=guest,
        check_in_date=check_in_date,
        check_out_date=check_out_date,
        booking_duration_type="daily",
        num_guests=1,
        rate_per_night=room.price_per_night,
        status=status,
        booking_source="Walk-in",
        balance_due=room.price_per_night * 2,
        total_amount=room.price_per_night * 2,
    )


# ── Phase 1: pure overdue calculation ──────────────────────────────────────


class IsBookingOverdueUnitTest(CircleCoreTenantTestCase):
    def setUp(self):
        self.settings_obj = GuestHouseSettings.objects.create(pk=1)
        self.room = make_room()
        self.guest = make_guest()

    def test_future_checked_in_booking_is_not_overdue(self):
        booking = _make_future_booking(self.room, self.guest, status="Checked In")
        info = is_booking_overdue(booking, self.settings_obj)
        self.assertFalse(info.overdue)

    def test_elapsed_checked_in_booking_suggests_checked_out(self):
        booking = _make_elapsed_booking(self.room, self.guest, status="Checked In")
        info = is_booking_overdue(booking, self.settings_obj)
        self.assertTrue(info.overdue)
        self.assertEqual(info.suggested_transition, "Checked Out")
        self.assertEqual(info.display_label, "Checkout overdue")
        self.assertIsNotNone(info.overdue_since)

    def test_elapsed_confirmed_booking_suggests_no_show(self):
        booking = _make_elapsed_booking(self.room, self.guest, status="Confirmed")
        info = is_booking_overdue(booking, self.settings_obj)
        self.assertTrue(info.overdue)
        self.assertEqual(info.suggested_transition, "No Show")

    def test_elapsed_pending_booking_suggests_no_show(self):
        booking = _make_elapsed_booking(self.room, self.guest, status="Pending")
        info = is_booking_overdue(booking, self.settings_obj)
        self.assertTrue(info.overdue)
        self.assertEqual(info.suggested_transition, "No Show")

    def test_terminal_statuses_are_never_overdue_even_if_dates_elapsed(self):
        for status in ("Cancelled", "No Show", "Checked Out"):
            booking = _make_elapsed_booking(self.room, self.guest, status=status)
            info = is_booking_overdue(booking, self.settings_obj)
            self.assertFalse(info.overdue, f"status={status} should never be flagged overdue")

    def test_is_booking_overdue_writes_nothing(self):
        booking = _make_elapsed_booking(self.room, self.guest, status="Checked In")
        before_status = booking.status
        is_booking_overdue(booking, self.settings_obj)
        booking.refresh_from_db()
        self.assertEqual(booking.status, before_status)


# ── Phase 2: GET requests must never mutate ────────────────────────────────


class ExpireElapsedBookingsGetSafetyTest(CircleCoreTenantTestCase):
    def setUp(self):
        self.owner = make_owner()
        activate_trial(self.owner)
        self.client.login(username="owner", password="testpass123")
        self.room = make_room("Overdue Room")
        self.guest = make_guest()
        self.booking = _make_elapsed_booking(self.room, self.guest, status="Checked In")

    def _snapshot(self):
        self.booking.refresh_from_db()
        self.room.refresh_from_db()
        return {
            "booking_status": self.booking.status,
            "check_out_time": self.booking.check_out_time,
            "balance_due": self.booking.balance_due,
            "room_status": self.room.status,
            "room_cleaning_status": self.room.cleaning_status,
        }

    def _assert_unchanged(self, before, url):
        response = self.client.get(url)
        self.assertIn(response.status_code, (200, 302))
        after = self._snapshot()
        self.assertEqual(before, after, f"GET {url} mutated booking/room state: {before} -> {after}")

    def test_home_does_not_mutate(self):
        before = self._snapshot()
        self._assert_unchanged(before, reverse("core:home"))

    def test_room_list_does_not_mutate(self):
        before = self._snapshot()
        self._assert_unchanged(before, reverse("core:room_list"))

    def test_room_detail_does_not_mutate(self):
        before = self._snapshot()
        self._assert_unchanged(before, reverse("core:room_detail", args=[self.room.pk]))

    def test_cleaning_board_does_not_mutate(self):
        before = self._snapshot()
        self._assert_unchanged(before, reverse("core:cleaning"))

    def test_availability_does_not_mutate(self):
        before = self._snapshot()
        self._assert_unchanged(before, reverse("core:availability"))

    def test_booking_list_does_not_mutate(self):
        before = self._snapshot()
        self._assert_unchanged(before, reverse("core:booking_list"))

    def test_booking_detail_does_not_mutate(self):
        before = self._snapshot()
        self._assert_unchanged(before, reverse("core:booking_detail", args=[self.booking.pk]))

    def test_housekeeping_mobile_does_not_mutate(self):
        before = self._snapshot()
        self._assert_unchanged(before, reverse("core:housekeeping_mobile"))

    def test_overdue_confirmed_booking_is_not_marked_no_show_by_any_get(self):
        other_room = make_room("Second Overdue Room")
        confirmed = _make_elapsed_booking(other_room, make_guest(first_name="Second", phone="0810000099"), status="Confirmed")
        for url in (
            reverse("core:home"),
            reverse("core:booking_list"),
            reverse("core:booking_detail", args=[confirmed.pk]),
            reverse("core:room_detail", args=[self.room.pk]),
        ):
            self.client.get(url)
            confirmed.refresh_from_db()
            self.assertEqual(confirmed.status, "Confirmed", f"GET {url} changed a Confirmed booking's status")

    def test_room_detail_still_shows_overdue_indicator(self):
        # Phase 2 explicitly forbids silently removing overdue visibility —
        # the page must still be able to tell staff this booking is overdue,
        # it just must not act on it itself.
        response = self.client.get(reverse("core:room_detail", args=[self.room.pk]))
        self.assertContains(response, "Checkout overdue")

    def test_booking_detail_still_shows_overdue_indicator(self):
        response = self.client.get(reverse("core:booking_detail", args=[self.booking.pk]))
        self.assertContains(response, "Checkout overdue")


# ── Phase 3-5: the expire_elapsed_bookings command ─────────────────────────


class ExpireElapsedBookingsCommandTest(CircleCoreTenantTestCase):
    def setUp(self):
        self.owner = make_owner()
        activate_trial(self.owner)
        self.room = make_room("Command Room")
        self.guest = make_guest()

    def _call(self, *args):
        stdout, stderr = StringIO(), StringIO()
        call_command("expire_elapsed_bookings", *args, stdout=stdout, stderr=stderr)
        return stdout.getvalue(), stderr.getvalue()

    def test_dry_run_makes_no_writes(self):
        booking = _make_elapsed_booking(self.room, self.guest, status="Checked In")
        out, err = self._call("--dry-run")
        booking.refresh_from_db()
        self.room.refresh_from_db()
        self.assertEqual(booking.status, "Checked In")
        self.assertEqual(self.room.status, "Available")
        self.assertIn("Checked Out", out)
        self.assertEqual(AuditLog.objects.filter(reason="Automatic elapsed-booking reconciliation").count(), 0)

    def test_neither_flag_defaults_to_dry_run(self):
        booking = _make_elapsed_booking(self.room, self.guest, status="Checked In")
        out, err = self._call()
        booking.refresh_from_db()
        self.assertEqual(booking.status, "Checked In")
        self.assertIn("dry-run", out.lower())

    def test_both_flags_is_an_error(self):
        with self.assertRaises(CommandError):
            call_command("expire_elapsed_bookings", "--dry-run", "--apply", stdout=StringIO(), stderr=StringIO())

    def test_apply_transitions_elapsed_checked_in_to_checked_out(self):
        booking = _make_elapsed_booking(self.room, self.guest, status="Checked In")
        self._call("--apply")
        booking.refresh_from_db()
        self.room.refresh_from_db()
        self.assertEqual(booking.status, "Checked Out")
        self.assertIsNotNone(booking.check_out_time)
        self.assertEqual(self.room.status, "Cleaning")
        self.assertEqual(self.room.cleaning_status, "Needs Cleaning")

    def test_apply_transitions_elapsed_confirmed_to_no_show(self):
        booking = _make_elapsed_booking(self.room, self.guest, status="Confirmed")
        self._call("--apply")
        booking.refresh_from_db()
        self.assertEqual(booking.status, "No Show")

    def test_apply_transitions_elapsed_pending_to_no_show(self):
        booking = _make_elapsed_booking(self.room, self.guest, status="Pending")
        self._call("--apply")
        booking.refresh_from_db()
        self.assertEqual(booking.status, "No Show")

    def test_no_show_does_not_erase_amount_owed(self):
        booking = _make_elapsed_booking(self.room, self.guest, status="Confirmed")
        owed_before = booking.balance_due
        self.assertGreater(owed_before, Decimal("0.00"))
        self._call("--apply")
        booking.refresh_from_db()
        self.assertEqual(booking.balance_due, owed_before)

    def test_repeated_apply_is_idempotent(self):
        booking = _make_elapsed_booking(self.room, self.guest, status="Checked In")
        self._call("--apply")
        booking.refresh_from_db()
        first_status = booking.status
        first_checkout_time = booking.check_out_time
        self._call("--apply")
        booking.refresh_from_db()
        self.assertEqual(booking.status, first_status)
        self.assertEqual(booking.check_out_time, first_checkout_time)
        self.assertEqual(
            AuditLog.objects.filter(object_type="Booking", object_id=str(booking.pk), reason="Automatic elapsed-booking reconciliation").count(),
            1,
        )

    def test_future_bookings_are_left_untouched(self):
        booking = _make_future_booking(self.room, self.guest, status="Confirmed")
        self._call("--apply")
        booking.refresh_from_db()
        self.assertEqual(booking.status, "Confirmed")

    def test_already_cancelled_booking_is_left_untouched(self):
        booking = _make_elapsed_booking(self.room, self.guest, status="Cancelled")
        self._call("--apply")
        booking.refresh_from_db()
        self.assertEqual(booking.status, "Cancelled")

    def test_already_checked_out_booking_is_left_untouched(self):
        booking = _make_elapsed_booking(self.room, self.guest, status="Checked Out")
        self._call("--apply")
        booking.refresh_from_db()
        self.assertEqual(booking.status, "Checked Out")

    def test_already_no_show_booking_is_left_untouched(self):
        booking = _make_elapsed_booking(self.room, self.guest, status="No Show")
        self._call("--apply")
        booking.refresh_from_db()
        self.assertEqual(booking.status, "No Show")

    def test_audit_log_created_for_automatic_transition(self):
        booking = _make_elapsed_booking(self.room, self.guest, status="Checked In")
        self._call("--apply")
        entry = AuditLog.objects.filter(object_type="Booking", object_id=str(booking.pk)).order_by("-created_at").first()
        self.assertIsNotNone(entry)
        self.assertIsNone(entry.actor)
        self.assertEqual(entry.reason, "Automatic elapsed-booking reconciliation")
        self.assertEqual(entry.before.get("status"), "Checked In")
        self.assertEqual(entry.after.get("status"), "Checked Out")
        self.assertIn("run_id", entry.after)

    def test_public_schema_is_excluded_and_produces_no_error(self):
        with schema_context("public"):
            GuestHouseTenant.objects.get_or_create(
                schema_name="public",
                defaults=dict(
                    name="Circle Core Platform",
                    owner_name="Platform",
                    owner_email="platform-expire@example.com",
                    owner_phone="0800000000",
                    is_active=True,
                    is_verified=True,
                ),
            )
        out, err = self._call("--dry-run")
        self.assertEqual(err, "")
        self.assertNotIn("core_booking", err)

    def test_tenant_domain_filter_restricts_processing(self):
        _make_elapsed_booking(self.room, self.guest, status="Checked In")
        out, err = self._call("--dry-run", "--tenant-domain", "some-domain-that-does-not-match.example.com")
        self.assertNotIn(self.tenant.schema_name, out)


class ExpireElapsedBookingsSharedCapacityTest(CircleCoreTenantTestCase):
    def setUp(self):
        self.owner = make_owner()
        activate_trial(self.owner)
        enable_shared_capacity()
        self.room = make_shared_room("Shared Room", price=180, max_guests=6)

    def _call(self, *args):
        stdout, stderr = StringIO(), StringIO()
        call_command("expire_elapsed_bookings", *args, stdout=stdout, stderr=stderr)
        return stdout.getvalue(), stderr.getvalue()

    def _current_shared_occupancy(self):
        # occupancy_snapshot() is calendar/date-scoped (used for availability
        # display) and would not count a booking whose check_out_date has
        # already elapsed as occupying "today" — exactly the overdue case
        # this test is about — so "currently checked in" is measured
        # directly from status instead.
        return sum(
            allocation.allocated_guests
            for allocation in RoomAllocation.objects.filter(room=self.room, booking__status="Checked In")
        )

    def test_one_occupant_expiring_releases_only_their_own_capacity(self):
        guest_a = make_guest(first_name="StayingA", phone="0820000501")
        guest_b = make_guest(first_name="LeavingB", phone="0820000502")
        staying = _make_elapsed_booking(self.room, guest_a, status="Checked In", num_guests=2)
        # Give "staying" a check-out date in the future so it is NOT overdue,
        # while "leaving" genuinely is — both checked in, sharing the room.
        staying.check_in_date = timezone.localdate() - datetime.timedelta(days=1)
        staying.check_out_date = timezone.localdate() + datetime.timedelta(days=5)
        staying.save()
        leaving = _make_elapsed_booking(self.room, guest_b, status="Checked In", num_guests=1)

        occupied_before = self._current_shared_occupancy()
        self.assertEqual(occupied_before, 3)

        self._call("--apply")

        staying.refresh_from_db()
        leaving.refresh_from_db()
        self.room.refresh_from_db()
        self.assertEqual(staying.status, "Checked In", "the still-active occupant must not be touched")
        self.assertEqual(leaving.status, "Checked Out")
        self.assertNotEqual(self.room.status, "Cleaning", "the room must not go to Cleaning while an occupant remains")

        occupied_after = self._current_shared_occupancy()
        self.assertEqual(occupied_after, occupied_before - 1)
        self.assertGreaterEqual(occupied_after, 0)

    def test_final_occupant_expiring_triggers_full_turnover(self):
        guest = make_guest(first_name="LastOut", phone="0820000503")
        booking = _make_elapsed_booking(self.room, guest, status="Checked In", num_guests=1)
        self._call("--apply")
        booking.refresh_from_db()
        self.room.refresh_from_db()
        self.assertEqual(booking.status, "Checked Out")
        self.assertEqual(self.room.status, "Cleaning")
        self.assertEqual(self.room.cleaning_status, "Needs Cleaning")

    def test_occupancy_never_goes_negative(self):
        guest = make_guest(first_name="Solo", phone="0820000504")
        booking = _make_elapsed_booking(self.room, guest, status="Checked In", num_guests=3)
        self._call("--apply")
        self._call("--apply")  # rerun: must stay idempotent, never double-release
        booking.refresh_from_db()
        self.assertEqual(booking.status, "Checked Out")
        occupied_after = self._current_shared_occupancy()
        self.assertGreaterEqual(occupied_after, 0)
        occupied, _cap, remaining = occupancy_snapshot(self.room, timezone.localdate())
        self.assertGreaterEqual(occupied, 0)
        self.assertLessEqual(remaining, self.room.max_guests)


class ExpireElapsedBookingsConcurrencyTest(ConcurrencyTenantTestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.schema_name = connection.schema_name
        GuestHouseSettings.objects.get_or_create(pk=1)

    def test_concurrent_command_executions_are_idempotent(self):
        room = make_room("Concurrent Room")
        guest = make_guest(first_name="Concurrent", phone="0830000601")
        booking = _make_elapsed_booking(room, guest, status="Checked In")

        def run_apply():
            def _run():
                call_command("expire_elapsed_bookings", "--apply", stdout=StringIO(), stderr=StringIO())
                return True
            return _run

        results = run_concurrently(self.schema_name, [run_apply(), run_apply()])
        for result, exc in results:
            self.assertIsNone(exc, f"concurrent command run raised: {exc}")

        with schema_context(self.schema_name):
            booking.refresh_from_db()
            self.assertEqual(booking.status, "Checked Out")
            audit_count = AuditLog.objects.filter(
                object_type="Booking", object_id=str(booking.pk), reason="Automatic elapsed-booking reconciliation"
            ).count()
            self.assertEqual(audit_count, 1, "an elapsed booking must only ever be reconciled once, even under a race")

    def test_scheduled_expiry_races_manual_checkout_safely(self):
        from core.booking_transactions import check_out_multi_room_booking

        room = make_room("Race Room")
        guest = make_guest(first_name="Racer", phone="0830000602")
        booking = _make_elapsed_booking(room, guest, status="Checked In")

        def run_command():
            def _run():
                call_command("expire_elapsed_bookings", "--apply", stdout=StringIO(), stderr=StringIO())
                return "command"
            return _run

        def run_manual_checkout():
            def _run():
                fresh = Booking.objects.get(pk=booking.pk)
                return check_out_multi_room_booking(fresh)
            return _run

        results = run_concurrently(self.schema_name, [run_command(), run_manual_checkout()])
        exceptions = [exc for _, exc in results if exc is not None]
        # Either side may legitimately lose the race (the other one already
        # checked it out first) via ValidationError — that is the expected,
        # safe outcome, not a bug. What must never happen is a crash from
        # anything else, or the booking ending up in a corrupted state.
        for exc in exceptions:
            from django.core.exceptions import ValidationError
            self.assertIsInstance(exc, ValidationError)

        with schema_context(self.schema_name):
            booking.refresh_from_db()
            self.assertEqual(booking.status, "Checked Out")
            # Whichever side won the race, exactly one of them actually
            # performed the transition — never both, never neither.
            audit_count = AuditLog.objects.filter(
                object_type="Booking", object_id=str(booking.pk), reason="Automatic elapsed-booking reconciliation"
            ).count()
            self.assertLessEqual(audit_count, 1)


class ExpireElapsedBookingsTenantFailureIsolationTest(CircleCoreTenantTestCase):
    """One tenant's processing failure must not block another tenant's
    elapsed bookings from being reconciled, and the command must report the
    failure (CommandError + stderr) rather than silently swallowing it.

    This deliberately avoids provisioning a second real Postgres schema: a
    genuine second django-tenants schema is expensive to create/drop inside
    a test, and doing so was found (by comparing full-suite runs with and
    without it) to destabilize unrelated tests elsewhere in this large
    shared-database test suite. Instead, a second GuestHouseTenant row is
    created with auto_create_schema disabled (so no schema is actually
    provisioned) to stand in for a "broken" tenant, and Command._process_tenant
    is patched to fail only for that row's schema name. This exercises the
    exact same per-tenant try/except isolation path in
    core/management/commands/expire_elapsed_bookings.py without the
    real-schema churn."""

    def setUp(self):
        self.owner = make_owner()
        activate_trial(self.owner)
        self.room = make_room("Isolation Room")
        self.guest = make_guest()

    def test_one_tenant_failure_does_not_block_another_tenants_processing(self):
        booking = _make_elapsed_booking(self.room, self.guest, status="Checked In")
        healthy_schema = self.tenant.schema_name

        with schema_context("public"):
            broken_tenant = GuestHouseTenant(
                schema_name="expire_isolation_broken",
                name="Broken", owner_name="Broken", owner_email="broken-expire@example.com",
                owner_phone="0000000000", is_active=True, is_verified=True,
            )
            broken_tenant.auto_create_schema = False
            broken_tenant.save()

        original = expire_cmd.Command._process_tenant

        def flaky(cmd_self, tenant, apply_changes, run_id):
            if tenant.schema_name != healthy_schema:
                raise RuntimeError(f"simulated failure in tenant {tenant.schema_name}")
            return original(cmd_self, tenant, apply_changes, run_id)

        with patch.object(expire_cmd.Command, "_process_tenant", flaky):
            stdout, stderr = StringIO(), StringIO()
            with self.assertRaises(CommandError):
                call_command("expire_elapsed_bookings", "--apply", stdout=stdout, stderr=stderr)
            self.assertIn("expire_isolation_broken", stderr.getvalue())

        booking.refresh_from_db()
        self.assertEqual(
            booking.status, "Checked Out",
            "the healthy tenant must still be processed despite another tenant failing",
        )


# ── Phase 6: outstanding-balance reporting ─────────────────────────────────


class OutstandingBalanceReportingTest(CircleCoreTenantTestCase):
    def setUp(self):
        self.owner = make_owner()
        activate_trial(self.owner)
        self.client.login(username="owner", password="testpass123")
        self.room = make_room("Reporting Room")

    def _checked_out_unpaid_booking(self):
        guest = make_guest(first_name="Departed", phone="0850000601")
        booking = _make_elapsed_booking(self.room, guest, status="Checked Out")
        return booking

    def test_departed_unpaid_booking_remains_in_home_outstanding_balance(self):
        booking = self._checked_out_unpaid_booking()
        response = self.client.get(reverse("core:home"))
        self.assertGreaterEqual(response.context["departed_outstanding_balance"], booking.balance_due)
        self.assertGreaterEqual(response.context["outstanding_balances"], booking.balance_due)

    def test_no_show_unpaid_booking_remains_visible_if_financially_valid(self):
        guest = make_guest(first_name="NoShowDebt", phone="0850000602")
        booking = _make_elapsed_booking(self.room, guest, status="No Show")
        response = self.client.get(reverse("core:home"))
        self.assertGreaterEqual(response.context["departed_outstanding_balance"], booking.balance_due)

    def test_cancelled_unpaid_booking_still_counts_as_outstanding(self):
        guest = make_guest(first_name="CancelledDebt", phone="0850000603")
        booking = _make_elapsed_booking(self.room, guest, status="Cancelled")
        response = self.client.get(reverse("core:home"))
        self.assertGreaterEqual(response.context["departed_outstanding_balance"], booking.balance_due)

    def test_fully_paid_departed_booking_is_excluded(self):
        booking = self._checked_out_unpaid_booking()
        Payment.objects.create(booking=booking, amount=booking.balance_due, payment_method="Cash", payment_type="Payment")
        booking.refresh_from_db()
        self.assertEqual(booking.balance_due, Decimal("0.00"))
        response = self.client.get(reverse("core:home"))
        self.assertNotIn(booking, list(response.context["unpaid_bookings"]))

    def test_fully_refunded_departed_booking_is_excluded(self):
        booking = self._checked_out_unpaid_booking()
        original_amount = booking.total_amount
        Payment.objects.create(booking=booking, amount=original_amount, payment_method="Cash", payment_type="Payment")
        booking.refresh_from_db()
        from core.models import BookingRefund
        BookingRefund.objects.create(booking=booking, amount=original_amount, refund_method="Cash", reason="Guest complaint")
        booking.refresh_from_db()
        response = self.client.get(reverse("core:home"))
        self.assertNotIn(booking, list(response.context["unpaid_bookings"]))

    def test_mixed_active_and_departed_totals_correctly(self):
        active_guest = make_guest(first_name="StillHere", phone="0850000604")
        active_booking = Booking.objects.create(
            room=make_room("Active Room"), guest=active_guest,
            check_in_date=timezone.localdate(), check_out_date=timezone.localdate() + datetime.timedelta(days=2),
            booking_duration_type="daily", num_guests=1, rate_per_night=Decimal("500.00"),
            status="Checked In", booking_source="Walk-in",
            balance_due=Decimal("1000.00"), total_amount=Decimal("1000.00"),
        )
        departed_booking = self._checked_out_unpaid_booking()

        response = self.client.get(reverse("core:home"))
        ctx = response.context
        self.assertGreaterEqual(ctx["active_stay_outstanding_balance"], active_booking.balance_due)
        self.assertGreaterEqual(ctx["departed_outstanding_balance"], departed_booking.balance_due)
        self.assertEqual(
            ctx["outstanding_balances"],
            ctx["active_stay_outstanding_balance"] + ctx["departed_outstanding_balance"],
        )

    def test_page_get_cannot_make_debt_disappear(self):
        booking = self._checked_out_unpaid_booking()
        owed_before = booking.balance_due
        for _ in range(3):
            self.client.get(reverse("core:home"))
            self.client.get(reverse("core:booking_detail", args=[booking.pk]))
        booking.refresh_from_db()
        self.assertEqual(booking.balance_due, owed_before)
        response = self.client.get(reverse("core:home"))
        self.assertGreaterEqual(response.context["departed_outstanding_balance"], owed_before)

    def test_reports_view_departed_outstanding_matches_home(self):
        booking = self._checked_out_unpaid_booking()
        home_response = self.client.get(reverse("core:home"))
        reports_response = self.client.get(reverse("core:reports"))
        self.assertGreaterEqual(reports_response.context["departed_outstanding_balance"], booking.balance_due)
        self.assertEqual(
            reports_response.context["outstanding_balances"],
            reports_response.context["active_stay_outstanding_balance"] + reports_response.context["departed_outstanding_balance"],
        )
