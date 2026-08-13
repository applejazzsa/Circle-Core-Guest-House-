from dataclasses import dataclass
from importlib import import_module

from django.conf import settings

from .errors import ControlAPIError


@dataclass(frozen=True)
class OperationContext:
    principal: object
    operation_id: str
    correlation_id: str
    idempotency_key: str
    requested_by: str
    requester_role: str
    reason: str
    requested_at: str
    approval_reference: str
    expected_before_state: object


class BaseProductControlBackend:
    """Product extension point. Implementations must enforce their own domain authorization and invariants."""

    def health(self, context):
        return {"status": "healthy"}

    def capabilities(self, context):
        return {
            "contract_version": "1.0",
            "capabilities": {
                name: False for name in (
                    "tenant_creation", "trials", "subscriptions", "manual_payments", "suspension",
                    "account_unlocking", "session_revocation", "product_enablement", "branches",
                    "properties", "multiple_locations", "background_job_retry", "maintenance_mode",
                    "compensating_operations",
                    "tenant_read", "tenant_activation", "trial_extension", "subscription_plan_change",
                    "trial_conversion",
                    "subscription_grace_period", "subscription_cancellation", "archiving",
                    "administrator_invitations", "password_reset", "force_password_reset",
                    "user_management", "user_role_management",
                )
            },
        }

    def read(self, resource, identifiers, context):
        raise ControlAPIError("capability_not_supported", "This read capability is not implemented by the product.", status=501)

    def execute(self, action, identifiers, payload, context):
        raise ControlAPIError("capability_not_supported", "This operation is not implemented by the product.", status=501)


class UnavailableProductControlBackend(BaseProductControlBackend):
    def health(self, context):
        return {"status": "degraded", "reason": "product_backend_not_configured"}


def load_backend():
    path = getattr(
        settings,
        "PRODUCT_CONTROL_API_BACKEND",
        "circle_core_control_api.backends.UnavailableProductControlBackend",
    )
    module_name, class_name = path.rsplit(".", 1)
    backend_class = getattr(import_module(module_name), class_name)
    return backend_class()
