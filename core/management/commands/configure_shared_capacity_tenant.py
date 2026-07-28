"""
Enable shared-capacity booking for a specific, pre-registered tenant and set
each of its rooms' booking_mode according to a fixed configuration payload.

Usage:
    python manage.py configure_shared_capacity_tenant \
        --domain ellacombrink.guesthouse.circlecore.co.za \
        --dry-run

    python manage.py configure_shared_capacity_tenant \
        --domain ellacombrink.guesthouse.circlecore.co.za \
        --apply

Design notes
------------
The tenant domain is used ONLY to look up which tenant this invocation
targets — general business logic (core/availability.py, core/models.py,
core/booking_transactions.py, every view) never references a specific
domain anywhere; `Room.effective_booking_mode` only ever checks the
tenant-level `shared_capacity_booking_enabled` flag and the room's own
`booking_mode`, regardless of which tenant it belongs to.

TENANT_ROOM_MODE_CONFIG below is the one place a *specific* tenant's room
layout is described, keyed by its exact domain. This command refuses to
touch any domain not explicitly registered here — it does not guess or
infer a configuration for an unrecognized tenant. Adding a second tenant
later means adding a second dict entry; the resolution/validation/
transaction/reporting logic below never changes.

This command:
  - never deletes a Room;
  - never touches Booking, Payment, or any invoice/refund model at all;
  - never modifies room capacity (max_guests) or pricing (price_per_night/
    pricing_model) — those were already configured correctly by an earlier,
    separate data-configuration task, and this command only *verifies* them
    against the values below, reporting any drift without correcting it;
  - only ever writes inside schema_context(tenant.schema_name) for the one
    resolved tenant, so it cannot affect any other tenant even in principle;
  - is idempotent — re-running it after it already applied reports every
    field as "unchanged" and performs no writes.

Dry-run and --apply execute the exact same code path (resolve, validate,
compute diff, write) inside one transaction.atomic() block; --dry-run
forces a rollback at the very end regardless of outcome, so a dry run is a
true preview of what --apply would do, not a separate/simpler code path
that could drift out of sync with it.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django_tenants.utils import schema_context

from tenants.models import Domain
from core.models import GuestHouseSettings, Room


class _DryRunRollback(Exception):
    """Internal signal used to unwind the atomic block after a dry run."""


TENANT_ROOM_MODE_CONFIG = {
    "ellacombrink.guesthouse.circlecore.co.za": {
        "shared_capacity_rooms": [
            "Room 01", "Room 02", "Room 03", "Room 04", "Room 05",
            "Room 09", "Room 10", "Room 11", "Room 12", "Room 13", "Room 14",
            "Room 15", "Room 16",
            "Room 17A", "Room 17B", "Room 17C", "Room 17D",
            "Room 24", "Room 25",
        ],
        "whole_room_rooms": [
            "Room 06 (Accessible)", "Room 07",
            "Room 08A (Flat)", "Room 08B (Flat)", "Room 08C (Flat)",
            "Room 18", "Room 19",
            "Room 20A", "Room 20B", "Room 21A", "Room 21B",
            "Room 22A", "Room 22B", "Room 23A", "Room 23B",
        ],
        # Verified only — never written by this command.
        "expected_capacities": {
            "Room 01": 8, "Room 02": 7, "Room 03": 7, "Room 04": 7, "Room 05": 7,
            "Room 09": 8, "Room 10": 8, "Room 11": 8, "Room 12": 8, "Room 13": 8, "Room 14": 8,
            "Room 15": 6, "Room 16": 6,
            "Room 17A": 10, "Room 17B": 10, "Room 17C": 10, "Room 17D": 10,
            "Room 24": 6, "Room 25": 6,
        },
        # Verified only — never written by this command. (rate, pricing_model)
        "expected_rates": {
            "Room 01": (Decimal("180.00"), "per_person"), "Room 02": (Decimal("180.00"), "per_person"),
            "Room 03": (Decimal("180.00"), "per_person"), "Room 04": (Decimal("180.00"), "per_person"),
            "Room 05": (Decimal("180.00"), "per_person"),
            "Room 06 (Accessible)": (Decimal("260.00"), "per_person"),
            "Room 07": (Decimal("260.00"), "per_person"),
            "Room 08A (Flat)": (Decimal("180.00"), "per_person"),
            "Room 08B (Flat)": (Decimal("180.00"), "per_person"),
            "Room 08C (Flat)": (Decimal("180.00"), "per_person"),
            "Room 09": (Decimal("180.00"), "per_person"), "Room 10": (Decimal("180.00"), "per_person"),
            "Room 11": (Decimal("180.00"), "per_person"), "Room 12": (Decimal("180.00"), "per_person"),
            "Room 13": (Decimal("180.00"), "per_person"), "Room 14": (Decimal("180.00"), "per_person"),
            "Room 15": (Decimal("180.00"), "per_person"), "Room 16": (Decimal("180.00"), "per_person"),
            "Room 17A": (Decimal("180.00"), "per_person"), "Room 17B": (Decimal("180.00"), "per_person"),
            "Room 17C": (Decimal("180.00"), "per_person"), "Room 17D": (Decimal("180.00"), "per_person"),
            "Room 18": (Decimal("260.00"), "per_person"), "Room 19": (Decimal("260.00"), "per_person"),
            "Room 20A": (Decimal("260.00"), "per_person"), "Room 20B": (Decimal("260.00"), "per_person"),
            "Room 21A": (Decimal("260.00"), "per_person"), "Room 21B": (Decimal("260.00"), "per_person"),
            "Room 22A": (Decimal("260.00"), "per_person"), "Room 22B": (Decimal("260.00"), "per_person"),
            "Room 23A": (Decimal("260.00"), "per_person"), "Room 23B": (Decimal("260.00"), "per_person"),
            "Room 24": (Decimal("180.00"), "per_person"), "Room 25": (Decimal("180.00"), "per_person"),
        },
    },
}


class Command(BaseCommand):
    help = (
        "Enable shared_capacity_booking_enabled and set each room's booking_mode "
        "for one pre-registered tenant, looked up by its exact domain."
    )

    def add_arguments(self, parser):
        parser.add_argument("--domain", required=True, help="Exact tenant domain to configure.")
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true", help="Preview changes only (default).")
        mode.add_argument("--apply", action="store_true", help="Actually write the changes.")

    def handle(self, *args, **options):
        domain_str = options["domain"].strip()
        apply_changes = bool(options["apply"])

        # 1 & 2: require an exact domain match; refuse anything ambiguous.
        matches = list(Domain.objects.select_related("tenant").filter(domain=domain_str))
        if not matches:
            raise CommandError(
                f"No tenant found for domain '{domain_str}'. "
                "Run 'python manage.py list_tenants' to see registered domains."
            )
        if len(matches) > 1:
            raise CommandError(
                f"Refusing to proceed: {len(matches)} Domain records match '{domain_str}' — "
                "this must resolve to exactly one tenant."
            )
        tenant = matches[0].tenant

        config = TENANT_ROOM_MODE_CONFIG.get(domain_str)
        if config is None:
            raise CommandError(
                f"No configuration payload is registered for domain '{domain_str}'. "
                "This command only configures tenants explicitly listed in "
                "TENANT_ROOM_MODE_CONFIG — add an entry there first."
            )

        # 3: identify the tenant clearly before touching anything.
        self.stdout.write(f"Tenant ID:   {tenant.id}")
        self.stdout.write(f"Tenant name: {tenant.name}")
        self.stdout.write(f"Tenant slug: {tenant.schema_name}")
        self.stdout.write(f"Domain:      {domain_str}")
        self.stdout.write("")

        mode_label = "APPLY" if apply_changes else "DRY-RUN"
        self.stdout.write(self.style.WARNING(f"=== Mode: {mode_label} ==="))
        self.stdout.write("")

        try:
            with transaction.atomic():
                report = self._configure(tenant, config)
                if not apply_changes:
                    raise _DryRunRollback()
        except _DryRunRollback:
            pass

        self._print_report(report, apply_changes)

    # -- Everything below runs strictly inside schema_context(tenant.schema_name) --
    def _configure(self, tenant, config):
        report = {
            "tenant_flag": None,
            "rooms": [],
            "missing_rooms": [],
            "capacity_drift": [],
            "rate_drift": [],
        }

        with schema_context(tenant.schema_name):
            settings_obj, _ = GuestHouseSettings.objects.get_or_create(pk=1)
            before_flag = settings_obj.shared_capacity_booking_enabled
            if not before_flag:
                settings_obj.shared_capacity_booking_enabled = True
                settings_obj.save(update_fields=["shared_capacity_booking_enabled"])
            report["tenant_flag"] = {"before": before_flag, "after": settings_obj.shared_capacity_booking_enabled}

            desired_modes = {}
            for name in config["shared_capacity_rooms"]:
                desired_modes[name] = "SHARED_CAPACITY"
            for name in config["whole_room_rooms"]:
                desired_modes[name] = "WHOLE_ROOM"

            rooms_by_name = {room.name: room for room in Room.objects.filter(name__in=desired_modes.keys())}

            for name, desired_mode in desired_modes.items():
                room = rooms_by_name.get(name)
                if room is None:
                    report["missing_rooms"].append(name)
                    continue
                before_mode = room.booking_mode
                if before_mode != desired_mode:
                    room.booking_mode = desired_mode
                    room.save(update_fields=["booking_mode"])
                report["rooms"].append({
                    "name": name, "before": before_mode, "after": desired_mode,
                    "changed": before_mode != desired_mode,
                })

                expected_capacity = config["expected_capacities"].get(name)
                if expected_capacity is not None and room.max_guests != expected_capacity:
                    report["capacity_drift"].append(
                        f"{name}: expected max_guests={expected_capacity}, found {room.max_guests} (not changed)"
                    )
                expected_rate = config["expected_rates"].get(name)
                if expected_rate is not None:
                    exp_price, exp_model = expected_rate
                    if room.price_per_night != exp_price or room.pricing_model != exp_model:
                        report["rate_drift"].append(
                            f"{name}: expected {exp_price} ({exp_model}), "
                            f"found {room.price_per_night} ({room.pricing_model}) (not changed)"
                        )

            # 8 & 9: never delete anything, never touch bookings/payments — no
            # such calls exist anywhere in this method; nothing further to do.

        if report["missing_rooms"]:
            raise CommandError(
                "Refusing to proceed: the following configured rooms were not found "
                f"in tenant '{tenant.schema_name}': {', '.join(report['missing_rooms'])}. "
                "No changes were made."
            )

        return report

    def _print_report(self, report, applied):
        self.stdout.write("--- Tenant setting ---")
        flag = report["tenant_flag"]
        state = "unchanged" if flag["before"] == flag["after"] else "changed"
        self.stdout.write(f"shared_capacity_booking_enabled: before={flag['before']} after={flag['after']} ({state})")
        self.stdout.write("")

        self.stdout.write("--- Room booking_mode ---")
        changed_count = 0
        for entry in report["rooms"]:
            state = "changed" if entry["changed"] else "unchanged"
            if entry["changed"]:
                changed_count += 1
            self.stdout.write(f"{entry['name']:24s} before={entry['before']:16s} after={entry['after']:16s} ({state})")
        self.stdout.write("")
        self.stdout.write(f"Rooms configured: {len(report['rooms'])}, changed: {changed_count}, unchanged: {len(report['rooms']) - changed_count}")
        self.stdout.write("")

        if report["capacity_drift"]:
            self.stdout.write(self.style.WARNING("--- Capacity drift detected (NOT modified) ---"))
            for line in report["capacity_drift"]:
                self.stdout.write(self.style.WARNING(line))
            self.stdout.write("")
        else:
            self.stdout.write("Capacity check: all configured rooms match expected capacity. No changes made (none needed).")
            self.stdout.write("")

        if report["rate_drift"]:
            self.stdout.write(self.style.WARNING("--- Rate drift detected (NOT modified) ---"))
            for line in report["rate_drift"]:
                self.stdout.write(self.style.WARNING(line))
            self.stdout.write("")
        else:
            self.stdout.write("Rate check: all configured rooms match expected PPPN rate. No changes made (none needed).")
            self.stdout.write("")

        if applied:
            self.stdout.write(self.style.SUCCESS("=== Changes committed. ==="))
        else:
            self.stdout.write(self.style.WARNING("=== DRY RUN — no changes were committed. Re-run with --apply to write these changes. ==="))
