from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="command_dashboard"),
    path("login/", views.command_login, name="command_login"),
    path("logout/", views.command_logout, name="command_logout"),
    path("tenants/", views.tenant_list, name="command_tenants"),
    path("tenants/<str:schema_name>/", views.tenant_detail, name="command_tenant_detail"),
    path("tenants/<str:schema_name>/extend-trial/", views.extend_trial, name="command_extend_trial"),
    path("tenants/<str:schema_name>/suspend/", views.suspend_tenant, name="command_suspend_tenant"),
    path("tenants/<str:schema_name>/activate/", views.activate_tenant, name="command_activate_tenant"),
    path("tenants/<str:schema_name>/record-payment/", views.record_payment, name="command_record_payment"),
    path("tenants/<str:schema_name>/impersonate/", views.impersonate_tenant, name="command_impersonate"),
    path("leads/", views.lead_list, name="command_leads"),
    path("leads/<int:pk>/status/", views.lead_update_status, name="command_lead_status"),
]
