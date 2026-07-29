"""
Authoritative, scheduled replacement for the removed GET-triggered
_expire_elapsed_bookings() mutations that used to run inside page views
(home, room_list, room_detail, cleaning_board, availability, booking_list,
booking_detail, housekeeping_mobile). Those views are now pure reads — this
command is the only thing that actually transitions an elapsed booking.

Run hourly (see docker/cron-entrypoint.sh):
    python manage.py expire_elapsed_bookings --apply --settings=config.settings_production

Always dry-run first when checking this command's behaviour:
    python manage.py expire_elapsed_bookings --dry-run

Transitions applied (identical to the removed function's rules):
  Checked In, elapsed  -> Checked Out (shared-capacity-aware: only this
                          booking's own allocation is released; the whole
                          room only turns Cleaning once the last occupant
                          has left — see check_out_multi_room_booking()).
  Confirmed/Pending,
  elapsed               -> No Show (releases reserved inventory implicitly:
                          No Show is an INACTIVE_STATUSES value, so it drops
                          out of every occupancy/availability calculation on
                          its own, without a separate "release" write).

Every automatic transition writes an AuditLog entry (actor=None — no staff
user is fabricated) tagged with this run's run_id, mirroring the audit shape
the manual checkout/no-show views already produce.
"""

import logging
import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django_tenants.utils import schema_context

from core.availability import is_booking_overdue
from core.booking_transactions import _booking_rooms, _lock_rooms, check_out_multi_room_booking
from core.models import AuditLog, Booking, GuestHouseSettings
from core.views import _sync_room_status
from tenants.models import GuestHouseTenant

logger = logging.getLogger(__name__)

OVERDUE_ELIGIBLE_STATUSES = ("Pending", "Confirmed", "Checked In")


class Command(BaseCommand):
    help = (
        "Transition elapsed bookings (Checked In -> Checked Out, Confirmed/Pending -> "
        "No Show) for every tenant. This is the only place in the app allowed to make "
        "these transitions — page views are read-only."
    )

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Show what would change; write nothing.")
        parser.add_argument("--apply", action="store_true", help="Perform the transitions for real.")
        parser.add_argument("--tenant-domain", help="Only process the tenant that owns this domain.")
        parser.add_argument("--tenant-id", type=int, help="Only process this tenant by id.")

    def handle(self, *args, **options):
        if options["dry_run"] and options["apply"]:
            raise CommandError("Pass either --dry-run or --apply, not both.")
        apply_changes = bool(options["apply"])
        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                "No --apply flag given — running in dry-run mode. Nothing will be written."
            ))

        run_id = uuid.uuid4().hex[:12]

        # Tenants are isolated by Postgres schema, not a shared tenant_id
        # column (see config/settings.py TENANT_APPS) — Booking only exists
        # inside real tenant schemas, so the public pseudo-tenant is always
        # excluded, exactly like every other tenant-iterating command in
        # this codebase (send_trial_reminders, send_spa_reminders, ...).
        queryset = GuestHouseTenant.objects.exclude(schema_name="public")
        if options.get("tenant_domain"):
            queryset = queryset.filter(domains__domain=options["tenant_domain"])
        if options.get("tenant_id"):
            queryset = queryset.filter(pk=options["tenant_id"])
        tenants = list(queryset.order_by("pk").distinct())

        total_checked_out = 0
        total_no_show = 0
        failed_tenants = []

        for tenant in tenants:
            try:
                checked_out, no_show = self._process_tenant(tenant, apply_changes, run_id)
            except Exception as exc:
                # One tenant's failure must never abort or corrupt another
                # tenant's processing — isolate and keep going.
                failed_tenants.append(tenant.schema_name)
                logger.exception("expire_elapsed_bookings failed for tenant %s", tenant.schema_name)
                self.stderr.write(f"[{tenant.schema_name}] ERROR: {exc}")
                continue
            total_checked_out += checked_out
            total_no_show += no_show
            mode = "" if apply_changes else " (dry-run)"
            self.stdout.write(f"[{tenant.schema_name}] {checked_out} checked-out, {no_show} no-show{mode}")

        summary = (
            f"{len(tenants)} tenant(s) processed, {total_checked_out} checked out, "
            f"{total_no_show} marked no-show"
        )
        if failed_tenants:
            summary += f", {len(failed_tenants)} tenant(s) failed: {', '.join(failed_tenants)}"
        summary += f". run_id={run_id}"

        if failed_tenants:
            self.stderr.write(self.style.ERROR(summary))
            raise CommandError(f"{len(failed_tenants)} tenant(s) failed: {', '.join(failed_tenants)}")
        self.stdout.write(self.style.SUCCESS(summary))

    def _process_tenant(self, tenant, apply_changes, run_id):
        checked_out = 0
        no_show = 0
        with schema_context(tenant.schema_name):
            settings_obj = GuestHouseSettings.objects.filter(pk=1).first()
            if not settings_obj:
                return 0, 0

            # Deterministic order (ascending pk) so a rerun always visits
            # candidates the same way — required for the lock ordering in
            # _apply_one() to stay deadlock-safe against itself.
            candidate_ids = list(
                Booking.objects.filter(status__in=OVERDUE_ELIGIBLE_STATUSES)
                .order_by("pk")
                .values_list("pk", flat=True)
            )

            for booking_id in candidate_ids:
                if apply_changes:
                    result = self._apply_one(booking_id, settings_obj, tenant, run_id)
                else:
                    result = self._dry_run_one(booking_id, settings_obj)
                if result == "Checked Out":
                    checked_out += 1
                elif result == "No Show":
                    no_show += 1
        return checked_out, no_show

    def _dry_run_one(self, booking_id, settings_obj):
        booking = Booking.objects.select_related("room").filter(pk=booking_id).first()
        if not booking:
            return None
        info = is_booking_overdue(booking, settings_obj)
        if not info.overdue:
            return None
        self.stdout.write(
            f"  [DRY RUN] {booking.booking_reference}: {booking.status} -> {info.suggested_transition} "
            f"(overdue since {info.overdue_since})"
        )
        return info.suggested_transition

    def _apply_one(self, booking_id, settings_obj, tenant, run_id):
        with transaction.atomic():
            booking = Booking.objects.select_related("room").filter(pk=booking_id).first()
            if not booking:
                return None
            rooms = _booking_rooms(booking)
            if not rooms:
                return None
            # Lock every affected room first, in the same fixed ascending-pk
            # order every other booking mutation in this codebase already
            # uses (see _lock_rooms in booking_transactions.py) — this is
            # what keeps a scheduled run and a concurrent manual checkout
            # from ever deadlocking against each other.
            _lock_rooms(room.pk for room in rooms)
            # Re-fetch the booking row itself under a lock, and recheck its
            # status/elapsed condition now that the locks are held — another
            # worker or a manual staff action may have already moved it.
            booking = Booking.objects.select_for_update().select_related("room").filter(pk=booking_id).first()
            if not booking or booking.status not in OVERDUE_ELIGIBLE_STATUSES:
                return None
            info = is_booking_overdue(booking, settings_obj)
            if not info.overdue:
                return None

            before_status = booking.status
            if booking.status == "Checked In":
                # Already shared-capacity-aware: releases only this
                # booking's own allocation, preserves other occupants, and
                # only forces the whole room to Cleaning when this was the
                # last one still checked in.
                booking = check_out_multi_room_booking(booking)
                new_status = "Checked Out"
            else:
                booking.status = "No Show"
                booking.save(update_fields=["status"])
                for room in _booking_rooms(booking):
                    _sync_room_status(room)
                new_status = "No Show"

            AuditLog.objects.create(
                actor=None,
                action="update",
                object_type="Booking",
                object_id=str(booking.pk),
                object_repr=str(booking)[:255],
                before={"status": before_status},
                after={
                    "status": new_status,
                    "tenant_schema": tenant.schema_name,
                    "run_id": run_id,
                    "overdue_since": info.overdue_since.isoformat() if info.overdue_since else None,
                },
                reason="Automatic elapsed-booking reconciliation",
            )
            return new_status
