# Circle Core Guest House Production Go-Live Checklist

Do not mark the app production-ready unless every required gate below is passed on the target VPS or a production-equivalent environment.

## Automated Gates

- `python manage.py check --settings=config.settings_production` passes.
- `python manage.py check --deploy --settings=config.settings_production` passes.
- `python manage.py makemigrations --check --dry-run` reports no changes.
- `python manage.py migrate_schemas --noinput --settings=config.settings_production` completes.
- `python manage.py test core` passes against PostgreSQL.
- Static files collect successfully with `python manage.py collectstatic --noinput --settings=config.settings_production`.

## Backup and Restore

- Run `python manage.py backup_local --settings=config.settings_production`.
- Copy the database dump and media zip off the VPS.
- Restore the dump into a separate PostgreSQL instance with `pg_restore`.
- Restore media files into a separate media directory.
- Start the app against the restored database and media.
- Confirm tenants, users, bookings, payments, PDFs, and uploaded files are present.

## Tenant Safety

- Tenant/customer records must be suspended or deactivated, not hard-deleted.
- Django admin must not allow tenant deletion.
- Command Center suspend and activate actions must be audited.
- Failed registration cleanup is the only approved hard-delete path.

## Audit Trail

Confirm audit records exist for:

- Booking create/edit/cancel/no-show.
- Check-in and check-out.
- Payment create/delete.
- Refunds.
- POS sale and refund.
- Subscription activation, suspension, manual payment, and trial extension.
- Command Center impersonation token issue and tenant impersonation entry.

## South Africa Timezone

- `TIME_ZONE` is `Africa/Johannesburg`.
- `USE_TZ=True`.
- Trial expiry, subscription grace period, reports, invoice issue dates, check-in timestamps, and check-out timestamps display correctly in South African local time.

## Invoice and Receipt Numbering

- Booking invoice number is based on unique `booking_reference`.
- Receipt number is based on unique booking/payment reference combination.
- Numbers must not collide after restart, migration, or restore.

## Permissions

- Staff users cannot access billing, settings, exports, security reports, daily close controls, or admin-only pages unless their role permits it.
- Cleaner role can only use housekeeping/maintenance-safe views.
- Tenant users cannot access another tenant workspace.
- Public pages do not expose tenant/customer lists.

## Browser and Mobile Testing

- Test current Chrome desktop.
- Test current Edge desktop.
- Test Safari/iOS or a real mobile browser.
- On a phone, create booking, create guest, record payment, check in, check out, and view booking detail.

## Bad Data Testing

Try and confirm clean validation errors for:

- Negative payment.
- Check-out before check-in.
- Very long names.
- Duplicate room names.
- Booking with zero guests.
- Discount bigger than booking subtotal.
- Deposit bigger than booking total.
- Maintenance or blocked room booking.
- Unsafe upload file.
- Oversized upload file.

## SMTP Failure Handling

- Temporarily configure invalid SMTP credentials.
- Create booking and record payment.
- Confirm booking/payment saves.
- Confirm error is logged and the request does not crash.

## Media and Uploads

- Logo and cleaning proof uploads reject unsafe file types.
- Logo and cleaning proof uploads reject files larger than 5MB.
- Uploaded cleaning proof photos require authenticated access.
- Nginx media rules do not expose private operational files unintentionally.

## POPIA and Privacy

- Public privacy page exists.
- Public terms page exists.
- Public data request process exists.
- Workspace owner can export operational data where plan permits.
- Deletion/correction requests have an operational process.

## Performance

Seed or import at least:

- 50 rooms.
- 500 guests.
- 1,000 bookings.
- 2,000 payments.

Confirm dashboard, bookings list, reports, payments list, and search remain usable.

## Deployment Security

- `DEBUG=False`.
- `.env` is used and not committed.
- `SECRET_KEY` is strong and unique to production.
- `ALLOWED_HOSTS` is exact.
- HTTPS works.
- Admin URL is non-default if configured.
- PostgreSQL is not publicly exposed.
- Redis is not publicly exposed.
- Backups are not publicly exposed.
- Error pages do not show stack traces.

## Disaster Recovery

Answer yes before launch:

- If the VPS dies today, can the latest database and media backup be restored elsewhere?
- Is the restore process documented and tested?
- Are credentials stored outside the VPS?
- Is DNS/subdomain recovery understood?
- Can a new server be provisioned and restored without guessing?
