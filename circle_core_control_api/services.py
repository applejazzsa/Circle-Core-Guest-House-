import json
import hashlib
from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .authentication import body_digest
from .backends import OperationContext, load_backend
from .errors import ControlAPIError
from .models import IdempotencyRecord, ProductControlAuditEvent


@dataclass(frozen=True)
class ServiceResponse:
    body: dict
    status: int


def safe_error_body(error, *, correlation_id="", product_audit_id=""):
    body = {
        "error_code": error.error_code,
        "safe_error_message": error.safe_message,
        "retryable": error.retryable,
        "correlation_id": str(correlation_id or ""),
        "product_audit_id": str(product_audit_id or ""),
    }
    if error.current_state is not None:
        body["current_state"] = error.current_state
    return body


def record_rejection(*, action, principal=None, envelope=None, error, request_digest="", target_reference=""):
    envelope = envelope or {}
    try:
        return ProductControlAuditEvent.objects.create(
            operation_id=envelope.get("operation_id"),
            correlation_id=envelope.get("correlation_id"),
            caller_identity=getattr(principal, "identity", ""),
            requested_by=envelope.get("requested_by", ""),
            requester_role=envelope.get("requester_role", ""),
            reason=envelope.get("reason", ""),
            action=action[:100],
            target_reference=str(target_reference)[:200],
            request_digest=request_digest,
            outcome="rejected",
            error_code=error.error_code[:80],
            metadata={"retryable": error.retryable, "http_status": error.status},
        )
    except Exception:
        return None


def _ensure_json_safe(value, field):
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        raise ControlAPIError("invalid_product_response", f"Product backend returned invalid {field}.", status=500)


def _validate_backend_result(result):
    if not isinstance(result, dict):
        raise ControlAPIError("invalid_product_response", "Product backend returned an invalid response.", status=500)
    before = result.get("before_state", {})
    after = result.get("after_state", {})
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise ControlAPIError("invalid_product_response", "Product state evidence must be objects.", status=500)
    if result.get("result", "success") not in {"success", "accepted"}:
        raise ControlAPIError("invalid_product_response", "Product backend returned an invalid result state.", status=500)
    if not isinstance(result.get("data", {}), dict):
        raise ControlAPIError("invalid_product_response", "Product backend data must be an object.", status=500)
    if not isinstance(result.get("audit_metadata", {}), dict):
        raise ControlAPIError("invalid_product_response", "Product audit metadata must be an object.", status=500)
    if result.get("http_status", 200) not in {200, 201, 202}:
        raise ControlAPIError("invalid_product_response", "Product backend returned an invalid success status.", status=500)
    for field in ("before_state", "after_state", "data"):
        _ensure_json_safe(result.get(field, {}), field)
    return before, after


def _acquire_idempotency(principal, action, target_reference, envelope, request_digest):
    try:
        with transaction.atomic():
            return IdempotencyRecord.objects.create(
                caller_identity=principal.identity,
                idempotency_key=envelope["idempotency_key"],
                operation_id=envelope["operation_id"],
                correlation_id=envelope["correlation_id"],
                action=action,
                target_reference=str(target_reference)[:200],
                request_digest=request_digest,
            ), True
    except IntegrityError:
        existing = IdempotencyRecord.objects.filter(
            caller_identity=principal.identity,
            idempotency_key=envelope["idempotency_key"],
        ).first() or IdempotencyRecord.objects.filter(
            caller_identity=principal.identity,
            operation_id=envelope["operation_id"],
        ).first()
        if existing is None:
            raise ControlAPIError("idempotency_conflict", "The operation conflicts with an existing request.", status=409)
        if (
            existing.request_digest != request_digest
            or existing.operation_id != envelope["operation_id"]
            or existing.action != action
            or existing.target_reference != str(target_reference)[:200]
        ):
            raise ControlAPIError("idempotency_key_reused", "The idempotency key or operation ID was reused with different request data.", status=409)
        return existing, False


def execute_write(request, principal, action, identifiers, envelope):
    request_digest = body_digest(request.body)
    idempotency_payload = {key: value for key, value in envelope.items() if key != "requested_at"}
    idempotency_digest = hashlib.sha256(
        json.dumps(idempotency_payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    target_reference = identifiers.get("tenant_id") or identifiers.get("user_id") or identifiers.get("job_id") or envelope["payload"].get("external_smart_control_tenant_reference", "")
    required_approvals = getattr(settings, "PRODUCT_CONTROL_API_APPROVAL_REQUIRED_ACTIONS", set())
    if action in required_approvals and not envelope.get("approval_reference"):
        raise ControlAPIError("approval_required", "An approval reference is required for this operation.", status=403)

    record, created = _acquire_idempotency(principal, action, target_reference, envelope, idempotency_digest)
    if not created:
        if record.state in {"completed", "failed"} and record.response_status and record.response_body:
            ProductControlAuditEvent.objects.create(
                operation_id=record.operation_id,
                correlation_id=record.correlation_id,
                caller_identity=principal.identity,
                requested_by=envelope["requested_by"],
                requester_role=envelope["requester_role"],
                reason=envelope["reason"],
                action=action,
                target_reference=record.target_reference,
                request_digest=request_digest,
                outcome="duplicate",
                metadata={"original_idempotency_record": str(record.id)},
            )
            return ServiceResponse(record.response_body, record.response_status)
        raise ControlAPIError("operation_in_progress", "The operation is already being processed.", status=409, retryable=True)

    context = OperationContext(
        principal=principal,
        operation_id=str(envelope["operation_id"]),
        correlation_id=str(envelope["correlation_id"]),
        idempotency_key=str(envelope["idempotency_key"]),
        requested_by=envelope["requested_by"],
        requester_role=envelope["requester_role"],
        reason=envelope["reason"],
        requested_at=str(envelope["requested_at"]),
        approval_reference=envelope.get("approval_reference", ""),
        expected_before_state=envelope["expected_before_state"],
    )
    try:
        with transaction.atomic():
            locked = IdempotencyRecord.objects.select_for_update().get(pk=record.pk)
            result = load_backend().execute(action, identifiers, envelope["payload"], context)
            before, after = _validate_backend_result(result)
            audit_target_reference = str(result.get("audit_target_reference") or target_reference)[:200]
            audit = ProductControlAuditEvent.objects.create(
                operation_id=envelope["operation_id"],
                correlation_id=envelope["correlation_id"],
                caller_identity=principal.identity,
                requested_by=envelope["requested_by"],
                requester_role=envelope["requester_role"],
                reason=envelope["reason"],
                action=action,
                target_reference=audit_target_reference,
                request_digest=request_digest,
                outcome="completed",
                before_state=before,
                after_state=after,
                metadata={
                    **result.get("audit_metadata", {}),
                    "key_id": principal.key_id,
                    "requested_at": str(envelope["requested_at"]),
                    "approval_reference": envelope.get("approval_reference", ""),
                },
            )
            response = {
                "operation_id": str(envelope["operation_id"]),
                "product_audit_id": str(audit.id),
                "correlation_id": str(envelope["correlation_id"]),
                "result": result.get("result", "success"),
                "before_state": before,
                "after_state": after,
                "executed_at": timezone.now().isoformat(),
                "reversible": bool(result.get("reversible", False)),
                "compensating_action": result.get("compensating_action") or None,
                "data": result.get("data", {}),
            }
            locked.state = "completed"
            locked.response_status = int(result.get("http_status", 200))
            locked.response_body = response
            locked.completed_at = timezone.now()
            locked.save(update_fields=["state", "response_status", "response_body", "completed_at", "updated_at"])
        return ServiceResponse(response, locked.response_status)
    except ControlAPIError as error:
        return _record_failed_operation(record, principal, action, target_reference, envelope, request_digest, error)
    except Exception:
        error = ControlAPIError("internal_error", "The product could not complete the operation.", status=500, retryable=False)
        return _record_failed_operation(record, principal, action, target_reference, envelope, request_digest, error)


def _record_failed_operation(record, principal, action, target_reference, envelope, request_digest, error):
    audit = ProductControlAuditEvent.objects.create(
        operation_id=envelope["operation_id"],
        correlation_id=envelope["correlation_id"],
        caller_identity=principal.identity,
        requested_by=envelope["requested_by"],
        requester_role=envelope["requester_role"],
        reason=envelope["reason"],
        action=action,
        target_reference=str(target_reference)[:200],
        request_digest=request_digest,
        outcome="failed" if error.status >= 500 else "rejected",
        error_code=error.error_code,
        metadata={
            "retryable": error.retryable,
            "http_status": error.status,
            "key_id": principal.key_id,
            "requested_at": str(envelope["requested_at"]),
            "approval_reference": envelope.get("approval_reference", ""),
        },
    )
    body = safe_error_body(error, correlation_id=envelope["correlation_id"], product_audit_id=audit.id)
    IdempotencyRecord.objects.filter(pk=record.pk).update(
        state="failed", response_status=error.status, response_body=body, completed_at=timezone.now()
    )
    return ServiceResponse(body, error.status)
