from django.contrib import admin
from django_tenants.admin import TenantAdminMixin

from .models import Domain, GuestHouseTenant


@admin.register(GuestHouseTenant)
class GuestHouseTenantAdmin(TenantAdminMixin, admin.ModelAdmin):
    list_display = ('name', 'schema_name', 'owner_name', 'owner_email', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'owner_email', 'schema_name')
    readonly_fields = ('schema_name', 'created_at')


@admin.register(Domain)
class DomainAdmin(admin.ModelAdmin):
    list_display = ('domain', 'tenant', 'is_primary')
    list_filter = ('is_primary',)
