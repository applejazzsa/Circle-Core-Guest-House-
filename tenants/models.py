from django.db import models
from django_tenants.models import DomainMixin, TenantMixin


class GuestHouseTenant(TenantMixin):
    name = models.CharField(max_length=200)
    owner_name = models.CharField(max_length=200)
    owner_email = models.EmailField(unique=True)
    owner_phone = models.CharField(max_length=20, blank=True)
    is_active = models.BooleanField(default=True)
    is_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    auto_create_schema = True

    class Meta:
        verbose_name = 'Guest House'
        verbose_name_plural = 'Guest Houses'

    def __str__(self):
        return self.name


class Domain(DomainMixin):
    pass


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
