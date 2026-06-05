from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth.views import LoginView, LogoutView
from django.urls import include, path

from tenants import views as tenant_views

urlpatterns = [
    path("manifest.webmanifest", tenant_views.pwa_manifest, name="pwa_manifest"),
    path("service-worker.js", tenant_views.service_worker, name="service_worker"),
    path("admin/", admin.site.urls),
    path("login/", LoginView.as_view(template_name="login.html", redirect_authenticated_user=True), name="login"),
    path("logout/", LogoutView.as_view(next_page="login"), name="logout"),
    # PayFast — called by PayFast server (no login required) and by tenant users
    path("payfast/itn/", tenant_views.payfast_itn, name="payfast_itn"),
    path("payfast/initiate/", tenant_views.payfast_initiate, name="payfast_initiate"),
    path("subscription/payment/success/", tenant_views.payment_success, name="payment_success"),
    path("", include("core.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
