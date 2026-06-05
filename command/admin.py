from django.contrib import admin

from .models import CommandAuditLog, CommandUser


@admin.register(CommandUser)
class CommandUserAdmin(admin.ModelAdmin):
    list_display = ["username", "email", "is_active", "created_at", "last_login_at"]
    list_filter = ["is_active", "created_at"]
    search_fields = ["username", "email"]
    readonly_fields = ["created_at", "last_login_at"]


@admin.register(CommandAuditLog)
class CommandAuditLogAdmin(admin.ModelAdmin):
    list_display = ["created_at", "actor_username", "action", "schema_name", "object_repr", "ip_address"]
    list_filter = ["action", "schema_name", "created_at"]
    search_fields = ["actor_username", "schema_name", "object_repr", "reason"]
    readonly_fields = [
        "created_at",
        "actor",
        "actor_username",
        "action",
        "schema_name",
        "object_repr",
        "before",
        "after",
        "reason",
        "ip_address",
    ]
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
