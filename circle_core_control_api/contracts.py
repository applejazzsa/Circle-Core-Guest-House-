import json
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.utils import timezone

from .errors import ControlAPIError


WRITE_FIELDS = {
    "operation_id", "correlation_id", "idempotency_key", "requested_by", "requester_role",
    "reason", "requested_at", "expected_before_state", "payload",
}


def parse_json_object(request):
    try:
        value = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ControlAPIError("invalid_json", "The request body must be valid JSON.", status=400)
    if not isinstance(value, dict):
        raise ControlAPIError("invalid_request", "The request body must be a JSON object.", status=400)
    return value


def _uuid(value, field):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise ControlAPIError("validation_failed", f"{field} must be a UUID.", status=400)


def validate_write_envelope(request, action):
    data = parse_json_object(request)
    missing = sorted(WRITE_FIELDS - set(data))
    if missing:
        raise ControlAPIError("validation_failed", "Required request fields are missing.", status=400)
    unexpected = set(data) - WRITE_FIELDS - {"approval_reference"}
    if unexpected:
        raise ControlAPIError("validation_failed", "The request contains unsupported top-level fields.", status=400)

    operation_id = _uuid(data["operation_id"], "operation_id")
    correlation_id = _uuid(data["correlation_id"], "correlation_id")
    idempotency_key = _uuid(data["idempotency_key"], "idempotency_key")
    header_pairs = (
        ("X-Operation-ID", operation_id),
        ("X-Correlation-ID", correlation_id),
        ("Idempotency-Key", idempotency_key),
    )
    if any(request.headers.get(name, "") != str(expected) for name, expected in header_pairs):
        raise ControlAPIError("header_body_mismatch", "Operation headers must match the signed request body.", status=400)

    requested_by = str(data["requested_by"]).strip()
    requester_role = str(data["requester_role"]).strip()
    reason = str(data["reason"]).strip()
    approval_reference = str(data.get("approval_reference", "")).strip()
    if not requested_by or len(requested_by) > 200 or not requester_role or len(requester_role) > 100:
        raise ControlAPIError("validation_failed", "Requester identity and role are required.", status=400)
    if len(reason) < 10 or len(reason) > 1000:
        raise ControlAPIError("validation_failed", "Reason must contain between 10 and 1000 characters.", status=400)
    if not isinstance(data["payload"], dict):
        raise ControlAPIError("validation_failed", "payload must be an object.", status=400)
    if data["expected_before_state"] is not None and not isinstance(data["expected_before_state"], dict):
        raise ControlAPIError("validation_failed", "expected_before_state must be an object or null.", status=400)

    try:
        requested_at = datetime.fromisoformat(str(data["requested_at"]).replace("Z", "+00:00"))
        if requested_at.tzinfo is None:
            raise ValueError
    except (ValueError, TypeError):
        raise ControlAPIError("validation_failed", "requested_at must be an RFC 3339 timestamp.", status=400)
    skew = abs((timezone.now() - requested_at).total_seconds())
    if skew > 600:
        raise ControlAPIError("expired_request", "requested_at is outside the accepted window.", status=400)

    normalized = {
        **data,
        "operation_id": operation_id,
        "correlation_id": correlation_id,
        "idempotency_key": idempotency_key,
        "requested_by": requested_by,
        "requester_role": requester_role,
        "reason": reason,
        "approval_reference": approval_reference,
    }
    validate_action_payload(action, normalized["payload"])
    return normalized


def validate_action_payload(action, payload):
    required_by_action = {
        "extend_trial": {"trial_days"}, "change_plan": {"plan_code", "effective_at", "proration_policy"},
        "convert_trial_to_paid": {"plan_code", "price", "billing_cycle", "start_date", "payment_state", "next_billing_date"},
        "manual_payment": {"amount", "currency", "payment_date", "payment_method_category", "internal_reference"},
        "apply_grace_period": {"grace_days"}, "cancel_subscription": {"effective_at"},
        "invite_admin": {"user_reference"},
        "invite_user": {"email", "full_name", "role"}, "change_user_role": {"role"},
    }
    if action in required_by_action:
        missing = {field for field in required_by_action[action] if payload.get(field) in (None, "")}
        if missing:
            raise ControlAPIError("validation_failed", "Required operation fields are missing.", status=400)
        if action in {"extend_trial", "apply_grace_period"}:
            field, maximum = ("trial_days", 90) if action == "extend_trial" else ("grace_days", 30)
            if isinstance(payload[field], bool) or not isinstance(payload[field], int) or not 1 <= payload[field] <= maximum:
                raise ControlAPIError("validation_failed", f"{field} is outside the supported range.", status=422)
        if action == "manual_payment":
            if len(str(payload["currency"])) != 3 or not str(payload["currency"]).isalpha():
                raise ControlAPIError("validation_failed", "currency must be a three-letter code.", status=422)
            if not isinstance(payload.get("evidence_metadata", {}), dict):
                raise ControlAPIError("validation_failed", "evidence_metadata must be an object.", status=400)
        if action == "convert_trial_to_paid":
            if payload["billing_cycle"] not in {"monthly", "annual"}:
                raise ControlAPIError("validation_failed", "billing_cycle is not supported.", status=422)
            if payload["payment_state"] not in {"unpaid", "pending_verification", "paid"}:
                raise ControlAPIError("validation_failed", "payment_state is not supported.", status=422)
        return
    if action != "create_tenant":
        return
    required = {
        "legal_or_trading_name", "tenant_display_name", "product", "plan", "billing_cycle",
        "trial_start", "trial_expiry", "primary_administrator", "timezone", "currency", "country",
        "primary_location", "initial_feature_flags", "external_smart_control_tenant_reference",
    }
    if required - set(payload):
        raise ControlAPIError("validation_failed", "Tenant provisioning fields are missing.", status=400)
    text_fields = (
        "legal_or_trading_name", "tenant_display_name", "product", "plan", "billing_cycle",
        "timezone", "currency", "country", "external_smart_control_tenant_reference",
    )
    if any(not isinstance(payload.get(field), str) or not payload[field].strip() for field in text_fields):
        raise ControlAPIError("validation_failed", "Tenant provisioning text fields must be non-empty strings.", status=400)
    if not isinstance(payload["primary_administrator"], dict):
        raise ControlAPIError("validation_failed", "primary_administrator must be an object.", status=400)
    administrator = payload["primary_administrator"]
    if not str(administrator.get("name", "")).strip() or not str(administrator.get("email", "")).strip():
        raise ControlAPIError("validation_failed", "Primary administrator name and email are required.", status=400)
    try:
        validate_email(administrator["email"])
    except DjangoValidationError:
        raise ControlAPIError("validation_failed", "Primary administrator email is invalid.", status=400)
    if not isinstance(payload["primary_location"], dict) or not isinstance(payload["initial_feature_flags"], dict):
        raise ControlAPIError("validation_failed", "Primary location and initial feature flags must be objects.", status=400)
    if any(not isinstance(value, bool) for value in payload["initial_feature_flags"].values()):
        raise ControlAPIError("validation_failed", "Initial feature flag values must be booleans.", status=400)
    if payload["billing_cycle"] not in {"monthly", "annual", "once_off"}:
        raise ControlAPIError("validation_failed", "billing_cycle is not supported by the common contract.", status=400)
    if len(payload["currency"]) != 3 or not payload["currency"].isupper():
        raise ControlAPIError("validation_failed", "currency must be a three-letter uppercase code.", status=400)
    if len(payload["country"]) != 2 or not payload["country"].isupper():
        raise ControlAPIError("validation_failed", "country must be a two-letter uppercase code.", status=400)
    try:
        ZoneInfo(payload["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        raise ControlAPIError("validation_failed", "timezone must be a valid IANA timezone.", status=400)
    trial_dates = []
    for field in ("trial_start", "trial_expiry"):
        value = payload[field]
        if value is None:
            trial_dates.append(None)
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError
            trial_dates.append(parsed)
        except (AttributeError, TypeError, ValueError):
            raise ControlAPIError("validation_failed", f"{field} must be an RFC 3339 date-time or null.", status=400)
    if (trial_dates[0] is None) != (trial_dates[1] is None):
        raise ControlAPIError("validation_failed", "Trial start and expiry must both be supplied or both be null.", status=400)
    if trial_dates[0] is not None and trial_dates[1] <= trial_dates[0]:
        raise ControlAPIError("validation_failed", "Trial expiry must be after trial start.", status=400)
