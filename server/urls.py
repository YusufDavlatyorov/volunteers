from django.contrib import admin
from django.urls import path,include
from django.conf import settings
from django.conf.urls.static import static
from django.contrib.sitemaps.views import sitemap
from django.templatetags.static import static as static_url
from django.views.generic import RedirectView

from myapp.views import home_view

from .health import liveness_view, readiness_view
from .seo import SITEMAPS, robots_txt

urlpatterns = [
    # Operational health — public, cheap, no secrets (see server/health.py).
    path('health/', liveness_view, name='health_liveness'),
    path('health/ready/', readiness_view, name='health_readiness'),

    # SEO — see server/seo.py (public pages, robots rules, sitemap).
    path('robots.txt', robots_txt, name='robots_txt'),
    path('sitemap.xml', sitemap, {'sitemaps': SITEMAPS}, name='sitemap'),
    path('favicon.ico', RedirectView.as_view(url=static_url('images/brand/favicon-48.png'), permanent=True)),

    path('admin/', admin.site.urls),
    path('', home_view, name='home'),
    path('',include('accounts.urls')),
    path('myapp/',include('myapp.urls')),
]

if settings.DEBUG:
    urlpatterns+=static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)