from django.contrib import admin
from django.urls import path,include
from django.conf import settings
from django.conf.urls.static import static

from .health import liveness_view, readiness_view

urlpatterns = [
    # Operational health — public, cheap, no secrets (see server/health.py).
    path('health/', liveness_view, name='health_liveness'),
    path('health/ready/', readiness_view, name='health_readiness'),

    path('admin/', admin.site.urls),
    path('',include('accounts.urls')),
    path('myapp/',include('myapp.urls')),
]

if settings.DEBUG:
    urlpatterns+=static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)