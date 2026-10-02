"""Request middleware for myapp."""

import logging

from django.conf import settings

from myapp.services import presence

logger = logging.getLogger(__name__)


def _skip_prefixes():
    prefixes = ["/health/", "/ws/", "/favicon.ico"]
    for url in (getattr(settings, "STATIC_URL", None), getattr(settings, "MEDIA_URL", None)):
        if url and url.startswith("/"):
            prefixes.append(url)
    return tuple(prefixes)


class PresenceMiddleware:
    """An authenticated page view is a sign of life for availability
    (Feature Pack v2, feature 3). Throttled inside `presence.mark_seen` to one
    DB write per user per minute, so a normal request costs one cache op.
    Must sit after AuthenticationMiddleware. Never raises into the request."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.skip_prefixes = _skip_prefixes()

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and not request.path.startswith(self.skip_prefixes)
        ):
            try:
                presence.mark_seen(user, source="web")
            except Exception:  # noqa: BLE001 — presence must never break a page
                logger.warning("presence mark_seen failed user=#%s", getattr(user, "pk", None))
        return self.get_response(request)
