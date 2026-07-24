import hashlib
import hmac
import ipaddress
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .errors import AuthenticationError, ControlAPIError, PermissionDeniedError
from .models import RequestNonce


@dataclass(frozen=True)
class ControlPrincipal:
    key_id: str
    identity: str
    permissions: frozenset
    tenant_allowlist: frozenset

    def permits(self, permission):
        return "*" in self.permissions or permission in self.permissions

    def permits_tenant(self, tenant_id):
        return not self.tenant_allowlist or str(tenant_id) in self.tenant_allowlist


def body_digest(body):
    return hashlib.sha256(body).hexdigest()


def canonical_request(method, path, timestamp, nonce, body):
    return "\n".join((method.upper(), path, timestamp, nonce, body_digest(body))).encode("utf-8")


def sign_request(secret, method, path, timestamp, nonce, body):
    return "sha256=" + hmac.new(
        secret.encode("utf-8"), canonical_request(method, path, timestamp, nonce, body), hashlib.sha256
    ).hexdigest()


def _client_ip(request):
    return (request.META.get("REMOTE_ADDR") or "").strip()


def _ip_allowed(client_ip, configured_ranges):
    if not configured_ranges:
        return True
    try:
        address = ipaddress.ip_address(client_ip)
        return any(address in ipaddress.ip_network(value, strict=False) for value in configured_ranges)
    except ValueError:
        return False


def authenticate_request(request):
    if getattr(settings, "PRODUCT_CONTROL_API_REQUIRE_HTTPS", True) and not request.is_secure():
        raise AuthenticationError("https_required", "HTTPS is required.")

    max_bytes = getattr(settings, "PRODUCT_CONTROL_API_MAX_REQUEST_BYTES", 131072)
    try:
        content_length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        raise ControlAPIError("invalid_request", "Content-Length is invalid.", status=400)
    if content_length > max_bytes:
        raise ControlAPIError("request_too_large", "The request body exceeds the allowed size.", status=413)
    if len(request.body) > max_bytes:
        raise ControlAPIError("request_too_large", "The request body exceeds the allowed size.", status=413)
    if request.method in {"POST", "PUT", "PATCH"} and request.content_type != "application/json":
        raise ControlAPIError("invalid_content_type", "Content-Type must be application/json.", status=415)

    key_id = request.headers.get("X-Control-Key-Id", "").strip()
    timestamp_text = request.headers.get("X-Control-Timestamp", "").strip()
    nonce = request.headers.get("X-Control-Nonce", "").strip()
    supplied_signature = request.headers.get("X-Control-Signature", "").strip().lower()
    if not key_id or not timestamp_text or not nonce or not supplied_signature:
        raise AuthenticationError()
    if len(key_id) > 100 or len(nonce) < 16 or len(nonce) > 128:
        raise AuthenticationError()

    key_config = getattr(settings, "PRODUCT_CONTROL_API_KEYS", {}).get(key_id)
    if not isinstance(key_config, dict) or not key_config.get("enabled", True):
        raise AuthenticationError()
    secret = key_config.get("secret", "")
    if not secret and key_config.get("secret_env"):
        secret = os.getenv(str(key_config["secret_env"]), "")
    identity = str(key_config.get("identity", "")).strip()
    if len(secret) < 32 or not identity:
        raise AuthenticationError()

    try:
        request_time = datetime.fromtimestamp(int(timestamp_text), tz=dt_timezone.utc)
    except (ValueError, OverflowError):
        raise AuthenticationError("invalid_timestamp", "Request timestamp is invalid.")
    now = timezone.now()
    skew = getattr(settings, "PRODUCT_CONTROL_API_TIMESTAMP_SKEW_SECONDS", 300)
    if abs((now - request_time).total_seconds()) > skew:
        raise AuthenticationError("expired_timestamp", "Request timestamp is outside the accepted window.")

    if not _ip_allowed(_client_ip(request), key_config.get("allowed_ips", [])):
        raise AuthenticationError("caller_ip_denied", "The caller network is not allowed.", status=403)

    expected = sign_request(secret, request.method, request.get_full_path(), timestamp_text, nonce, request.body)
    if not hmac.compare_digest(expected, supplied_signature):
        raise AuthenticationError("invalid_signature", "Request signature is invalid.")

    rate_limit = getattr(settings, "PRODUCT_CONTROL_API_RATE_LIMIT_PER_MINUTE", 120)
    if RequestNonce.objects.filter(caller_identity=identity, created_at__gte=now - timedelta(minutes=1)).count() >= rate_limit:
        raise ControlAPIError("rate_limit_exceeded", "Request rate limit exceeded.", status=429, retryable=True)

    try:
        with transaction.atomic():
            RequestNonce.objects.create(
                key_id=key_id,
                nonce=nonce,
                caller_identity=identity,
                request_digest=body_digest(request.body),
                request_timestamp=request_time,
                expires_at=now + timedelta(seconds=skew * 2),
            )
    except IntegrityError:
        raise AuthenticationError("replay_detected", "The request nonce has already been used.")

    return ControlPrincipal(
        key_id=key_id,
        identity=identity,
        permissions=frozenset(key_config.get("permissions", [])),
        tenant_allowlist=frozenset(str(value) for value in key_config.get("tenant_allowlist", [])),
    )


def authorize(principal, permission, tenant_id=None):
    if not principal.permits(permission):
        raise PermissionDeniedError()
    if tenant_id is not None and not principal.permits_tenant(tenant_id):
        raise PermissionDeniedError("The caller is not permitted to access this tenant.")
