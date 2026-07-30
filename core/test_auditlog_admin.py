"""
AuditLog must be append-only through Django Admin: no add, no change, no
delete (individual or bulk) — for any user, including a superuser. Tenant
owner accounts in this app are provisioned as Django superusers, so without
this hardening a tenant's own owner could delete audit evidence via
/admin/core/auditlog/. Application code (services, management commands)
must still be able to create AuditLog rows via the ORM directly; only the
admin UI is restricted.

See also core/test_expire_elapsed_bookings.py, which covers the
expire_elapsed_bookings command's own audit-trail creation.
"""

import datetime

from django.contrib.admin.sites import AdminSite
from django.urls import reverse
from django.utils import timezone

from core.admin import AuditLogAdmin
from core.models import AuditLog, Booking
from core.tests import CircleCoreTenantTestCase, activate_trial, make_guest, make_owner, make_room


class AuditLogAdminPermissionUnitTest(CircleCoreTenantTestCase):
    """Direct checks against the ModelAdmin's permission methods, independent
    of any particular URL routing."""

    def setUp(self):
        self.owner = make_owner()
        self.admin_instance = AuditLogAdmin(AuditLog, AdminSite())

    def test_has_delete_permission_is_false_for_superuser(self):
        request = type("Request", (), {"user": self.owner})()
        self.assertFalse(self.admin_instance.has_delete_permission(request))

    def test_has_delete_permission_is_false_for_specific_object(self):
        AuditLog.objects.create(action="update", object_type="Booking", object_id="1", reason="test")
        entry = AuditLog.objects.first()
        request = type("Request", (), {"user": self.owner})()
        self.assertFalse(self.admin_instance.has_delete_permission(request, obj=entry))

    def test_has_add_permission_still_false(self):
        request = type("Request", (), {"user": self.owner})()
        self.assertFalse(self.admin_instance.has_add_permission(request))

    def test_has_change_permission_still_false(self):
        request = type("Request", (), {"user": self.owner})()
        self.assertFalse(self.admin_instance.has_change_permission(request))


class AuditLogAdminHttpTest(CircleCoreTenantTestCase):
    """End-to-end checks through the actual admin URLs and views, using a
    real superuser session — this is the exact path a tenant owner would use."""

    def setUp(self):
        self.owner = make_owner()
        activate_trial(self.owner)
        self.client.login(username="owner", password="testpass123")
        self.entry = AuditLog.objects.create(
            action="update", object_type="Booking", object_id="999",
            object_repr="CCG-TEST-0001 - Some Guest", reason="Automatic elapsed-booking reconciliation",
        )

    def test_tenant_owner_superuser_cannot_delete_via_admin_delete_view(self):
        # Tenant owners are provisioned as is_superuser=True in this app —
        # this is exactly the "tenant owner marked is_superuser=True" case.
        self.assertTrue(self.owner.is_superuser)
        url = reverse("admin:core_auditlog_delete", args=[self.entry.pk])
        response = self.client.post(url, {"post": "yes"})
        self.assertIn(response.status_code, (403, 302))
        self.assertTrue(AuditLog.objects.filter(pk=self.entry.pk).exists())

    def test_delete_confirmation_page_itself_is_blocked(self):
        url = reverse("admin:core_auditlog_delete", args=[self.entry.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_bulk_delete_selected_action_cannot_remove_rows(self):
        url = reverse("admin:core_auditlog_changelist")
        response = self.client.post(
            url,
            {"action": "delete_selected", "_selected_action": [str(self.entry.pk)], "index": "0"},
        )
        # Whatever the response (400/403/302 depending on whether the action
        # is even offered), the row must still exist afterward.
        self.assertTrue(AuditLog.objects.filter(pk=self.entry.pk).exists())

    def test_changelist_does_not_offer_delete_selected_action(self):
        url = reverse("admin:core_auditlog_changelist")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'value="delete_selected"')

    def test_add_view_is_blocked(self):
        url = reverse("admin:core_auditlog_add")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_change_view_is_readonly_not_editable(self):
        # With has_view_permission (default True) but has_change_permission
        # False, Django renders the change page as a read-only detail view
        # (200) rather than blocking it outright — that's correct per
        # requirement 5 ("do not prevent viewing"). What must actually be
        # blocked is persisting an edit via POST.
        url = reverse("admin:core_auditlog_change", args=[self.entry.pk])
        get_response = self.client.get(url)
        self.assertEqual(get_response.status_code, 200)

        post_response = self.client.post(url, {"reason": "tampered", "action": "update", "object_type": "Booking"})
        self.assertEqual(post_response.status_code, 403)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.reason, "Automatic elapsed-booking reconciliation")

    def test_authorised_admin_can_still_view_records(self):
        changelist_url = reverse("admin:core_auditlog_changelist")
        response = self.client.get(changelist_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "CCG-TEST-0001")


class AuditLogApplicationBehaviorTest(CircleCoreTenantTestCase):
    """Confirms the admin hardening doesn't affect anything outside the
    admin UI: application code can still write AuditLog rows, and deleting a
    Booking still leaves its audit trail alone (no FK to cascade along)."""

    def setUp(self):
        self.owner = make_owner()

    def test_application_code_can_still_create_audit_rows(self):
        before_count = AuditLog.objects.count()
        AuditLog.objects.create(
            actor=self.owner, action="create", object_type="Booking", object_id="42",
            object_repr="CCG-TEST-0042 - Someone", reason="Direct application write",
        )
        self.assertEqual(AuditLog.objects.count(), before_count + 1)

    def test_deleting_a_booking_does_not_delete_its_audit_entries(self):
        room = make_room()
        guest = make_guest()
        check_in = timezone.localdate() + datetime.timedelta(days=1)
        booking = Booking.objects.create(
            room=room, guest=guest, check_in_date=check_in,
            check_out_date=check_in + datetime.timedelta(days=1),
            booking_duration_type="daily", num_guests=1,
            rate_per_night=room.price_per_night, status="Confirmed", booking_source="Walk-in",
        )
        booking_id = str(booking.pk)  # captured before delete() — Django nulls booking.pk after deletion
        AuditLog.objects.create(
            actor=self.owner, action="update", object_type="Booking", object_id=booking_id,
            object_repr=str(booking)[:255], reason="Automatic elapsed-booking reconciliation",
        )
        audit_count_before = AuditLog.objects.filter(object_type="Booking", object_id=booking_id).count()
        self.assertEqual(audit_count_before, 1)

        booking.delete()

        audit_count_after = AuditLog.objects.filter(object_type="Booking", object_id=booking_id).count()
        self.assertEqual(audit_count_after, 1, "AuditLog has no FK to Booking, so deleting it must not remove the audit trail")

    def test_existing_audit_records_are_unaffected_by_this_test_suite(self):
        AuditLog.objects.create(action="update", object_type="Booking", object_id="1", reason="pre-existing")
        snapshot_before = list(AuditLog.objects.values("id", "action", "object_type", "object_id", "reason"))
        # Simulate the kind of no-op admin interaction a read-only viewer would do.
        AdminSite()
        snapshot_after = list(AuditLog.objects.values("id", "action", "object_type", "object_id", "reason"))
        self.assertEqual(snapshot_before, snapshot_after)
