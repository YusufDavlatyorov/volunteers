"""Template helpers for the Feature Pack v2 live UI (navbar bell, do-not-disturb).

* ``{% unread_notifications_count as n %}`` — the signed-in user's unread
  in-app notifications (one indexed COUNT; 0 for anonymous users), so the bell
  badge is right on first paint without an extra request.
* ``profile.away_until|away_last_day`` — ``Profile.away_until`` is stored as
  the START of the day after the inclusive last day; this returns that last
  day (a ``date``) for display ("Не беспокоить до 12.10.2026"), or ``None``.
"""

import datetime

from django import template
from django.utils import timezone

register = template.Library()


@register.simple_tag(takes_context=True)
def unread_notifications_count(context):
    request = context.get("request")
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return 0
    from myapp.services import notify

    try:
        return notify.unread_count(user)
    except Exception:  # noqa: BLE001 — a badge must never break a page
        return 0


@register.filter
def away_last_day(value):
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return (value - datetime.timedelta(microseconds=1)).date()
    return value - datetime.timedelta(days=1)
