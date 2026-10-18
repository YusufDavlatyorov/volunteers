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


# Background / high-frequency POSTs that are not a person's action: the live
# location ping, "mark as read", the profile's own location refresh.
ACTIVITY_IGNORED_URL_NAMES = frozenset({
    "task_location", "update_location", "task_chat_read", "notification_read", "notifications_read_all",
    "profile_language",
})


class DailyActivityMiddleware:
    """Per-day activity for the profile heatmap (``UserDailyActivity``,
    services/activity.py::record_request): the first page of the day counts
    one action (afterwards a cache flag makes page views free), each
    successful state-changing request (POST/PUT/PATCH/DELETE answered < 400)
    one more — a single ``F()`` UPDATE. Off with ``USER_ACTIVITY_TRACKING``
    (and under ``manage.py test``). Must sit after AuthenticationMiddleware.
    Never raises into the request."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.skip_prefixes = _skip_prefixes()

    def __call__(self, request):
        response = self.get_response(request)
        if not getattr(settings, "USER_ACTIVITY_TRACKING", False):
            return response
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated or request.path.startswith(self.skip_prefixes):
            return response
        match = getattr(request, "resolver_match", None)
        if match is not None and match.url_name in ACTIVITY_IGNORED_URL_NAMES:
            return response
        action = request.method not in ("GET", "HEAD", "OPTIONS", "TRACE") and response.status_code < 400
        try:
            from myapp.services import activity

            activity.record_request(user, first_of_day_only=not action)
        except Exception:  # noqa: BLE001 — activity must never break a page
            logger.warning("daily activity failed user=#%s", getattr(user, "pk", None))
        return response
