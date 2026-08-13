import uuid
import logging

from django.conf import settings
from django.http import HttpResponseNotFound, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .authentication import authenticate_request, authorize, body_digest
from .backends import load_backend
from .contracts import validate_write_envelope
from .errors import ControlAPIError
from .models import IdempotencyRecord
from .models import ProductControlAuditEvent
from .services import execute_write, record_rejection, safe_error_body


logger = logging.getLogger("circle_core_control_api")


def _ensure_enabled():
    if not getattr(settings, "PRODUCT_CONTROL_API_ENABLED", False):
        return HttpResponseNotFound()
    return None


def _safe_uuid(value, field):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise ControlAPIError("validation_failed", f"{field} must be a UUID.", status=400)


def _read_query(request, resource):
    paginated = {"tenants", "tenant_users", "tenant_activity"}
    allowed = {"cursor", "page_size"} if resource in paginated else set()
    if set(request.GET) - allowed:
        raise ControlAPIError("validation_failed", "Unsupported query parameter.", status=400)
    result = {}
    if "cursor" in request.GET:
        cursor = request.GET["cursor"]
        if not cursor or len(cursor) > 500:
            raise ControlAPIError("validation_failed", "cursor is invalid.", status=400)
        result["cursor"] = cursor
    if "page_size" in request.GET:
        try:
            page_size = int(request.GET["page_size"])
        except ValueError:
            raise ControlAPIError("validation_failed", "page_size must be an integer.", status=400)
        if page_size < 1 or page_size > 100:
            raise ControlAPIError("validation_failed", "page_size must be between 1 and 100.", status=400)
        result["page_size"] = page_size
    return result


def _error_response(error, *, request, action, principal=None, envelope=None, target_reference=""):
    audit = None
    if principal is not None or envelope:
        audit = record_rejection(
            action=action,
            principal=principal,
            envelope=envelope,
            error=error,
            request_digest=body_digest(request.body),
            target_reference=target_reference,
        )
    else:
        logger.warning("Product control request rejected: action=%s error_code=%s", action, error.error_code)
    correlation = (envelope or {}).get("correlation_id") or request.headers.get("X-Correlation-ID", "")
    return JsonResponse(
        safe_error_body(error, correlation_id=correlation, product_audit_id=getattr(audit, "id", "")),
        status=error.status,
    )


@csrf_exempt
@require_http_methods(["GET", "POST"])
def tenants_collection_endpoint(request):
    if request.method == "GET":
        return read_endpoint(request, resource="tenants")
    return write_endpoint(request, action="create_tenant")


@csrf_exempt
@require_http_methods(["GET"])
def read_endpoint(request, resource, tenant_id=None, operation_id=None, audit_id=None, job_id=None):
    disabled_response = _ensure_enabled()
    if disabled_response is not None:
        return disabled_response
    principal = None
    identifiers = {key: str(value) for key, value in {"tenant_id": tenant_id, "operation_id": operation_id, "audit_id": audit_id, "job_id": job_id}.items() if value is not None}
    action = f"read.{resource}"
    try:
        principal = authenticate_request(request)
        authorize(principal, action, tenant_id)
        query = _read_query(request, resource)
        if resource == "operation":
            operation_uuid = _safe_uuid(operation_id, "operation_id")
            record = IdempotencyRecord.objects.filter(caller_identity=principal.identity, operation_id=operation_uuid).first()
            if record is None:
                raise ControlAPIError("operation_not_found", "Operation was not found.", status=404)
            data = {
                "operation_id": str(record.operation_id), "correlation_id": str(record.correlation_id),
                "action": record.action, "state": record.state, "target_reference": record.target_reference,
                "result": record.response_body if record.state != "processing" else {},
            }
        elif resource == "audit":
            audit_uuid = _safe_uuid(audit_id, "audit_id")
            confirmed = ProductControlAuditEvent.objects.filter(
                pk=audit_uuid, caller_identity=principal.identity,
            ).first()
            if confirmed is None:
                raise ControlAPIError("audit_not_found", "Product audit evidence was not found.", status=404)
            data = {
                "audit_id": str(confirmed.id),
                "operation_id": str(confirmed.operation_id or ""),
                "correlation_id": str(confirmed.correlation_id or ""),
                "action": confirmed.action,
                "tenant": confirmed.target_reference,
                "target_reference": confirmed.target_reference,
                "administrator_reference": confirmed.requested_by,
                "administrator_role": confirmed.requester_role,
                "reason": confirmed.reason,
                "outcome": confirmed.outcome,
                "result": confirmed.outcome,
                "error_code": confirmed.error_code,
                "before_state": confirmed.before_state,
                "after_state": confirmed.after_state,
                "created_at": confirmed.created_at.isoformat(),
                "timestamp": confirmed.created_at.isoformat(),
            }
        elif resource == "health":
            data = load_backend().health({"principal": principal})
        elif resource == "capabilities":
            data = load_backend().capabilities({"principal": principal})
        else:
            data = load_backend().read(resource, identifiers, {"principal": principal, "query": query})
        if not isinstance(data, dict):
            raise ControlAPIError("invalid_product_response", "Product backend returned an invalid response.", status=500)
        correlation_id = request.headers.get("X-Correlation-ID", "")
        read_audit = ProductControlAuditEvent.objects.create(
            correlation_id=correlation_id or None, caller_identity=principal.identity,
            action=action, target_reference=str(tenant_id or job_id or operation_id or audit_id or "")[:200],
            request_digest=body_digest(request.body), outcome="completed",
            metadata={"read_only": True, "key_id": principal.key_id},
        )
        return JsonResponse({"schema_version": "1.0", "correlation_id": correlation_id, "product_audit_id": str(read_audit.id), "data": data})
    except ControlAPIError as error:
        return _error_response(error, request=request, action=action, principal=principal, target_reference=tenant_id or job_id or operation_id or audit_id or "")
    except Exception:
        error = ControlAPIError("internal_error", "The product could not complete the request.", status=500)
        return _error_response(error, request=request, action=action, principal=principal, target_reference=tenant_id or job_id or operation_id or audit_id or "")


@csrf_exempt
@require_http_methods(["POST"])
def write_endpoint(request, action, tenant_id=None, user_id=None, job_id=None):
    disabled_response = _ensure_enabled()
    if disabled_response is not None:
        return disabled_response
    principal = None
    envelope = None
    identifiers = {key: str(value) for key, value in {"tenant_id": tenant_id, "user_id": user_id, "job_id": job_id}.items() if value is not None}
    try:
        principal = authenticate_request(request)
        envelope = validate_write_envelope(request, action)
        scoped_tenant = tenant_id
        if user_id is not None:
            scoped_tenant = envelope["payload"].get("tenant_id")
            if not scoped_tenant:
                raise ControlAPIError("validation_failed", "User operations require payload.tenant_id.", status=400)
            identifiers["tenant_id"] = str(scoped_tenant)
        if job_id is not None and action == "retry_job":
            scoped_tenant = envelope["payload"].get("tenant_id")
            if not scoped_tenant:
                raise ControlAPIError("validation_failed", "Job retry requires payload.tenant_id.", status=400)
            identifiers["tenant_id"] = str(scoped_tenant)
        authorize(principal, f"write.{action}", scoped_tenant)
        response = execute_write(request, principal, action, identifiers, envelope)
        return JsonResponse(response.body, status=response.status)
    except ControlAPIError as error:
        return _error_response(
            error,
            request=request,
            action=action,
            principal=principal,
            envelope=envelope,
            target_reference=tenant_id or user_id or job_id or "",
        )
    except Exception:
        error = ControlAPIError("internal_error", "The product could not complete the request.", status=500)
        return _error_response(
            error,
            request=request,
            action=action,
            principal=principal,
            envelope=envelope,
            target_reference=tenant_id or user_id or job_id or "",
        )
