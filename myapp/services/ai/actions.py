"""Action tool handlers + the small shared services they (and the views) call.

Design rules from the project spec:

* the LLM never mutates a model directly — every action goes through a service
  function here that does its own validation and permission check;
* no business rule is duplicated — where a view already owns the logic
  (work-stage transition, the "recommend to one volunteer" message) it is
  extracted here and the view is updated to call the same function;
* every action is two-phase: ``validate`` (used to build the confirmation
  prompt, nothing changes) then ``execute`` (run only after the user confirms,
  via a server-issued single-use token — see ``confirmations.py``).
"""

from __future__ import annotations

import logging

from ...forms import HelpRequestForm
from ...models import HelpRequest
from ...notifications import notify_users, staff_recipients, volunteer_queryset_for_region
from accounts.models import Users

logger = logging.getLogger(__name__)

_HELP_TYPES = {t for t, _ in HelpRequest._meta.get_field("help_type").choices}
_PRIORITIES = {p for p, _ in HelpRequest._meta.get_field("priority").choices}
_ADVANCEABLE_STAGES = set(HelpRequest.WORK_STAGE_ORDER[1:])  # en_route, arrived, in_progress


def _ok(**data) -> dict:
    return {"success": True, **data}


def _err(error: str, **extra) -> dict:
    return {"success": False, "error": error, **extra}


# --------------------------------------------------------------------------- #
# shared services (also called by myapp/views.py)
# --------------------------------------------------------------------------- #

STAGE_CLIENT_NOTIFY = {
    HelpRequest.WORK_STAGE_EN_ROUTE: "Волонтёр выехал к вам",
    HelpRequest.WORK_STAGE_ARRIVED: "Волонтёр на месте",
}


def advance_work_stage_for(actor, task: HelpRequest, target: str) -> HelpRequest:
    """Advance ``task.work_stage`` (forward-only, active-only — raises ValueError
    otherwise) and notify the client on the stages that matter. Shared by
    ``task_advance_stage_view`` and the ``advance_work_stage`` AI action."""
    task.advance_work_stage(target)  # model method owns the transition rules
    if task.work_stage in STAGE_CLIENT_NOTIFY:
        notify_users(
            [task.client],
            STAGE_CLIENT_NOTIFY[task.work_stage],
            f"Запрос #{task.id}: {task.get_work_stage_display().lower()}.",
        )
    logger.info(
        "work stage of task #%s set to %s by user #%s",
        task.id, task.work_stage, getattr(actor, "pk", None),
    )
    return task


def notify_volunteer_recommendation(actor, task: HelpRequest, volunteer) -> int:
    """Send one volunteer a recommendation for a still-pending task (NOT an
    assignment — the task stays pending). Shared by ``task_notify_volunteer_view``
    and the ``notify_volunteer_about_task`` AI action. Returns delivered count."""
    subject = "Рекомендованный запрос помощи"
    message = (
        f"Куратор рекомендует вам запрос #{task.id} ({task.get_help_type_display()}) "
        f"в регионе {task.get_region_display() or '—'}.\n"
        f"Это рекомендация, а не назначение — запрос остаётся свободным, пока вы сами его не примете."
    )
    delivered = notify_users([volunteer], subject, message)
    logger.info(
        "task #%s recommended to volunteer #%s by user #%s",
        task.id, volunteer.pk, getattr(actor, "pk", None),
    )
    return delivered


def request_staff_review(actor, task: HelpRequest, note: str) -> int:
    """Escalate a request to the operational staff (active curators + admins)
    through the existing notification fan-out. No new workflow/model — the
    ``*_at`` / ``*_by`` fields already on the row plus this INFO log are the
    audit trail. Returns delivered count."""
    note = (note or "").strip()[:1000]
    body = (
        f"Запрос #{task.id} ({task.get_help_type_display()}) требует внимания координатора.\n"
        f"Статус: {task.get_status_display()}\n"
        f"Регион: {task.get_region_display() or '—'}\n"
        f"Клиент: {task.client.username}\n"
        f"Волонтёр: {task.volunteer.username if task.volunteer_id else '—'}\n"
        f"Эскалацию инициировал: {actor.username} ({actor.role_display})\n"
        f"Комментарий: {note or '—'}"
    )
    delivered = notify_users(list(staff_recipients()), "Запрос передан на рассмотрение", body)
    logger.info(
        "task #%s escalated to staff by user #%s (delivered=%s)",
        task.id, getattr(actor, "pk", None), delivered,
    )
    return delivered


# --------------------------------------------------------------------------- #
# action: create_help_request  (client)
# --------------------------------------------------------------------------- #

def _clean_request_fields(args: dict) -> dict:
    return {
        "help_type": args.get("help_type"),
        "priority": args.get("priority") or "",
        "description": (args.get("description") or "").strip(),
        "address": (args.get("address") or "").strip(),
        "phone": (args.get("phone") or "").strip(),
        "latitude": args.get("latitude"),
        "longitude": args.get("longitude"),
    }


def validate_create_help_request(ctx, **args) -> dict:
    if ctx.role != "client" or ctx.user is None:
        return _err("permission_denied")
    fields = _clean_request_fields(args)
    if fields["help_type"] not in _HELP_TYPES:
        return _err("invalid_args", note="Unknown help_type.", valid=sorted(_HELP_TYPES))
    form = HelpRequestForm(fields)
    if not form.is_valid():
        return _err("invalid_args", fields={k: v[0] for k, v in form.errors.items()})

    region = args.get("region") if args.get("region") in {"dushanbe", "sogd", "khatlon", "gbao", "rrp"} else ctx.region
    label = dict(HelpRequest._meta.get_field("help_type").choices)[fields["help_type"]]
    summary = (
        f"Create a help request: type «{label}», region «{region or 'not set'}», "
        f"description «{fields['description'][:120]}», address «{fields['address']}»."
    )
    return {"ok": True, "summary": summary, "clean_args": {**fields, "region": region or ""}}


def execute_create_help_request(ctx, **clean_args) -> dict:
    if ctx.role != "client" or ctx.user is None:
        return _err("permission_denied")
    region = clean_args.pop("region", "") or ctx.region
    form = HelpRequestForm(_clean_request_fields(clean_args))
    if not form.is_valid():
        return _err("invalid_args", fields={k: v[0] for k, v in form.errors.items()})
    obj = form.save(commit=False)
    obj.client = ctx.user
    obj.region = region
    obj.save()
    delivered = notify_users(
        volunteer_queryset_for_region(region),
        "Новый запрос помощи",
        f"Новый запрос в регионе {obj.get_region_display() or '—'}: {obj.description[:180]}",
    )
    logger.info("ai: help request #%s created for client #%s", obj.id, ctx.user_id)
    return _ok(request_id=obj.id, status=obj.status, volunteers_notified=delivered)


# --------------------------------------------------------------------------- #
# action: escalate_task_to_staff  (task client / assigned volunteer / staff)
# --------------------------------------------------------------------------- #

def _load_task(request_id):
    try:
        return HelpRequest.objects.select_related("client", "volunteer").get(pk=request_id)
    except (HelpRequest.DoesNotExist, ValueError, TypeError):
        return None


def _may_escalate(ctx, task) -> bool:
    return bool(
        ctx.is_staff
        or (ctx.user_id is not None and task.client_id == ctx.user_id)
        or (ctx.user_id is not None and task.volunteer_id == ctx.user_id)
    )


def validate_escalate_task_to_staff(ctx, **args) -> dict:
    task = _load_task(args.get("request_id"))
    if task is None:
        return _err("not_found")
    if not _may_escalate(ctx, task):
        return _err("permission_denied")
    note = (args.get("note") or "").strip()[:1000]
    summary = (
        f"Escalate request #{task.id} ({task.get_help_type_display()}, status "
        f"{task.get_status_display()}) to curators/admins."
        + (f" Note: «{note}»." if note else "")
    )
    return {"ok": True, "summary": summary, "clean_args": {"request_id": task.id, "note": note}}


def execute_escalate_task_to_staff(ctx, **clean_args) -> dict:
    task = _load_task(clean_args.get("request_id"))
    if task is None:
        return _err("not_found")
    if not _may_escalate(ctx, task):
        return _err("permission_denied")
    delivered = request_staff_review(ctx.user, task, clean_args.get("note", ""))
    return _ok(request_id=task.id, staff_notified=delivered)


# --------------------------------------------------------------------------- #
# action: notify_volunteer_about_task  (admin / curator)
# --------------------------------------------------------------------------- #

def _load_volunteer(volunteer_id):
    try:
        return Users.objects.get(pk=volunteer_id, is_volunteer=True, is_active=True)
    except (Users.DoesNotExist, ValueError, TypeError):
        return None


def validate_notify_volunteer_about_task(ctx, **args) -> dict:
    if not ctx.is_staff:
        return _err("permission_denied")
    task = _load_task(args.get("request_id"))
    if task is None:
        return _err("not_found")
    if task.status != "pending":
        return _err("task_not_pending")
    volunteer = _load_volunteer(args.get("volunteer_id"))
    if volunteer is None:
        return _err("volunteer_not_found")
    summary = (
        f"Send volunteer {volunteer.username} a recommendation for pending request "
        f"#{task.id} ({task.get_help_type_display()}, {task.get_region_display() or '—'}). "
        f"This is a recommendation, not an assignment — the request stays open."
    )
    return {"ok": True, "summary": summary,
            "clean_args": {"request_id": task.id, "volunteer_id": volunteer.id}}


def execute_notify_volunteer_about_task(ctx, **clean_args) -> dict:
    if not ctx.is_staff:
        return _err("permission_denied")
    task = _load_task(clean_args.get("request_id"))
    volunteer = _load_volunteer(clean_args.get("volunteer_id"))
    if task is None or volunteer is None:
        return _err("not_found")
    if task.status != "pending":
        return _err("task_not_pending")
    delivered = notify_volunteer_recommendation(ctx.user, task, volunteer)
    return _ok(request_id=task.id, volunteer_id=volunteer.id, notified=delivered)


# --------------------------------------------------------------------------- #
# action: advance_work_stage  (assigned volunteer / staff)
# --------------------------------------------------------------------------- #

def _may_advance(ctx, task) -> bool:
    return bool(ctx.is_staff or (ctx.user_id is not None and task.volunteer_id == ctx.user_id))


def validate_advance_work_stage(ctx, **args) -> dict:
    task = _load_task(args.get("request_id"))
    if task is None:
        return _err("not_found")
    if not _may_advance(ctx, task):
        return _err("permission_denied")
    if task.status != "active":
        return _err("task_not_active")
    stage = args.get("stage")
    if stage not in _ADVANCEABLE_STAGES:
        return _err("invalid_args", valid=sorted(_ADVANCEABLE_STAGES))
    order = HelpRequest.WORK_STAGE_ORDER
    if order.index(stage) <= order.index(task.work_stage):
        return _err("not_forward", current=task.work_stage)
    labels = dict(HelpRequest._meta.get_field("work_stage").choices)
    summary = (
        f"Advance request #{task.id} work stage from «{labels[task.work_stage]}» "
        f"to «{labels[stage]}»."
    )
    return {"ok": True, "summary": summary, "clean_args": {"request_id": task.id, "stage": stage}}


def execute_advance_work_stage(ctx, **clean_args) -> dict:
    task = _load_task(clean_args.get("request_id"))
    if task is None:
        return _err("not_found")
    if not _may_advance(ctx, task):
        return _err("permission_denied")
    try:
        advance_work_stage_for(ctx.user, task, clean_args.get("stage"))
    except ValueError as exc:
        return _err("invalid_transition", note=str(exc))
    return _ok(request_id=task.id, work_stage=task.work_stage)
