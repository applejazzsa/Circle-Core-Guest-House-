from circle_core_control_api.backends import BaseProductControlBackend
from circle_core_control_api.errors import ControlAPIError
from circle_core_control_api.models import ProductControlAuditEvent


class TestProductBackend(BaseProductControlBackend):
    calls = 0
    tenants = {}

    @classmethod
    def reset(cls):
        cls.calls = 0
        cls.tenants = {}

    def capabilities(self, context):
        result = super().capabilities(context)
        result["capabilities"]["tenant_creation"] = True
        result["product"] = "reference-test-product"
        return result

    def read(self, resource, identifiers, context):
        return {"resource": resource, "identifiers": identifiers}

    def execute(self, action, identifiers, payload, context):
        type(self).calls += 1
        if action == "create_tenant":
            reference = payload["external_smart_control_tenant_reference"]
            if reference in type(self).tenants:
                raise ControlAPIError("tenant_already_exists", "A tenant already exists for this reference.", status=409)
            tenant_id = f"tenant-{len(type(self).tenants) + 1}"
            type(self).tenants[reference] = tenant_id
            return {
                "before_state": {},
                "after_state": {"tenant_id": tenant_id, "state": "trialing"},
                "data": {"tenant_id": tenant_id, "administrator_id": "admin-1", "activation_queued": True},
                "reversible": True,
                "compensating_action": "archive_tenant",
                "http_status": 201,
            }
        return {
            "before_state": {"status": "active"},
            "after_state": {"status": "active"},
            "data": {},
        }


class RollbackProductBackend(BaseProductControlBackend):
    def execute(self, action, identifiers, payload, context):
        ProductControlAuditEvent.objects.create(
            action="temporary_backend_side_effect",
            outcome="accepted",
            caller_identity=context.principal.identity,
        )
        raise ControlAPIError("provisioning_step_failed", "A required provisioning step failed.", status=422)


class CrashingProductBackend(BaseProductControlBackend):
    def execute(self, action, identifiers, payload, context):
        raise RuntimeError("database-password=must-never-leak")
