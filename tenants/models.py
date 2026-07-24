import uuid

from django.db import models
from django_tenants.models import DomainMixin, TenantMixin


class GuestHouseTenant(TenantMixin):
    smart_control_reference = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    name = models.CharField(max_length=200)
    owner_name = models.CharField(max_length=200)
    owner_email = models.EmailField(unique=True)
    owner_phone = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)
    is_verified = models.BooleanField(default=False)
    control_previous_subscription_status = models.CharField(max_length=20, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    product_access_enabled = models.BooleanField(default=True)
    notes_internal = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    auto_create_schema = True

    class Meta:
        verbose_name = 'Guest House'
        verbose_name_plural = 'Guest Houses'

    def __str__(self):
        return self.name

    def delete(self, *args, allow_hard_delete=False, **kwargs):
        if kwargs.get("force_drop"):
            allow_hard_delete = True
        if not allow_hard_delete:
            raise RuntimeError(
                "Tenant hard delete is disabled. Suspend/deactivate tenants instead."
            )
        return super().delete(*args, **kwargs)


class Domain(DomainMixin):
    pass


class ControlActivationOutbox(models.Model):
    STATES = [('pending', 'Pending'), ('sent', 'Sent'), ('failed', 'Failed')]
    KINDS = [('activation', 'Activation'), ('administrator_invitation', 'Administrator invitation'), ('password_reset', 'Password reset')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(GuestHouseTenant, on_delete=models.PROTECT, related_name='control_activation_messages')
    user_id = models.PositiveBigIntegerField()
    recipient = models.EmailField()
    kind = models.CharField(max_length=40, choices=KINDS, default='activation')
    state = models.CharField(max_length=20, choices=STATES, default='pending', db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error_code = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    requested_at = models.DateTimeField(auto_now=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['created_at']
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'user_id', 'kind'], name='unique_guest_control_activation_kind'),
        ]


class ControlOperationNotification(models.Model):
    STATES = [('queued', 'Queued'), ('suppressed', 'Suppressed'), ('sent', 'Sent'), ('failed', 'Failed')]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(GuestHouseTenant, on_delete=models.PROTECT, related_name='control_operation_notifications')
    operation_id = models.UUIDField(unique=True)
    action = models.CharField(max_length=80)
    recipient = models.EmailField(blank=True)
    behavior = models.CharField(max_length=30)
    state = models.CharField(max_length=20, choices=STATES)
    safe_payload = models.JSONField(default=dict, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error_code = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)


class Lead(models.Model):
    STATUS_CHOICES = [
        ('new', 'New Lead'),
        ('contacted', 'Contacted'),
        ('demo_scheduled', 'Demo Scheduled'),
        ('demo_completed', 'Demo Completed'),
        ('trial_started', 'Trial Started'),
        ('converted', 'Converted'),
        ('lost', 'Lost'),
    ]

    full_name = models.CharField(max_length=200)
    business_name = models.CharField(max_length=200)
    email = models.EmailField()
    phone = models.CharField(max_length=50)
    num_rooms = models.PositiveIntegerField(null=True, blank=True)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='new')
    source = models.CharField(max_length=50, default='website', blank=True)
    tenant = models.ForeignKey(GuestHouseTenant, null=True, blank=True,
                               on_delete=models.SET_NULL, related_name='leads')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    contacted_at = models.DateTimeField(null=True, blank=True)
    notes_internal = models.TextField(blank=True, help_text='Internal Circle Core notes — not visible to customer')

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Lead'
        verbose_name_plural = 'Leads'

    def __str__(self):
        return f'{self.business_name} — {self.full_name} ({self.get_status_display()})'
