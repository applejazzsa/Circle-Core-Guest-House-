import json
import time
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, modify_settings, override_settings
from django.urls import resolve
from django.utils import timezone

from circle_core_control_api.authentication import sign_request
from circle_core_control_api.models import IdempotencyRecord, ProductControlAuditEvent, RequestNonce
from circle_core_control_api.tests.backends import TestProductBackend


SECRET = "test-signing-secret-with-at-least-32-bytes"
KEYS = {
    "active-key": {
        "secret": SECRET,
        "identity": "smart-control-staging",
        "permissions": ["*"],
        "tenant_allowlist": ["tenant-allowed"],
        "allowed_ips": ["127.0.0.1/32"],
        "enabled": True,
    }
}


@modify_settings(MIDDLEWARE={"remove": "django_tenants.middleware.main.TenantMainMiddleware"})
@override_settings(
    ROOT_URLCONF="config.urls_public",
    SECURE_SSL_REDIRECT=False,
    PRODUCT_CONTROL_API_ENABLED=True,
    PRODUCT_CONTROL_API_REQUIRE_HTTPS=True,
    PRODUCT_CONTROL_API_KEYS=KEYS,
    PRODUCT_CONTROL_API_BACKEND="circle_core_control_api.tests.backends.TestProductBackend",
    PRODUCT_CONTROL_API_RATE_LIMIT_PER_MINUTE=100,
)
class ProductControlAPITests(TestCase):
    def setUp(self):
        TestProductBackend.reset()

    def _headers(self, method, path, body=b"", *, timestamp=None, nonce=None, secret=SECRET, key_id="active-key", extra=None):
        timestamp = str(timestamp if timestamp is not None else int(time.time()))
        nonce = nonce or uuid.uuid4().hex
        headers = {
            "HTTP_X_CONTROL_KEY_ID": key_id,
            "HTTP_X_CONTROL_TIMESTAMP": timestamp,
            "HTTP_X_CONTROL_NONCE": nonce,
            "HTTP_X_CONTROL_SIGNATURE": sign_request(secret, method, path, timestamp, nonce, body),
            "HTTP_X_CORRELATION_ID": str(uuid.uuid4()),
            "REMOTE_ADDR": "127.0.0.1",
        }
        headers.update(extra or {})
        return headers

    def _tenant_payload(self, reference="scc-tenant-1"):
        return {
            "legal_or_trading_name": "Example Trading",
            "tenant_display_name": "Example Tenant",
            "product": "business-desk",
            "plan": "starter",
            "billing_cycle": "monthly",
            "trial_start": timezone.now().isoformat(),
            "trial_expiry": (timezone.now() + timedelta(days=14)).isoformat(),
            "primary_administrator": {"name": "Primary Admin", "email": "admin@example.invalid", "phone": "+27000000000"},
            "timezone": "Africa/Johannesburg",
            "currency": "ZAR",
            "country": "ZA",
            "primary_location": {"name": "Head Office"},
            "initial_feature_flags": {},
            "external_smart_control_tenant_reference": reference,
        }

    def _write_body(self, payload=None, *, operation_id=None, correlation_id=None, idempotency_key=None, approval_reference=None):
        operation_id = operation_id or uuid.uuid4()
        correlation_id = correlation_id or uuid.uuid4()
        idempotency_key = idempotency_key or uuid.uuid4()
        value = {
            "operation_id": str(operation_id),
            "correlation_id": str(correlation_id),
            "idempotency_key": str(idempotency_key),
            "requested_by": "owner@circlecore.co.za",
            "requester_role": "platform_owner",
            "reason": "Approved staging contract verification",
            "requested_at": timezone.now().isoformat(),
            "expected_before_state": {},
            "payload": payload if payload is not None else self._tenant_payload(),
        }
        if approval_reference is not None:
            value["approval_reference"] = approval_reference
        return json.dumps(value, separators=(",", ":")).encode(), operation_id, correlation_id, idempotency_key

    def _post(self, path="/internal/control/v1/tenants", payload=None, **body_options):
        body, operation_id, correlation_id, idempotency_key = self._write_body(payload, **body_options)
        headers = self._headers("POST", path, body, extra={
            "HTTP_X_OPERATION_ID": str(operation_id),
            "HTTP_X_CORRELATION_ID": str(correlation_id),
            "HTTP_IDEMPOTENCY_KEY": str(idempotency_key),
        })
        return self.client.post(path, data=body, content_type="application/json", secure=True, **headers), body, headers

    def test_valid_signed_authentication_and_capabilities(self):
        path = "/internal/control/v1/capabilities"
        response = self.client.get(path, secure=True, **self._headers("GET", path))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["data"]["capabilities"]["tenant_creation"])
        self.assertEqual(RequestNonce.objects.count(), 1)

    @override_settings(PRODUCT_CONTROL_API_KEYS={
        "env-key": {
            "secret_env": "GUEST_HOUSE_TEST_CONTROL_SECRET", "identity": "smart-control-staging-env",
            "permissions": ["read.health"], "allowed_ips": ["127.0.0.1/32"], "enabled": True,
        },
    })
    def test_signing_secret_can_be_resolved_without_embedding_it_in_key_json(self):
        path = "/internal/control/v1/health"
        with patch.dict("os.environ", {"GUEST_HOUSE_TEST_CONTROL_SECRET": SECRET}):
            response = self.client.get(
                path, secure=True, **self._headers("GET", path, key_id="env-key"),
            )
        self.assertEqual(response.status_code, 200)

    def test_required_endpoint_routes_are_registered(self):
        paths = [
            "/internal/control/v1/health", "/internal/control/v1/capabilities", "/internal/control/v1/tenants",
            "/internal/control/v1/plans", "/internal/control/v1/audits/00000000-0000-0000-0000-000000000001",
            "/internal/control/v1/tenants/t-1", "/internal/control/v1/tenants/t-1/users",
            "/internal/control/v1/tenants/t-1/subscription", "/internal/control/v1/tenants/t-1/activity",
            "/internal/control/v1/operations/00000000-0000-0000-0000-000000000001", "/internal/control/v1/jobs/j-1",
            "/internal/control/v1/tenants/t-1/activate", "/internal/control/v1/tenants/t-1/suspend",
            "/internal/control/v1/tenants/t-1/reactivate", "/internal/control/v1/tenants/t-1/archive",
            "/internal/control/v1/tenants/t-1/trial/extend", "/internal/control/v1/tenants/t-1/subscription/change-plan",
            "/internal/control/v1/tenants/t-1/subscription/manual-payment", "/internal/control/v1/tenants/t-1/subscription/grace-period",
            "/internal/control/v1/tenants/t-1/subscription/cancel", "/internal/control/v1/tenants/t-1/products/enable",
            "/internal/control/v1/tenants/t-1/products/disable", "/internal/control/v1/tenants/t-1/users/invite-admin",
            "/internal/control/v1/users/u-1/send-password-reset", "/internal/control/v1/users/u-1/force-password-reset",
            "/internal/control/v1/users/u-1/unlock", "/internal/control/v1/users/u-1/revoke-sessions",
            "/internal/control/v1/jobs/j-1/retry",
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(resolve(path).namespace, "product_control_api")

    def test_https_and_json_content_type_are_required(self):
        path = "/internal/control/v1/health"
        insecure = self.client.get(path, **self._headers("GET", path))
        self.assertEqual(insecure.status_code, 401)
        body, operation_id, correlation_id, idempotency_key = self._write_body()
        headers = self._headers("POST", "/internal/control/v1/tenants", body, extra={
            "HTTP_X_OPERATION_ID": str(operation_id), "HTTP_X_CORRELATION_ID": str(correlation_id),
            "HTTP_IDEMPOTENCY_KEY": str(idempotency_key),
        })
        wrong_type = self.client.post("/internal/control/v1/tenants", data=body, content_type="text/plain", secure=True, **headers)
        self.assertEqual(wrong_type.status_code, 415)

    def test_invalid_signature_is_rejected_safely(self):
        path = "/internal/control/v1/health"
        response = self.client.get(path, secure=True, **self._headers("GET", path, secret="wrong-secret-that-is-still-long-enough"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error_code"], "invalid_signature")
        self.assertNotContains(response, "Traceback", status_code=401)

    def test_expired_timestamp_is_rejected(self):
        path = "/internal/control/v1/health"
        response = self.client.get(path, secure=True, **self._headers("GET", path, timestamp=int(time.time()) - 1000))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error_code"], "expired_timestamp")

    def test_nonce_replay_is_rejected(self):
        path = "/internal/control/v1/health"
        timestamp = int(time.time())
        nonce = uuid.uuid4().hex
        headers = self._headers("GET", path, timestamp=timestamp, nonce=nonce)
        self.assertEqual(self.client.get(path, secure=True, **headers).status_code, 200)
        replay = self.client.get(path, secure=True, **headers)
        self.assertEqual(replay.status_code, 401)
        self.assertEqual(replay.json()["error_code"], "replay_detected")

    def test_idempotent_replay_returns_original_result_once(self):
        first, body, first_headers = self._post()
        self.assertEqual(first.status_code, 201)
        second_headers = self._headers("POST", "/internal/control/v1/tenants", body, extra={
            "HTTP_X_OPERATION_ID": first_headers["HTTP_X_OPERATION_ID"],
            "HTTP_X_CORRELATION_ID": first_headers["HTTP_X_CORRELATION_ID"],
            "HTTP_IDEMPOTENCY_KEY": first_headers["HTTP_IDEMPOTENCY_KEY"],
        })
        second = self.client.post("/internal/control/v1/tenants", data=body, content_type="application/json", secure=True, **second_headers)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.json(), first.json())
        self.assertEqual(TestProductBackend.calls, 1)
        self.assertEqual(IdempotencyRecord.objects.count(), 1)
        self.assertTrue(ProductControlAuditEvent.objects.filter(outcome="duplicate").exists())

    def test_duplicate_tenant_reference_is_rejected(self):
        self.assertEqual(self._post()[0].status_code, 201)
        duplicate = self._post()[0]
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()["error_code"], "tenant_already_exists")

    @override_settings(PRODUCT_CONTROL_API_KEYS={
        "active-key": {"secret": SECRET, "identity": "read-only-caller", "permissions": ["read.health"], "enabled": True}
    })
    def test_permission_denial(self):
        response = self._post()[0]
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error_code"], "permission_denied")

    def test_success_creates_immutable_product_audit(self):
        response = self._post()[0]
        audit = ProductControlAuditEvent.objects.get(id=response.json()["product_audit_id"])
        self.assertEqual(audit.outcome, "completed")
        path = f"/internal/control/v1/audits/{audit.id}"
        confirmation = self.client.get(path, secure=True, **self._headers("GET", path))
        self.assertEqual(confirmation.status_code, 200)
        self.assertEqual(confirmation.json()["data"]["audit_id"], str(audit.id))
        self.assertEqual(confirmation.json()["data"]["operation_id"], str(audit.operation_id))
        with self.assertRaises(RuntimeError):
            audit.delete()

    @override_settings(PRODUCT_CONTROL_API_BACKEND="circle_core_control_api.tests.backends.RollbackProductBackend")
    def test_required_step_failure_rolls_back_product_transaction(self):
        response = self._post()[0]
        self.assertEqual(response.status_code, 422)
        self.assertFalse(ProductControlAuditEvent.objects.filter(action="temporary_backend_side_effect").exists())
        self.assertTrue(ProductControlAuditEvent.objects.filter(action="create_tenant", error_code="provisioning_step_failed").exists())

    @override_settings(PRODUCT_CONTROL_API_BACKEND="circle_core_control_api.tests.backends.CrashingProductBackend")
    def test_unexpected_errors_do_not_expose_stack_or_secrets(self):
        response = self._post()[0]
        content = response.content.decode()
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error_code"], "internal_error")
        self.assertNotIn("database-password", content)
        self.assertNotIn("Traceback", content)

    def test_cross_tenant_access_is_denied_before_backend(self):
        path = "/internal/control/v1/tenants/tenant-denied"
        response = self.client.get(path, secure=True, **self._headers("GET", path))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error_code"], "permission_denied")

    def test_api_is_hidden_when_staging_flag_is_disabled(self):
        path = "/internal/control/v1/health"
        with override_settings(PRODUCT_CONTROL_API_ENABLED=False):
            response = self.client.get(path, secure=True, **self._headers("GET", path))
        self.assertEqual(response.status_code, 404)
