from django.urls import path

from .views import read_endpoint, tenants_collection_endpoint, write_endpoint


app_name = "product_control_api"

urlpatterns = [
    path("health", read_endpoint, {"resource": "health"}, name="health"),
    path("capabilities", read_endpoint, {"resource": "capabilities"}, name="capabilities"),
    path("plans", read_endpoint, {"resource": "plans"}, name="plans"),
    path("tenants", tenants_collection_endpoint, name="tenants"),
    path("tenants/<str:tenant_id>", read_endpoint, {"resource": "tenant"}, name="tenant"),
    path("tenants/<str:tenant_id>/users", read_endpoint, {"resource": "tenant_users"}, name="tenant_users"),
    path("tenants/<str:tenant_id>/subscription", read_endpoint, {"resource": "tenant_subscription"}, name="tenant_subscription"),
    path("tenants/<str:tenant_id>/activity", read_endpoint, {"resource": "tenant_activity"}, name="tenant_activity"),
    path("operations/<str:operation_id>", read_endpoint, {"resource": "operation"}, name="operation"),
    path("audits/<str:audit_id>", read_endpoint, {"resource": "audit"}, name="audit"),
    path("jobs/<str:job_id>", read_endpoint, {"resource": "job"}, name="job"),
    path("tenants/<str:tenant_id>/activate", write_endpoint, {"action": "activate_tenant"}, name="activate_tenant"),
    path("tenants/<str:tenant_id>/suspend", write_endpoint, {"action": "suspend_tenant"}, name="suspend_tenant"),
    path("tenants/<str:tenant_id>/reactivate", write_endpoint, {"action": "reactivate_tenant"}, name="reactivate_tenant"),
    path("tenants/<str:tenant_id>/archive", write_endpoint, {"action": "archive_tenant"}, name="archive_tenant"),
    path("tenants/<str:tenant_id>/restore", write_endpoint, {"action": "restore_tenant"}, name="restore_tenant"),
    path("tenants/<str:tenant_id>/trial/extend", write_endpoint, {"action": "extend_trial"}, name="extend_trial"),
    path("tenants/<str:tenant_id>/subscription/change-plan", write_endpoint, {"action": "change_plan"}, name="change_plan"),
    path("tenants/<str:tenant_id>/subscription/convert-to-paid", write_endpoint, {"action": "convert_trial_to_paid"}, name="convert_trial_to_paid"),
    path("tenants/<str:tenant_id>/subscription/manual-payment", write_endpoint, {"action": "manual_payment"}, name="manual_payment"),
    path("tenants/<str:tenant_id>/subscription/grace-period", write_endpoint, {"action": "apply_grace_period"}, name="apply_grace_period"),
    path("tenants/<str:tenant_id>/subscription/cancel", write_endpoint, {"action": "cancel_subscription"}, name="cancel_subscription"),
    path("tenants/<str:tenant_id>/products/enable", write_endpoint, {"action": "enable_product"}, name="enable_product"),
    path("tenants/<str:tenant_id>/products/disable", write_endpoint, {"action": "disable_product"}, name="disable_product"),
    path("tenants/<str:tenant_id>/users/invite-admin", write_endpoint, {"action": "invite_admin"}, name="invite_admin"),
    path("tenants/<str:tenant_id>/users/invite", write_endpoint, {"action": "invite_user"}, name="invite_user"),
    path("users/<str:user_id>/send-password-reset", write_endpoint, {"action": "send_password_reset"}, name="send_password_reset"),
    path("users/<str:user_id>/force-password-reset", write_endpoint, {"action": "force_password_reset"}, name="force_password_reset"),
    path("users/<str:user_id>/unlock", write_endpoint, {"action": "unlock_user"}, name="unlock_user"),
    path("users/<str:user_id>/revoke-sessions", write_endpoint, {"action": "revoke_sessions"}, name="revoke_sessions"),
    path("users/<str:user_id>/disable", write_endpoint, {"action": "disable_user"}, name="disable_user"),
    path("users/<str:user_id>/reactivate", write_endpoint, {"action": "reactivate_user"}, name="reactivate_user"),
    path("users/<str:user_id>/change-role", write_endpoint, {"action": "change_user_role"}, name="change_user_role"),
    path("jobs/<str:job_id>/retry", write_endpoint, {"action": "retry_job"}, name="retry_job"),
]
