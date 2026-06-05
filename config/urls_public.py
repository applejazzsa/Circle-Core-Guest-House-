from django.conf import settings
from django.conf.urls.static import static
from django.urls import path

from tenants import views as tenant_views

urlpatterns = [
    path('manifest.webmanifest', tenant_views.pwa_manifest, name='pwa_manifest'),
    path('service-worker.js', tenant_views.service_worker, name='service_worker'),
    path('login/', tenant_views.public_login, name='login'),
    path('register/', tenant_views.register, name='register'),
    path('request-demo/', tenant_views.request_demo, name='request_demo'),
    path('verify/<str:token>/', tenant_views.verify_email, name='verify_email'),
    path('', tenant_views.landing, name='landing'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
