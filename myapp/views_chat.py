"""Chat HTTP endpoints (Feature Pack v2, feature 8).

The WebSocket (``chat.send`` / ``chat.typing`` / ``chat.read``) is the primary
transport; these serve the page, history paging, image uploads, moderation and
the fallbacks for networks where WS is blocked. All rules are in
``myapp/services/chat.py``; permissions come from ``services/access.py`` and
are re-checked in every view.

    GET  /myapp/tasks/<pk>/chat/                   the chat page
    GET  /myapp/tasks/<pk>/chat/messages/          ?before=<id> | ?after=<id> (JSON)
    POST /myapp/tasks/<pk>/chat/send/              text, client_msg_id
    POST /myapp/tasks/<pk>/chat/upload/            image, text (caption), client_msg_id
    POST /myapp/tasks/<pk>/chat/read/              last_id
    POST /myapp/chat/messages/<id>/report/         reason
    POST /myapp/chat/messages/<id>/hide/           action=hide|unhide (staff)

A task the user can't open at all answers 404 (no existence oracle — the same
rule as the WS ``subscribe``); a task they can open but whose chat they can't
read (a volunteer looking at a pending task, an archived chat) answers 403.
"""

import json
from datetime import datetime

from django.contrib import messages as flash
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
import mimetypes

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models import ChatMessage, HelpRequest
from .services import access, chat, images

_STATUS = {
    chat.OK: 200,
    chat.FORBIDDEN: 403,
    chat.EMPTY: 400,
    chat.TOO_LONG: 400,
    chat.INVALID: 400,
    chat.RATE_LIMITED: 429,
    chat.OWN_MESSAGE: 400,
    chat.ALREADY: 200,
    images.ERR_TOO_LARGE: 413,
    images.ERR_INVALID: 400,
    images.ERR_FORMAT: 415,
}


def _wants_json(request):
    return (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("accept", "")
        or "application/json" in request.headers.get("content-type", "")
    )


def _data(request):
    if "application/json" in request.headers.get("content-type", ""):
        try:
            data = json.loads(request.body or b"{}")
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}
    return request.POST


def _int(raw):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _chat_task(request, pk, *, write=False):
    """The task, or Http404 / PermissionDenied per the module docstring."""
    task = get_object_or_404(HelpRequest.objects.select_related("client__profile", "volunteer__profile"), pk=pk)
    user = request.user
    allowed = access.can_write_chat(user, task) if write else access.can_read_chat(user, task)
    if not allowed:
        if not access.can_view_task(user, task):
            raise Http404
        raise PermissionDenied
    return task


def _visible_message(request, message_id):
    message = get_object_or_404(ChatMessage.objects.select_related("task", "sender__profile"), pk=message_id)
    if not access.can_read_chat(request.user, message.task):
        raise Http404  # someone else's chat: don't confirm the message exists
    return message


def _outcome_json(request, outcome):
    data = {"ok": outcome.ok, "code": outcome.code}
    if outcome.message is not None:
        data["message"] = chat.message_payload(outcome.message, request.user)
    return JsonResponse(data, status=_STATUS.get(outcome.code, 400))


@login_required
@require_GET
def task_chat_view(request, pk):
    task = _chat_task(request, pk)
    page = chat.history(request.user, task)
    rows = []
    for payload in page["messages"]:
        try:
            when = timezone.localtime(datetime.fromisoformat(payload["created_at"]))
        except (TypeError, ValueError):
            when = None
        rows.append({"p": payload, "time": when})
    return render(request, "myapp/chat.html", {
        "task": task,
        "page": page,
        "rows": rows,
        "peer_read": max((r["last_id"] for r in page["receipts"]), default=0),
        "quick_replies": _quick_replies_for(request.user, task),
        "can_write": page["can_write"],
        "is_staff": access.is_staff_user(request.user),
        "max_length": chat.CHAT_MESSAGE_MAX_LENGTH,
        "ended": task.status in ("completed", "cancelled"),
    })


@login_required
@require_GET
def task_chat_messages_view(request, pk):
    task = _chat_task(request, pk)
    page = chat.history(
        request.user, task,
        before_id=_int(request.GET.get("before")),
        after_id=_int(request.GET.get("after")) if request.GET.get("after") not in (None, "") else None,
        limit=_int(request.GET.get("limit")) or chat.PAGE_SIZE,
    )
    return JsonResponse({"ok": True, **page})


@login_required
@require_POST
def task_chat_send_view(request, pk):
    task = _chat_task(request, pk, write=True)
    data = _data(request)
    text = data.get("text", "")
    outcome = chat.send_message(request.user, task, text=text if isinstance(text, str) else None,
                                client_msg_id=data.get("client_msg_id", "") or "")
    if _wants_json(request):
        return _outcome_json(request, outcome)
    if not outcome.ok:
        flash.error(request, _FLASH.get(outcome.code, _FLASH[chat.INVALID]))
    return redirect("task_chat", pk=task.pk)


@login_required
@require_POST
def task_chat_upload_view(request, pk):
    task = _chat_task(request, pk, write=True)
    image = request.FILES.get("image")
    if image is None:
        outcome = chat.Outcome(False, chat.EMPTY)
    else:
        outcome = chat.send_message(request.user, task, text=request.POST.get("text", ""), image=image,
                                    client_msg_id=request.POST.get("client_msg_id", "") or "")
    if _wants_json(request):
        return _outcome_json(request, outcome)
    if not outcome.ok:
        flash.error(request, _FLASH.get(outcome.code, _FLASH[chat.INVALID]))
    return redirect("task_chat", pk=task.pk)


@login_required
@require_POST
def task_chat_read_view(request, pk):
    task = _chat_task(request, pk)
    last_id = _int(_data(request).get("last_id"))
    if last_id is None:
        return JsonResponse({"ok": False, "code": chat.INVALID}, status=400)
    cursor = chat.mark_read(request.user, task, last_id)
    return JsonResponse({"ok": True, "last_id": cursor})


@login_required
@require_POST
def chat_message_report_view(request, message_id):
    message = _visible_message(request, message_id)
    reason = _data(request).get("reason", "")
    outcome = chat.report(request.user, message, reason if isinstance(reason, str) else "")
    if _wants_json(request):
        return JsonResponse({"ok": outcome.ok, "code": outcome.code}, status=_STATUS.get(outcome.code, 400))
    if outcome.ok:
        flash.success(request, "Жалоба отправлена кураторам.")
    else:
        flash.error(request, _FLASH.get(outcome.code, _FLASH[chat.INVALID]))
    return redirect("task_chat", pk=message.task_id)


@login_required
@require_POST
def chat_message_hide_view(request, message_id):
    message = _visible_message(request, message_id)
    action = _data(request).get("action", "hide")
    if action not in ("hide", "unhide"):
        return JsonResponse({"ok": False, "code": chat.INVALID}, status=400)
    outcome = (chat.hide if action == "hide" else chat.unhide)(request.user, message)
    if _wants_json(request):
        return _outcome_json(request, outcome)
    if not outcome.ok:
        flash.error(request, _FLASH.get(outcome.code, _FLASH[chat.INVALID]))
    return redirect("task_chat", pk=message.task_id)


# Elderly-friendly one-tap replies: (i18n key, RU text sent without JS — the
# JS sends the text in the sender's UI language).
QUICK_REPLIES = (
    ("chat.quick.on_way", "Я в пути"),
    ("chat.quick.arrived", "Я на месте"),
    ("chat.quick.call_me", "Позвоните мне"),
)

# "On my way" / "I'm here" only make sense from the volunteer; the client gets
# "Call me". Staff type their own messages.
CLIENT_QUICK_REPLY_KEYS = ("chat.quick.call_me",)


def _quick_replies_for(user, task):
    if user.id == task.volunteer_id:
        return QUICK_REPLIES
    if user.id == task.client_id:
        return tuple(q for q in QUICK_REPLIES if q[0] in CLIENT_QUICK_REPLY_KEYS)
    return ()


# No-JS fallback messages (the JS path shows chat.error.* i18n keys).
_FLASH = {
    chat.FORBIDDEN: "В этот чат сейчас нельзя писать.",
    chat.EMPTY: "Сообщение пустое.",
    chat.TOO_LONG: f"Сообщение длиннее {chat.CHAT_MESSAGE_MAX_LENGTH} символов.",
    chat.INVALID: "Не удалось выполнить действие.",
    chat.RATE_LIMITED: "Слишком много сообщений — подождите минуту.",
    chat.OWN_MESSAGE: "Нельзя пожаловаться на своё сообщение.",
    images.ERR_TOO_LARGE: "Файл слишком большой (максимум 5 МБ).",
    images.ERR_INVALID: "Файл не похож на изображение.",
    images.ERR_FORMAT: "Поддерживаются JPEG, PNG и WebP.",
}


@login_required
@require_GET
def chat_message_image_view(request, message_id, variant):
    """A chat photo, only for users who may read that chat right now (hidden
    messages: staff only). Production hands the file to nginx with
    X-Accel-Redirect (`/protected-media/` is an `internal` alias of MEDIA_ROOT
    and `/media/chat/` is refused — DEPLOY.md); dev streams it."""
    if variant not in ("thumb", "full"):
        raise Http404
    message = get_object_or_404(ChatMessage.objects.select_related("task"), pk=message_id)
    user = request.user
    if not access.can_read_chat(user, message.task):
        raise Http404
    if message.hidden_at is not None and not access.is_staff_user(user):
        raise Http404
    path = chat.image_storage_path(message, variant)
    if path is None:
        raise Http404
    content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    if getattr(settings, "MEDIA_X_ACCEL", False):
        response = HttpResponse(content_type=content_type)
        response["X-Accel-Redirect"] = "/protected-media/" + path
    else:
        storage = message.image.storage
        if not storage.exists(path):
            raise Http404
        response = FileResponse(storage.open(path, "rb"), content_type=content_type)
    response["Cache-Control"] = "private, max-age=3600"
    response["X-Content-Type-Options"] = "nosniff"
    return response
