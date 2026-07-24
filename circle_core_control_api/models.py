import uuid

from django.db import models


class RequestNonce(models.Model):
    key_id = models.CharField(max_length=100)
    nonce = models.CharField(max_length=128)
    caller_identity = models.CharField(max_length=160)
    request_digest = models.CharField(max_length=64)
    request_timestamp = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["key_id", "nonce"], name="control_api_unique_key_nonce")]
        indexes = [models.Index(fields=["caller_identity", "created_at"], name="control_api_rate_idx")]


class IdempotencyRecord(models.Model):
    STATES = [(value, value.replace("_", " ").title()) for value in ("processing", "completed", "failed")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    caller_identity = models.CharField(max_length=160)
    idempotency_key = models.UUIDField()
    operation_id = models.UUIDField(db_index=True)
    correlation_id = models.UUIDField(db_index=True)
    action = models.CharField(max_length=100)
    target_reference = models.CharField(max_length=200, blank=True)
    request_digest = models.CharField(max_length=64)
    state = models.CharField(max_length=20, choices=STATES, default="processing", db_index=True)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["caller_identity", "idempotency_key"], name="control_api_unique_caller_idempotency"),
            models.UniqueConstraint(fields=["caller_identity", "operation_id"], name="control_api_unique_caller_operation"),
        ]


class ProductControlAuditEvent(models.Model):
    OUTCOMES = [(value, value.title()) for value in ("accepted", "completed", "rejected", "failed", "duplicate")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    operation_id = models.UUIDField(null=True, blank=True, db_index=True)
    correlation_id = models.UUIDField(null=True, blank=True, db_index=True)
    caller_identity = models.CharField(max_length=160, blank=True)
    requested_by = models.CharField(max_length=200, blank=True)
    requester_role = models.CharField(max_length=100, blank=True)
    action = models.CharField(max_length=100, db_index=True)
    target_reference = models.CharField(max_length=200, blank=True, db_index=True)
    request_digest = models.CharField(max_length=64, blank=True)
    outcome = models.CharField(max_length=20, choices=OUTCOMES)
    error_code = models.CharField(max_length=80, blank=True)
    before_state = models.JSONField(default=dict, blank=True)
    after_state = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if self.pk and ProductControlAuditEvent.objects.filter(pk=self.pk).exists():
            raise RuntimeError("Product control audit events are immutable")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("Product control audit events cannot be deleted")
