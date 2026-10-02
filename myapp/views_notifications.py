"""Notification + realtime HTTP endpoints (Feature Pack v2, features 11/12).

Every view is ``@login_required`` and only ever touches the requesting user's
own notifications (another user's id → 404). Mutations are ``@require_POST``.
"""

import json
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from .models import Notification
from .realtime import events
from .services import notify as notify_service
from .services import notify_texts

logger = logging.getLogger(__name__)

BELL_LIMIT = 10
NOTIFICATIONS_PAGE_SIZE = 25
POLL_LIMIT = 50


def _wants_json(request):
    return (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("accept", "")
    )


def _safe_link(request, link):
    if link and url_has_allowed_host_and_scheme(link, allowed_hosts={request.get_host()}) and link.startswith("/"):
        return link
    return None


def _user_lang(user):
    profile = getattr(user, "profile", None)
    return getattr(profile, "language", None) or notify_texts.DEFAULT_LANG


@login_required
@require_GET
def notifications_bell_view(request):
    """Navbar bell: unread count + latest 10 (two queries, any data volume)."""
    user = request.user
    latest = list(Notification.objects.filter(recipient=user).order_by("-created_at", "-id")[:BELL_LIMIT])
    return JsonResponse({
        "unread_count": notify_service.unread_count(user),
        "items": [notify_service.serialize(n) for n in latest],
    })


@login_required
@require_GET
def notifications_list_view(request):
    user = request.user
    qs = Notification.objects.filter(recipient=user).order_by("-created_at", "-id")
    type_filter = request.GET.get("type", "")
    if type_filter in notify_service.NOTIFICATION_TYPES:
        qs = qs.filter(type=type_filter)
    else:
        type_filter = ""
    unread_only = request.GET.get("unread") == "1"
    if unread_only:
        qs = qs.filter(read_at__isnull=True)
    page_obj = Paginator(qs, NOTIFICATIONS_PAGE_SIZE).get_page(request.GET.get("page"))
    lang = _user_lang(user)
    rows = []
    for n in page_obj:
        title, body = notify_texts.render_inapp(n.title_key, n.body_key, n.params, lang)
        rows.append({"n": n, "title": title, "body": body, "args": json.dumps(n.params or {}, ensure_ascii=False)})
    querystring = request.GET.copy()
    querystring.pop("page", None)
    type_choices = [
        (t.code, notify_texts.TYPE_LABELS.get(t.code, {}).get("en", t.code))
        for t in notify_service.types_for_role(user.role)
    ]
    return render(request, "myapp/notifications/list.html", {
        "rows": rows,
        "page_obj": page_obj,
        "querystring": querystring.urlencode(),
        "type_filter": type_filter,
        "unread_only": unread_only,
        "type_choices": type_choices,
        "unread_count": notify_service.unread_count(user),
    })


@login_required
@require_POST
def notification_read_view(request, pk):
    """Mark one of MY notifications read. ``open=1`` then follows its link."""
    n = Notification.objects.filter(pk=pk, recipient=request.user).first()
    if n is None:
        raise Http404
    payload = notify_service.mark_read(request.user, ids=[n.pk])
    if request.POST.get("open") == "1":
        return redirect(_safe_link(request, n.link) or "notifications")
    if _wants_json(request):
        return JsonResponse({"success": True, **payload})
    return redirect("notifications")


@login_required
@require_POST
def notifications_read_all_view(request):
    payload = notify_service.mark_read(request.user, all=True)
    if _wants_json(request):
        return JsonResponse({"success": True, **payload})
    return redirect("notifications")


@login_required
def notification_preferences_view(request):
    user = request.user
    if request.method == "POST":
        enabled = set()
        for raw in request.POST.getlist("pref"):
            type_code, _, channel = raw.partition(":")
            if type_code in notify_service.NOTIFICATION_TYPES and channel in notify_service.CHANNELS:
                enabled.add((type_code, channel))
        notify_service.save_preferences(user, enabled)
        logger.info("notification preferences updated by user #%s", user.pk)
        messages.success(request, "Настройки уведомлений сохранены.")
        return redirect("notification_preferences")
    return render(request, "myapp/notifications/preferences.html", {
        "matrix": [
            {**row, "label": notify_texts.TYPE_LABELS.get(row["type"], {}).get("en", row["type"])}
            for row in notify_service.preference_matrix(user)
        ],
        "channels": notify_service.ALL_CHANNELS,
    })


@login_required
@require_GET
def realtime_poll_view(request):
    """HTTP fallback for when the WebSocket is blocked: the same
    ``notification.new`` envelopes the socket would have pushed since the
    client's cursor (a notification id), oldest first, plus the unread count."""
    try:
        since = max(int(request.GET.get("since", "0")), 0)
    except ValueError:
        since = 0
    user = request.user
    fresh = list(
        Notification.objects.filter(recipient=user, read_at__isnull=True, pk__gt=since).order_by("id")[:POLL_LIMIT]
    )
    cursor = fresh[-1].pk if fresh else since
    if not fresh and since == 0:
        last = Notification.objects.filter(recipient=user).order_by("-id").values_list("id", flat=True).first()
        cursor = last or 0
    return JsonResponse({
        "events": [events.envelope(events.NOTIFICATION_NEW, notify_service.serialize(n)) for n in fresh],
        "cursor": cursor,
        "unread_count": notify_service.unread_count(user),
    })
