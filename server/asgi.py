"""
ASGI entry point — HTTP (Django) + WebSockets (Channels).

Production serves everything from this callable: gunicorn with uvicorn workers
(deploy/systemd/khayrkhoh-web.service, server/asgi_worker.py) on
127.0.0.1:8000; nginx proxies pages and /ws/ there and serves static/media
itself.

WebSockets: session auth (AuthMiddlewareStack) behind an Origin check against
ALLOWED_HOSTS (AllowedHostsOriginValidator). Protocol: docs/WS_PROTOCOL.md.
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'server.settings')

# Initialise Django (apps registry) before importing anything that touches models.
django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402

from django.conf import settings  # noqa: E402

from myapp.realtime.routing import websocket_urlpatterns  # noqa: E402

# Local dev only: `uvicorn server.asgi:application` then serves /static/ too, so
# the whole site (pages + /ws/) runs from one process. Production (DEBUG=False)
# is unchanged — nginx serves static files.
if settings.DEBUG:
    from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler  # noqa: E402

    django_asgi_app = ASGIStaticFilesHandler(django_asgi_app)

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AllowedHostsOriginValidator(
        AuthMiddlewareStack(URLRouter(websocket_urlpatterns))
    ),
})
