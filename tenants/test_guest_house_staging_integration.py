import json
import time
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.core import mail
from django.core.management import call_command
from django.http import HttpResponse
from django.test import RequestFactory, TransactionTestCase, modify_settings, override_settings
from django.utils import timezone
from django_tenants.utils import schema_context

from circle_core_control_api.authentication import sign_request
from circle_core_control_api.models import IdempotencyRecord, ProductControlAuditEvent
from core.middleware import SubscriptionMiddleware
from core.models import ControlManualPayment, Property, Subscription
from tenants.models import ControlActivationOutbox, ControlOperationNotification, GuestHouseTenant


SECRET = "guest-house-staging-contract-secret-000000000000"
KEYS = {
    "smart-control-staging": {
        "secret": SECRET,
        "identity": "smart-control-staging",
        "permissions": ["*"],
        "allowed_ips": ["127.0.0.1/32"],
        "enabled": True,
    },
}


@modify_settings(MIDDLEWARE={"remove": "django_tenants.middleware.main.TenantMainMiddleware"})
@override_settings(
    ROOT_URLCONF="config.urls_public",
    SECURE_SSL_REDIRECT=False,
    ENVIRONMENT="test",
    PRODUCT_CONTROL_API_ENABLED=True,
    PRODUCT_CONTROL_API_REQUIRE_HTTPS=True,
    PRODUCT_CONTROL_API_KEYS=KEYS,
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="staging-no-reply@example.invalid",
)
class GuestHouseStagingIntegrationTests(TransactionTestCase):
    """A signed, HTTP-level Smart Control simulation using only synthetic staging identities."""

    reset_sequences = True

    def tearDown(self):
        ControlActivationOutbox.objects.all().delete()
        ControlOperationNotification.objects.all().delete()
        for tenant in GuestHouseTenant.objects.exclude(schema_name="public"):
            tenant.delete(force_drop=True)
        super().tearDown()

    def headers(self, method, path, body=b"", *, timestamp=None, nonce=None, correlation_id=None):
        timestamp = str(timestamp if timestamp is not None else int(time.time()))
        nonce = nonce or uuid.uuid4().hex
        return {
            "HTTP_X_CONTROL_KEY_ID": "smart-control-staging",
            "HTTP_X_CONTROL_TIMESTAMP": timestamp,
            "HTTP_X_CONTROL_NONCE": nonce,
            "HTTP_X_CONTROL_SIGNATURE": sign_request(SECRET, method, path, timestamp, nonce, body),
            "HTTP_X_CORRELATION_ID": str(correlation_id or uuid.uuid4()),
            "REMOTE_ADDR": "127.0.0.1",
        }

    def get(self, path, correlation_id=None):
        return self.client.get(path, secure=True, **self.headers("GET", path, correlation_id=correlation_id))

    def post(self, path, payload, *, expected=None, operation_id=None, correlation_id=None,
             idempotency_key=None, approval=True):
        operation_id = operation_id or uuid.uuid4()
        correlation_id = correlation_id or uuid.uuid4()
        idempotency_key = idempotency_key or uuid.uuid4()
        envelope = {
            "operation_id": str(operation_id), "correlation_id": str(correlation_id),
            "idempotency_key": str(idempotency_key), "requested_by": "staging-admin@circlecore.co.za",
            "requester_role": "platform_administrator", "reason": "Synthetic Guest House staging integration verification",
            "requested_at": timezone.now().isoformat(), "expected_before_state": expected or {},
            "payload": payload,
        }
        if approval:
            envelope["approval_reference"] = f"staging-approval-{operation_id}"
        body = json.dumps(envelope, separators=(",", ":")).encode()
        headers = self.headers("POST", path, body, correlation_id=correlation_id)
        headers.update({"HTTP_X_OPERATION_ID": str(operation_id), "HTTP_IDEMPOTENCY_KEY": str(idempotency_key)})
        response = self.client.post(path, data=body, content_type="application/json", secure=True, **headers)
        return response, body, headers, operation_id, correlation_id, idempotency_key

    def tenant_payload(self):
        now = timezone.now()
        return {
            "legal_or_trading_name": "Synthetic Staging Lodge (Pty) Ltd",
            "tenant_display_name": "Synthetic Staging Lodge", "product": "guest-house",
            "plan": "professional", "billing_cycle": "monthly", "subscription_kind": "trial",
            "trial_start": now.isoformat(), "trial_expiry": (now + timedelta(days=30)).isoformat(),
            "next_billing_date": (timezone.localdate() + timedelta(days=30)).isoformat(),
            "primary_administrator": {
                "name": "Synthetic Administrator", "email": "guest-house-admin@example.invalid",
                "phone": "+27000000000", "send_activation_invitation": True,
                "force_secure_password_creation": True,
            },
            "primary_contact": {"email": "guest-house-contact@example.invalid", "phone": "+27000000001"},
            "timezone": "Africa/Johannesburg", "currency": "ZAR", "country": "ZA",
            "primary_location": {
                "property_name": "Synthetic Staging Property", "property_type": "lodge",
                "number_of_rooms": 12, "physical_location": "Anonymised staging address",
            },
            "initial_feature_flags": {},
            "external_smart_control_tenant_reference": str(uuid.uuid4()),
        }

    def assert_audit(self, response, operation_id, correlation_id):
        payload = response.json()
        self.assertTrue(payload.get("product_audit_id"))
        audit = self.get(f"/internal/control/v1/audits/{payload['product_audit_id']}", correlation_id)
        self.assertEqual(audit.status_code, 200)
        evidence = audit.json()["data"]
        self.assertEqual(evidence["operation_id"], str(operation_id))
        self.assertEqual(evidence["correlation_id"], str(correlation_id))
        self.assertEqual(evidence["outcome"], "completed")
        return payload

    def test_complete_synthetic_smart_control_to_guest_house_path(self):
        self.assertEqual(self.get("/internal/control/v1/health").json()["data"]["status"], "healthy")
        capabilities = self.get("/internal/control/v1/capabilities").json()["data"]["capabilities"]
        for capability in (
            "tenant_creation", "tenant_read", "trials", "trial_extension", "tenant_activation",
            "suspension", "subscription_plan_change", "manual_payments", "password_reset",
            "account_unlocking", "session_revocation", "product_enablement",
        ):
            self.assertTrue(capabilities[capability], capability)
        plans = self.get("/internal/control/v1/plans").json()["data"]["plans"]
        self.assertEqual({item["code"] for item in plans}, {"starter", "professional", "enterprise"})

        created, body, headers, operation_id, correlation_id, _ = self.post(
            "/internal/control/v1/tenants", self.tenant_payload(), approval=False,
        )
        self.assertEqual(created.status_code, 201)
        created_data = self.assert_audit(created, operation_id, correlation_id)["data"]
        tenant_id, user_id = created_data["tenant_id"], created_data["administrator_id"]
        tenant = GuestHouseTenant.objects.get(pk=tenant_id)
        self.assertEqual(created_data["activation_email_status"], "queued")
        self.assertEqual(ControlActivationOutbox.objects.get().state, "pending")
        call_command("dispatch_control_activations", limit=5, verbosity=0)
        self.assertEqual(ControlActivationOutbox.objects.get().state, "sent")
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(mail.outbox[0].to[0].endswith("@example.invalid"))
        self.assertNotIn("password=", mail.outbox[0].body)

        duplicate_headers = self.headers("POST", "/internal/control/v1/tenants", body, correlation_id=correlation_id)
        duplicate_headers.update({"HTTP_X_OPERATION_ID": headers["HTTP_X_OPERATION_ID"], "HTTP_IDEMPOTENCY_KEY": headers["HTTP_IDEMPOTENCY_KEY"]})
        duplicate = self.client.post("/internal/control/v1/tenants", data=body, content_type="application/json", secure=True, **duplicate_headers)
        self.assertEqual(duplicate.status_code, 201)
        self.assertEqual(duplicate.json(), created.json())
        self.assertEqual(GuestHouseTenant.objects.exclude(schema_name="public").count(), 1)

        listing = self.get("/internal/control/v1/tenants?page_size=10").json()["data"]
        self.assertEqual([item["tenant_id"] for item in listing["results"]], [tenant_id])
        current = self.get(f"/internal/control/v1/tenants/{tenant_id}").json()["data"]
        self.assertEqual(current["location_count"], 1)
        self.assertEqual(current["status"], "trialing")

        def action(path, payload, expected):
            response, _, _, op_id, corr_id, _ = self.post(path, payload, expected=expected)
            self.assertEqual(response.status_code, 200, response.content)
            return self.assert_audit(response, op_id, corr_id)

        extended = action(f"/internal/control/v1/tenants/{tenant_id}/trial/extend", {"trial_days": 7}, current)
        current = extended["after_state"]
        changed = action(f"/internal/control/v1/tenants/{tenant_id}/subscription/change-plan", {
            "plan_code": "enterprise", "effective_at": timezone.now().isoformat(),
            "proration_policy": "no_proration_staging_test",
        }, current)
        current = changed["after_state"]
        payment_path = f"/internal/control/v1/tenants/{tenant_id}/subscription/manual-payment"
        payment_payload = {
            "amount": "500.00", "currency": "ZAR", "payment_date": timezone.localdate().isoformat(),
            "payment_method_category": "eft", "internal_reference": f"STAGE-{tenant_id}",
            "invoice_reference": "SYNTHETIC-INVOICE", "activate_after_payment": False,
        }
        payment, payment_body, payment_headers, pay_op, pay_corr, _ = self.post(payment_path, payment_payload, expected=current)
        self.assertEqual(payment.status_code, 200)
        payment_result = self.assert_audit(payment, pay_op, pay_corr)
        self.assertEqual(payment_result["data"]["verification_status"], "pending_verification")
        current = payment_result["after_state"]

        activated = action(f"/internal/control/v1/tenants/{tenant_id}/activate", {}, current)
        current = activated["after_state"]
        self.assertEqual(current["status"], "active")
        reset = action(f"/internal/control/v1/users/{user_id}/send-password-reset", {"tenant_id": tenant_id}, current)
        self.assertEqual(reset["data"]["delivery_status"], "queued")
        self.assertNotIn("reset_token", json.dumps(reset["data"]).lower())
        self.assertNotIn("reset_url", json.dumps(reset["data"]).lower())

        with schema_context(tenant.schema_name):
            user = get_user_model().objects.get(pk=user_id)
            user.is_active = False
            user.save(update_fields=["is_active"])
        unlocked = action(f"/internal/control/v1/users/{user_id}/unlock", {"tenant_id": tenant_id}, current)
        self.assertTrue(unlocked["data"]["account_unlocked"])
        with schema_context(tenant.schema_name):
            session = SessionStore()
            session["_auth_user_id"] = str(user_id)
            session["_circle_core_tenant_schema"] = tenant.schema_name
            session.save()
        revoked = action(f"/internal/control/v1/users/{user_id}/revoke-sessions", {"tenant_id": tenant_id}, current)
        self.assertEqual(revoked["data"]["revoked_session_count"], 1)

        disabled = action(f"/internal/control/v1/tenants/{tenant_id}/products/disable", {}, current)
        self.assertFalse(disabled["after_state"]["product_enabled"])
        enabled = action(f"/internal/control/v1/tenants/{tenant_id}/products/enable", {}, disabled["after_state"])
        current = enabled["after_state"]

        with schema_context(tenant.schema_name):
            preserved = (Property.objects.count(), get_user_model().objects.count(), Subscription.objects.count())
        suspended = action(f"/internal/control/v1/tenants/{tenant_id}/suspend", {}, current)
        self.assertEqual(suspended["after_state"]["status"], "suspended")
        request = RequestFactory().get("/bookings/")
        request.user, request.tenant, request.session = user, tenant, {}
        with schema_context(tenant.schema_name):
            with patch("core.middleware.render", return_value=HttpResponse(status=402)) as render_blocked:
                blocked = SubscriptionMiddleware(lambda request: HttpResponse(status=204))(request)
            self.assertEqual(blocked.status_code, 402)
            self.assertEqual(render_blocked.call_args.args[1], "subscription/cancelled.html")
            self.assertEqual((Property.objects.count(), get_user_model().objects.count(), Subscription.objects.count()), preserved)
        reactivated = action(f"/internal/control/v1/tenants/{tenant_id}/reactivate", {}, suspended["after_state"])
        self.assertEqual(reactivated["after_state"]["status"], "active")
        with schema_context(tenant.schema_name):
            restored = SubscriptionMiddleware(lambda request: HttpResponse(status=204))(request)
            self.assertEqual(restored.status_code, 204)
            self.assertEqual(ControlManualPayment.objects.count(), 1)

        lookup = self.get(f"/internal/control/v1/operations/{pay_op}", pay_corr)
        self.assertEqual(lookup.json()["data"]["state"], "completed")
        replay_headers = self.headers("POST", payment_path, payment_body, correlation_id=pay_corr)
        replay_headers.update({"HTTP_X_OPERATION_ID": payment_headers["HTTP_X_OPERATION_ID"], "HTTP_IDEMPOTENCY_KEY": payment_headers["HTTP_IDEMPOTENCY_KEY"]})
        replay = self.client.post(payment_path, data=payment_body, content_type="application/json", secure=True, **replay_headers)
        self.assertEqual(replay.status_code, 200)
        with schema_context(tenant.schema_name):
            self.assertEqual(ControlManualPayment.objects.count(), 1)
        self.assertTrue(ProductControlAuditEvent.objects.filter(outcome="duplicate", operation_id=pay_op).exists())
        self.assertEqual(IdempotencyRecord.objects.filter(operation_id=pay_op).count(), 1)

    def test_conflicts_invalid_plan_and_authentication_fail_closed(self):
        bad = self.tenant_payload()
        bad["plan"] = "invalid"
        response = self.post("/internal/control/v1/tenants", bad, approval=False)[0]
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error_code"], "invalid_plan")

        path = "/internal/control/v1/health"
        expired = self.client.get(path, secure=True, **self.headers("GET", path, timestamp=int(time.time()) - 1000))
        self.assertEqual(expired.json()["error_code"], "expired_timestamp")
        nonce = uuid.uuid4().hex
        replay_headers = self.headers("GET", path, nonce=nonce)
        self.assertEqual(self.client.get(path, secure=True, **replay_headers).status_code, 200)
        self.assertEqual(self.client.get(path, secure=True, **replay_headers).json()["error_code"], "replay_detected")
