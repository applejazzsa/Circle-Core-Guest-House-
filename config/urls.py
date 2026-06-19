from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth.views import (
    LogoutView,
    PasswordResetCompleteView,
    PasswordResetConfirmView,
    PasswordResetDoneView,
    PasswordResetView,
)
from django.urls import include, path

from tenants import views as tenant_views
from core.auth_views import DualLoginView

urlpatterns = [
    path("manifest.webmanifest", tenant_views.pwa_manifest, name="pwa_manifest"),
    path("service-worker.js", tenant_views.service_worker, name="service_worker"),
    path("healthz/", tenant_views.healthz, name="healthz"),
    path(settings.ADMIN_URL, admin.site.urls),
    path("login/", DualLoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(next_page="login"), name="logout"),
    path(
        "password-reset/",
        PasswordResetView.as_view(
            template_name="registration/password_reset_form.html",
            email_template_name="registration/password_reset_email.txt",
            subject_template_name="registration/password_reset_subject.txt",
            success_url="/password-reset/done/",
        ),
        name="password_reset",
    ),
    path(
        "password-reset/done/",
        PasswordResetDoneView.as_view(template_name="registration/password_reset_done.html"),
        name="password_reset_done",
    ),
    path(
        "password-reset/<uidb64>/<token>/",
        PasswordResetConfirmView.as_view(
            template_name="registration/password_reset_confirm.html",
            success_url="/password-reset/complete/",
        ),
        name="password_reset_confirm",
    ),
    path(
        "password-reset/complete/",
        PasswordResetCompleteView.as_view(template_name="registration/password_reset_complete.html"),
        name="password_reset_complete",
    ),
    # PayFast — called by PayFast server (no login required) and by tenant users
    path("payfast/itn/", tenant_views.payfast_itn, name="payfast_itn"),
    path("payfast/initiate/", tenant_views.payfast_initiate, name="payfast_initiate"),
    path("subscription/payment/success/", tenant_views.payment_success, name="payment_success"),
    # Command Center — SHARED_APPS model, @command_required protects every view
    path("command/", include("command.urls")),
    path("", include("core.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
