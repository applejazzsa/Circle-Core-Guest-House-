from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path

from core import views as core_views
from tenants import views as tenant_views

urlpatterns = [
    path('internal/control/v1/', include('circle_core_control_api.urls')),
    path('manifest.webmanifest', tenant_views.pwa_manifest, name='pwa_manifest'),
    path('service-worker.js', tenant_views.service_worker, name='service_worker'),
    path('healthz/', tenant_views.healthz, name='healthz'),
    path('login/', tenant_views.public_login, name='login'),
    path('register/', tenant_views.register, name='register'),
    path('request-demo/', tenant_views.request_demo, name='request_demo'),
    path('verify/<str:token>/', tenant_views.verify_email, name='verify_email'),
    path('privacy/', tenant_views.privacy_policy, name='privacy_policy'),
    path('terms/', tenant_views.terms, name='terms'),
    path('data-requests/', tenant_views.data_requests, name='data_requests'),
    path('command/', include('command.urls')),
    path('command-enter/', core_views.command_enter, name='command_enter'),
    path('', tenant_views.landing, name='landing'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
