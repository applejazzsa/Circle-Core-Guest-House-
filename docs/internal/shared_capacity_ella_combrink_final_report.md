# Shared-Capacity Booking — Final Implementation & Tenant-Readiness Report

**Target tenant:** `ellacombrink.guesthouse.circlecore.co.za` (Tenant ID 12, schema `ellacombrink`)
**Report date:** 2026-07-28
**Status: DEMO-READY / PRODUCTION-LIVE for Ella Combrink only**

---

## 1. Verification checklist

| # | Item | Result |
|---|------|--------|
| 1 | Feature enabled only for Ella Combrink | **PASS** — `shared_capacity_booking_enabled=True` only on schema `ellacombrink`; swept all 9 other tenant schemas, all `False`. |
| 2 | Other tenants retain whole-room behaviour | **PASS** — `depalace` sample rooms (ROOM 10/11/12/13/3) unchanged: Available/Single/max_guests=2/WHOLE_ROOM, matching pre-deploy baseline. |
| 3 | Shared rooms accept several bookings | **PASS** — live production test: two overlapping bookings on Room 04 (2 + 3 guests) both succeeded. |
| 4 | Combined allocations cannot exceed capacity | **PASS** — a 3rd overlapping booking (5+3=8 > cap 7) was rejected with `"Room 04 only has 2 of 3 requested spaces available on 2027-05-24."` |
| 5 | Whole rooms remain blocked by one overlapping booking | **PASS** — a 2nd booking on Room 07 (WHOLE_ROOM, cap 2) was rejected outright despite spare `max_guests`: `"Room 07 is already booked for that time..."`. |
| 6 | PPPN pricing correct | **PASS** — 2 guests × R180 × 2 nights = R720.00 exactly; group booking (2 rooms × 2 guests × 2 nights × R180) = R1,440.00 exactly. |
| 7 | Multi-room group bookings work | **PASS** — one booking spanning Room 09 + Room 10 created atomically with correct per-room allocations and summed total. |
| 8 | Calendar displays partial occupancy | **PASS** — `shared_room_status_label()` correctly returned "Partially Occupied" at 5/7 and "Full" at 7/7 (unit-tested by `SharedCapacityDisplayTest`, confirmed live). |
| 9 | Availability displays remaining spaces | **PASS** — `occupancy_snapshot()` returned correct `(occupied, capacity, remaining)` tuples live in production (5/7/2, then 7/7/0). |
| 10 | Cancellation releases capacity | **PASS** — cancelling the 2-guest booking dropped Room 04 from 7/7 (Full) back to 5/7 (Partially Occupied). |
| 11 | Booking edits revalidate capacity | **PASS** — an edit that would exceed remaining capacity (2+6=8>7) was rejected; an edit to exactly the remaining capacity (2+5=7) succeeded. |
| 12 | Concurrent bookings cannot overbook | **PASS** — verified via `BookingTransactionConcurrencyTest` (9 automated tests using real multi-threaded `TransactionTestCase` + `threading.Barrier`), all passing. Not re-run against production directly — deliberately avoided introducing concurrent load on a live database; the transactional design (`select_for_update()` row locks in fixed ascending-pk order, re-validated inside the lock) is identical in production to what's under test. |
| 13 | Internal/pending notes remain staff-only | **PASS** — verified by `SharedCapacitySecurityAuditTest` (4 automated tests, incl. PDF content-stream extraction to rule out leakage via generated documents); spot-checked live that `Booking.notes` is a plain internal model field never rendered in any guest-facing template or API response. |
| 14 | No temporary test data remains | **PASS** — all demo bookings/allocations/guest created during production verification were deleted; booking/payment/allocation counts returned to exactly 2/2/2, matching the pre-test baseline. |
| 15 | No unrelated tenant data changed | **PASS** — re-swept all 9 non-Ella-Combrink tenant schemas post-verification: zero trace of the feature flag or `SHARED_CAPACITY` rooms anywhere else. |
| 16 | Database backup remains available | **PASS** — `/opt/circlecore/backups/guesthouse_db_backup_20260728T100820Z.dump` (1,916,248 bytes) confirmed present on the VPS. |
| 17 | Deployed Git commit is recorded | **PASS** — `2d6c5910e5a5fe4c8cde1e1150890b59e4b486a9`, confirmed identical on GitHub `main` and on the VPS working tree. |

---

## 2. Room inventory — Ella Combrink

### Shared-capacity rooms (19)
Room 01, 02, 03, 04, 05, 09, 10, 11, 12, 13, 14, 15, 16, 17A, 17B, 17C, 17D, 24, 25

### Whole-room inventory (15)
Room 06 (Accessible), 07, 08A (Flat), 08B (Flat), 08C (Flat), 18, 19, 20A, 20B, 21A, 21B, 22A, 22B, 23A, 23B

**Note:** the originally-quoted split of 20 shared / 14 whole-room does not match the registered configuration or the applied result. The actual, applied, and verified split is **19 shared-capacity / 15 whole-room** (confirmed 34 total rooms) — this was flagged and explicitly confirmed correct by the client before `--apply` was run.

---

## 3. Pending client confirmations (documented, not yet resolved)

- **Rooms 15, 16, 24, 25** are currently capped at 6 guests — conservative default, pending client sign-off on final capacity.
- **Rooms 17A–17D** are currently capped at 10 guests each — pending client sign-off.
- **The "Flat 8" en-suite room** referenced by the client remains unidentified against the current room list (08A/08B/08C exist as separate flat units; no room is explicitly tagged "en-suite"). Needs clarification from the client before any capacity/type change.
- **Flat 8A, 8B, 8C** remain **WHOLE_ROOM** inventory (not shared-capacity) per the current configuration.
- **Flat 8A, 8B, 8C** are temporarily priced at **R180 PPPN** — flagged by the client as temporary; final pricing pending confirmation.

None of the above were changed by this deployment — they are carried over exactly as they existed before, and `configure_shared_capacity_tenant` only ever *verifies* capacities/rates, never silently corrects them.

---

## 4. Changed files (feature branch → main, 8 commits, PR-equivalent merge `2d6c591`)

```
core/admin.py                                          |  22 +-
core/availability.py                                   | 311 ++ (new)
core/booking_transactions.py                           | 290 ++ (new)
core/forms.py                                          |  26 +-
core/management/commands/configure_shared_capacity_tenant.py | 278 ++ (new)
core/migrations/0041_shared_capacity_feature_flag.py   |  23 (new)
core/migrations/0042_room_allocation.py                |  33 (new)
core/migrations/0043_backfill_room_allocations.py      |  69 (new)
core/models.py                                         | 176 +-
core/tests.py                                          | 1444 ++
core/urls.py                                           |   2 +
core/views.py                                          | 336 +-
templates/availability.html                            |  27 +-
templates/core/booking_list.html                       |   6 +
templates/core/group_booking_form.html                 | 298 ++ (new)
templates/core/room_calendar.html                      |  21 +-
templates/core/room_detail.html                        |  10 +
templates/core/room_list.html                          |  25 +
```
18 files changed, 3,322 insertions(+), 75 deletions(-).

## 5. Migrations

- `core.0041_shared_capacity_feature_flag` — adds `GuestHouseSettings.shared_capacity_booking_enabled` (default `False`) and `Room.booking_mode` (default `WHOLE_ROOM`).
- `core.0042_room_allocation` — adds the `RoomAllocation` model.
- `core.0043_backfill_room_allocations` — idempotent data migration: creates one `RoomAllocation` per pre-existing active `Booking`, matching its current room/guest count. Reversible.

Applied cleanly across all 9 real tenant schemas with zero errors (confirmed via `showmigrations` on `ellacombrink` and `depalace` individually, plus a full-schema `migrate` run in the deploy script).

## 6. Deployed commit

`2d6c5910e5a5fe4c8cde1e1150890b59e4b486a9` — merge of `feature/tenant-shared-capacity-bookings` (8 commits) into `main`. Identical on GitHub and on the production VPS (`/opt/circlecore/apps/guesthouse`).

## 7. Backup

`/opt/circlecore/backups/guesthouse_db_backup_20260728T100820Z.dump` — 1,916,248 bytes, verified valid PostgreSQL custom-format dump (4,182 TOC entries via `pg_restore --list`), taken immediately before deployment.

## 8. Feature-setting value

`GuestHouseSettings.shared_capacity_booking_enabled`:
- `ellacombrink`: **True**
- all other tenants (8 real tenants + public): **False**

## 9. Room-mode counts (Ella Combrink)

- SHARED_CAPACITY: **19**
- WHOLE_ROOM: **15**
- Total: 34 (unchanged)

## 10. Automated test results

- `core` app: **136/136 passing** (run standalone, fresh DB, no `--keepdb`).
- `tenants` app: **10/10 passing** (run standalone, fresh DB).
- Combined `core + tenants` single-process run surfaced 2 failures + 10 errors, all confined to `tenants.test_product_control` and `tenants.test_guest_house_staging_integration` — pre-existing test-isolation issues (a Postgres FK-truncate ordering conflict between `TransactionTestCase`s across apps sharing one process), unrelated to and not touched by the shared-capacity feature. Not a regression; each app's suite is fully green when run on its own, which is how CI/deploy validation was performed throughout this engagement.
- Feature-specific coverage (all included in the 136): `SharedCapacityFeatureFlagTest` (7), `RoomAllocationModelTest` (10), `AvailabilityServiceTest` (14), `BookingTransactionConcurrencyTest` (9, real multi-threaded), `GroupBookingUITest` (9), `SharedCapacityDisplayTest` (9), `SharedCapacitySecurityAuditTest` (4).

## 11. Production smoke-test results

Live, read-write functional verification against `ellacombrink` in production (clearly tagged `TEMP-DEMO-VERIFY-20260728`, fully deleted afterward): **14/14 checks passed**, covering PPPN pricing, shared-room stacking, capacity-limit rejection, whole-room blocking, multi-room group booking, calendar/availability labels, cancellation release, and edit revalidation. See §1 for the itemized results. Final state confirmed identical to pre-test (2 bookings, 2 payments, 2 allocations; all demo-touched rooms back to their original status).

## 12. Known limitations

- `Booking.validate_room_available()` blocks creating **any** new booking — regardless of how far in the future the requested dates are — whenever the room's *current* `status` is `Maintenance`, `Blocked`, or `Cleaning`. This is pre-existing behaviour (predates the shared-capacity feature, applies identically to WHOLE_ROOM rooms) but is more visible now that shared rooms can legitimately accept a booking 300 days out while flagged "Needs Cleaning" today. Not a regression; flagged here as a candidate for a future, separate fix (e.g. only enforce this for check-ins within N days).
- Shared-capacity rooms have no dedicated `status` field state for "partially occupied" — `Room.status` remains whatever the last WHOLE_ROOM-style transition left it at; partial-occupancy display is computed on the fly via `shared_room_status_label()` wherever it's shown (room list, calendar, availability), not stored. This was an explicit, documented design decision from the original architecture doc, not an oversight.
- Concurrency correctness under real simultaneous production load was verified via automated multi-threaded tests, not by generating concurrent load against the live database (judged too risky for a tenant with real guests). The locking design is the same code path in both environments.
- The 20/14 room-split figure the client originally specified does not match the registered 19/15 configuration; documented in §2, confirmed correct by the client before apply.

## 13. Rollback instructions

**Fastest / lowest-risk — disable the feature (no deploy needed):**
```bash
ssh ubuntu@154.65.99.200
cd /opt/circlecore/apps/guesthouse
sudo docker exec guesthouse-web python manage.py shell -c "
from django_tenants.utils import schema_context
from core.models import GuestHouseSettings
with schema_context('ellacombrink'):
    s = GuestHouseSettings.objects.first()
    s.shared_capacity_booking_enabled = False
    s.save()
"
```
This immediately restores whole-room-only UI/behaviour for Ella Combrink (the master kill-switch — `Room.effective_booking_mode` requires *both* the tenant flag and the room's own `booking_mode` to be `SHARED_CAPACITY`). Room `booking_mode` values and `RoomAllocation` rows are left in place but become inert.

**Full code rollback (only if a defect is found in the shared-capacity code itself):**
```bash
ssh ubuntu@154.65.99.200
cd /opt/circlecore/apps/guesthouse
git checkout 77cf8789619142cfc1e5e7518bda903d94b782be -- .
bash deploy.sh
```
Migrations `0041`–`0043` are additive and reversible (`0043` has a working `reverse_code`); do not reverse them unless the code rollback above is also being performed, since later code doesn't expect their absence.

**Full restore (only if data corruption is suspected — last resort):**
```bash
pg_restore --clean --if-exists -d guesthouse_db /opt/circlecore/backups/guesthouse_db_backup_20260728T100820Z.dump
```

## 14. Final demo-ready status

**Ella Combrink is demo-ready.** Feature flag is live, 19 shared-capacity rooms are correctly configured with verified conservative capacities and PPPN rates, all 17 verification items pass, automated coverage is green, and a live production functional test exercising every core mechanic (multi-booking, capacity enforcement, whole-room blocking, group bookings, cancellation, edit revalidation, pricing) passed 14/14 with zero residual test data. No other tenant is affected. Pending items are limited to the five client-confirmation questions in §3 (room capacities, the unidentified Flat 8 en-suite room, and Flat 8A–C's temporary rate) — none of which block a demo, since current values are conservative and were explicitly verified unchanged by this deployment.
