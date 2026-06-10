from django.contrib.auth.hashers import check_password as _check_password, make_password
from django.db import models


class CommandUser(models.Model):
    """
    Admin users for the Circle Core Command Center.
    Lives in the public schema — independent from per-tenant auth.
    """
    username = models.CharField(max_length=150, unique=True)
    email = models.EmailField(unique=True)
    password = models.CharField(max_length=128)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_login_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Command User"
        ordering = ["username"]

    def __str__(self):
        return self.username

    def set_password(self, raw_password):
        self.password = make_password(raw_password)

    def check_password(self, raw_password):
        return _check_password(raw_password, self.password)


class TenantPaymentRecord(models.Model):
    """
    Full payment history per tenant — lives in the public schema so it
    survives across Command Center sessions and is never lost on re-seeding.
    """
    BILLING_CHOICES = [
        ("monthly", "Monthly"),
        ("annual", "Annual"),
    ]

    schema_name = models.CharField(max_length=63, db_index=True)
    tenant_name = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    reference = models.CharField(max_length=100, blank=True)
    billing_cycle = models.CharField(max_length=10, choices=BILLING_CHOICES, default="monthly")
    plan_name = models.CharField(max_length=100, blank=True)
    recorded_by = models.CharField(max_length=150, blank=True)
    notes = models.TextField(blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-recorded_at"]

    def __str__(self):
        return f"{self.tenant_name} R{self.amount} {self.recorded_at:%Y-%m-%d}"


class CronHealth(models.Model):
    """Single-row table updated by the cron container on every job run."""
    job_name = models.CharField(max_length=80, primary_key=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_status = models.CharField(max_length=20, default="unknown")
    last_message = models.TextField(blank=True)

    class Meta:
        verbose_name = "Cron Health"

    def __str__(self):
        return f"{self.job_name} — {self.last_status} @ {self.last_run_at}"


class CommandAuditLog(models.Model):
    actor = models.ForeignKey(CommandUser, on_delete=models.SET_NULL, null=True, blank=True)
    actor_username = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=80)
    schema_name = models.CharField(max_length=63, blank=True)
    object_repr = models.CharField(max_length=255, blank=True)
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    reason = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.actor_username} {self.action} {self.schema_name}"
