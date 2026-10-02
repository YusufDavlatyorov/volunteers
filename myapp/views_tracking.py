"""Live-tracking HTTP endpoints (Feature Pack v2, feature 7).

The WebSocket (``location.update`` / ``tracking.stop``) is the primary path;
these are the fallbacks for browsers/networks where WS is blocked, plus the
consent switch and a "latest position" read. All logic is in
``myapp/services/tracking.py``; permission for reads is
``access.can_see_location`` (staff, the task's client, the assigned volunteer —
active task only). Responses carry ids, coordinates and timestamps only.

    GET  /myapp/tasks/<pk>/location/         latest (+ ?trail=1 short trail)
    POST /myapp/tasks/<pk>/location/         one point (JSON or form body)
    POST /myapp/tasks/<pk>/tracking/stop/    the volunteer stops sharing
    POST /myapp/tracking/consent/            action=give|revoke
"""

import json
import logging

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .models import HelpRequest
from .services import access, tracking

logger = logging.getLogger(__name__)

HTTP_TRAIL_LIMIT = 200

# Soft drops (inaccurate / jump / too_soon) answer 200 so the sender just
# carries on with its next fix; only the hard failures get an error status.
_STATUS = {tracking.OK: 200, tracking.FORBIDDEN: 403, tracking.INVALID: 400}


def _body(request):
    """JSON object body, or the form fields; {} on garbage."""
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            data = json.loads(request.body or b"{}")
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None
    return request.POST.dict()


def _wants_json(request):
    return (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("accept", "")
        or "application/json" in request.headers.get("content-type", "")
    )


@login_required
def task_location_view(request, pk):
    if request.method == "GET":
        return _location_get(request, pk)
    if request.method == "POST":
        return _location_post(request, pk)
    response = JsonResponse({"ok": False, "code": "method_not_allowed"}, status=405)
    response["Allow"] = "GET, POST"
    return response


def _location_get(request, pk):
    task = get_object_or_404(HelpRequest, pk=pk)
    if not access.can_see_location(request.user, task):
        return JsonResponse({"ok": False, "code": tracking.FORBIDDEN}, status=403)
    data = {
        "ok": True,
        "task_id": task.pk,
        "latest": tracking.latest(task),
        "stopped": task.tracking_stopped_at is not None,
    }
    if request.GET.get("trail") in ("1", "true", "yes"):
        data["trail"] = tracking.trail(task, limit=HTTP_TRAIL_LIMIT)
    return JsonResponse(data)


def _location_post(request, pk):
    # CSRF applies (no exemption): the page's fetch sends X-CSRFToken.
    data = _body(request)
    if data is None:
        return JsonResponse({"ok": False, "code": tracking.INVALID}, status=400)
    outcome = tracking.accept_point(
        request.user, pk, data.get("lat"), data.get("lng"),
        accuracy=data.get("accuracy"), speed=data.get("speed"), heading=data.get("heading"),
        source="http",
    )
    body = {"ok": outcome.ok, "code": outcome.code}
    if outcome.ok:
        body["point"] = outcome.payload
    return JsonResponse(body, status=_STATUS.get(outcome.code, 200))


@login_required
@require_POST
def tracking_stop_view(request, pk):
    task = get_object_or_404(HelpRequest, pk=pk)
    if not tracking.stop(request.user, task, reason="volunteer"):
        return JsonResponse({"ok": False, "code": tracking.FORBIDDEN}, status=403)
    if _wants_json(request):
        return JsonResponse({"ok": True, "task_id": task.pk})
    return redirect("task_detail", pk=task.pk)


@login_required
@require_POST
def tracking_consent_view(request):
    """``action=give`` (default) or ``action=revoke`` — volunteers only."""
    user = request.user
    if not user.is_volunteer:
        return JsonResponse({"ok": False, "code": tracking.FORBIDDEN}, status=403)
    data = _body(request) or {}
    action = str(data.get("action") or "give").strip().lower()
    if action not in ("give", "revoke"):
        return JsonResponse({"ok": False, "code": tracking.INVALID}, status=400)
    if action == "give":
        tracking.give_consent(user)
    else:
        tracking.revoke_consent(user)
    consent = tracking.has_consent(user)

    next_url = data.get("next") or ""
    if not _wants_json(request) and next_url and next_url.startswith("/") and \
            url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()},
                                            require_https=request.is_secure()):
        return redirect(next_url)
    return JsonResponse({"ok": True, "consent": consent})
