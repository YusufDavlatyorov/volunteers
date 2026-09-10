"""Tool registry: schema + handler + role gate + read/action classification.

* ``schemas_for(names)`` renders the OpenAI ``tools`` array for a Groq call.
* ``allowed_tool_names(ctx)`` filters the registry by the caller's trusted role.
* ``dispatch(name, raw_args, ctx)`` runs a READ tool, or the VALIDATE phase of an
  ACTION tool (dispatch never executes an action — that needs a confirmed token).
* ``execute_action(name, clean_args, ctx)`` runs the EXECUTE phase, called only
  by ``assistant.confirm_action`` after ``confirmations.take`` succeeds.

Role gating and per-object checks are both enforced; result envelopes are always
``{"success": bool, ...}`` and never carry a traceback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import actions, read_tools
from .context import UserContext

logger = logging.getLogger(__name__)

CLIENT = "client"
VOLUNTEER = "volunteer"
CURATOR = "curator"
ADMIN = "admin"
ALL_AUTH = frozenset({CLIENT, VOLUNTEER, CURATOR, ADMIN})
STAFF = frozenset({CURATOR, ADMIN})
ADMIN_ONLY = frozenset({ADMIN})


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict
    roles: frozenset
    kind: str  # "read" | "action"
    handler: Optional[Callable] = None
    validate: Optional[Callable] = None
    execute: Optional[Callable] = None
    int_params: frozenset = field(default_factory=frozenset)


# --------------------------------------------------------------------------- #
# schema helpers
# --------------------------------------------------------------------------- #

def _obj(properties: dict | None = None, required: list | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


_STR = {"type": "string"}
_INT = {"type": "integer"}
_NUM = {"type": "number"}
_PAGE = {"limit": _INT, "offset": _INT}

_STATUS_ENUM = {"type": "string", "enum": ["pending", "active", "completed", "cancelled"]}
_PRIORITY_ENUM = {"type": "string", "enum": ["normal", "high", "emergency"]}
_REGION_ENUM = {"type": "string", "enum": ["dushanbe", "sogd", "khatlon", "gbao", "rrp"]}
_HELP_TYPE_ENUM = {
    "type": "string",
    "enum": ["medical", "grocery", "transport", "household", "emotional", "documents", "other"],
}
_STAGE_ENUM = {"type": "string", "enum": ["en_route", "arrived", "in_progress"]}


def _read(name, desc, roles, handler, properties=None, required=None, int_params=()):
    return ToolSpec(
        name=name, description=desc, parameters=_obj(properties, required),
        roles=frozenset(roles), kind="read", handler=handler,
        int_params=frozenset(int_params),
    )


def _action(name, desc, roles, validate, execute, properties=None, required=None, int_params=()):
    return ToolSpec(
        name=name, description=desc, parameters=_obj(properties, required),
        roles=frozenset(roles), kind="action", validate=validate, execute=execute,
        int_params=frozenset(int_params),
    )


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #

_SPECS = [
    # ---- common ----------------------------------------------------------- #
    _read("get_platform_help",
          "Curated explanation of how Generation Connect works. topic ∈ "
          "how_it_works, roles, create_request, volunteering, regions, contact.",
          ALL_AUTH, read_tools.get_platform_help, {"topic": _STR}),
    _read("get_my_profile", "The current user's own profile summary.",
          ALL_AUTH, read_tools.get_my_profile),

    # ---- client --------------------------------------------------------- #
    _read("get_my_requests",
          "The current client's own help requests (optionally filtered by status).",
          {CLIENT}, read_tools.get_my_requests, {"status": _STATUS_ENUM, **_PAGE},
          int_params=("limit", "offset")),
    _read("get_request_status",
          "Status and details of one help request the current user owns.",
          {CLIENT}, read_tools.get_request_status, {"request_id": _INT}, ["request_id"],
          int_params=("request_id",)),
    _read("get_my_donations", "The current user's own in-kind donation offers.",
          {CLIENT}, read_tools.get_my_donations, _PAGE, int_params=("limit", "offset")),
    _read("get_my_pet_reports", "The current user's own lost & found pet reports.",
          {CLIENT}, read_tools.get_my_pet_reports, _PAGE, int_params=("limit", "offset")),
    _read("get_my_events", "Upcoming events relevant to the current user's region.",
          {CLIENT, VOLUNTEER}, read_tools.get_my_events, _PAGE, int_params=("limit", "offset")),
    _action("create_help_request",
            "Create a new help request for the current client. Requires confirmation.",
            {CLIENT}, actions.validate_create_help_request, actions.execute_create_help_request,
            {"help_type": _HELP_TYPE_ENUM, "description": _STR, "address": _STR,
             "phone": _STR, "priority": _PRIORITY_ENUM, "region": _REGION_ENUM,
             "latitude": _NUM, "longitude": _NUM},
            ["help_type", "description", "address", "phone"]),

    # ---- volunteer ----------------------------------------------------- #
    _read("get_my_active_task", "The current volunteer's active task, if any.",
          {VOLUNTEER}, read_tools.get_my_active_task),
    _read("get_my_tasks", "The current volunteer's tasks (optionally filtered by status).",
          {VOLUNTEER}, read_tools.get_my_tasks, {"status": _STATUS_ENUM, **_PAGE},
          int_params=("limit", "offset")),
    _read("get_recommended_tasks",
          "Nearby pending requests recommended for the current volunteer (matching engine).",
          {VOLUNTEER}, read_tools.get_recommended_tasks),
    _read("get_task_details",
          "Details of one task the current volunteer is allowed to see (assigned, or "
          "pending in their region).",
          {VOLUNTEER}, read_tools.get_task_details, {"request_id": _INT}, ["request_id"],
          int_params=("request_id",)),
    _read("get_route_for_task",
          "Practical route/distance from the volunteer's saved location to a task location.",
          {VOLUNTEER}, read_tools.get_route_for_task, {"request_id": _INT}, ["request_id"],
          int_params=("request_id",)),
    _action("advance_work_stage",
            "Advance the work stage of an active task (en_route → arrived → in_progress). "
            "Assigned volunteer or staff. Requires confirmation.",
            {VOLUNTEER, CURATOR, ADMIN},
            actions.validate_advance_work_stage, actions.execute_advance_work_stage,
            {"request_id": _INT, "stage": _STAGE_ENUM}, ["request_id", "stage"],
            int_params=("request_id",)),

    # ---- escalation (task party or staff) ----------------------------- #
    _action("escalate_task_to_staff",
            "Escalate a request to curators/admins for review (notifies staff). The task's "
            "client, its assigned volunteer, or any staff member. Requires confirmation.",
            ALL_AUTH,
            actions.validate_escalate_task_to_staff, actions.execute_escalate_task_to_staff,
            {"request_id": _INT, "note": _STR}, ["request_id"],
            int_params=("request_id",)),

    # ---- curator / admin — operational reads -------------------------- #
    _read("get_dashboard_stats", "Platform-wide operational counters (the CRM dashboard tiles).",
          STAFF, read_tools.get_dashboard_stats),
    _read("region_activity", "Pending/active request counts per region.",
          STAFF, read_tools.region_activity),
    _read("list_help_requests",
          "Every help request, filterable by status/region/priority/help_type. Count-first, paginated.",
          STAFF, read_tools.list_help_requests,
          {"status": _STATUS_ENUM, "region": _REGION_ENUM, "priority": _PRIORITY_ENUM,
           "help_type": _HELP_TYPE_ENUM, **_PAGE}, int_params=("limit", "offset")),
    _read("get_help_request", "Full details of one help request (staff view).",
          STAFF, read_tools.get_help_request, {"request_id": _INT}, ["request_id"],
          int_params=("request_id",)),
    _read("list_overdue_tasks", "Active tasks past the 3h overdue threshold, right now.",
          STAFF, read_tools.list_overdue_tasks, _PAGE, int_params=("limit", "offset")),
    _read("list_stale_requests", "Pending requests waiting past 48h without a volunteer.",
          STAFF, read_tools.list_stale_requests, _PAGE, int_params=("limit", "offset")),
    _read("list_unassigned_requests", "Pending requests with no volunteer yet.",
          STAFF, read_tools.list_unassigned_requests, {"region": _REGION_ENUM, **_PAGE},
          int_params=("limit", "offset")),
    _read("find_requests_by_client", "Search help requests by client username (substring).",
          STAFF, read_tools.find_requests_by_client, {"query": _STR, **_PAGE}, ["query"],
          int_params=("limit", "offset")),
    _read("list_emergencies", "SOS / emergency reports, filterable by status.",
          STAFF, read_tools.list_emergencies,
          {"status": {"type": "string",
                      "enum": ["open", "acknowledged", "resolved", "cancelled"]}, **_PAGE},
          int_params=("limit", "offset")),
    _read("get_emergency", "One emergency report.",
          STAFF, read_tools.get_emergency, {"report_id": _INT}, ["report_id"],
          int_params=("report_id",)),
    _read("list_volunteers", "Volunteers with region/availability/rating/active-task-count "
          "(no contact details).",
          STAFF, read_tools.list_volunteers,
          {"region": _REGION_ENUM,
           "availability": {"type": "string", "enum": ["available", "busy", "offline"]}, **_PAGE},
          int_params=("limit", "offset")),
    _read("list_available_volunteers", "Volunteers currently marked available.",
          STAFF, read_tools.list_available_volunteers, {"region": _REGION_ENUM, **_PAGE},
          int_params=("limit", "offset")),
    _read("get_volunteer_recommendations",
          "Ranked, explainable shortlist of volunteers for a pending request (matching engine).",
          STAFF, read_tools.get_volunteer_recommendations, {"request_id": _INT}, ["request_id"],
          int_params=("request_id",)),
    _read("list_pending_applications", "Volunteer applications awaiting review.",
          STAFF, read_tools.list_pending_applications, _PAGE, int_params=("limit", "offset")),
    _read("list_events", "Events (newest first).",
          STAFF, read_tools.list_events, _PAGE, int_params=("limit", "offset")),
    _read("list_broadcasts", "Broadcast messages sent to volunteers (newest first).",
          STAFF, read_tools.list_broadcasts, _PAGE, int_params=("limit", "offset")),
    _read("list_donations", "In-kind donation offers, filterable by status.",
          STAFF, read_tools.list_donations,
          {"status": {"type": "string",
                      "enum": ["pending", "approved", "ready", "received", "distributed", "cancelled"]},
           **_PAGE}, int_params=("limit", "offset")),
    _read("list_pet_reports", "Lost & found pet reports, filterable by status.",
          STAFF, read_tools.list_pet_reports,
          {"status": {"type": "string", "enum": ["open", "matched", "resolved", "closed"]}, **_PAGE},
          int_params=("limit", "offset")),
    _action("notify_volunteer_about_task",
            "Send one volunteer a recommendation for a pending request (NOT an assignment — "
            "the request stays open). Staff only. Requires confirmation.",
            STAFF,
            actions.validate_notify_volunteer_about_task, actions.execute_notify_volunteer_about_task,
            {"request_id": _INT, "volunteer_id": _INT}, ["request_id", "volunteer_id"],
            int_params=("request_id", "volunteer_id")),

    # ---- admin only -------------------------------------------------- #
    _read("list_users", "User directory, count-first and paginated. Never returns "
          "passwords, tokens, phone or email.",
          ADMIN_ONLY, read_tools.list_users,
          {"role": {"type": "string", "enum": ["admin", "curator", "volunteer", "client"]},
           "region": _REGION_ENUM, "active": {"type": "boolean"}, **_PAGE},
          int_params=("limit", "offset")),
    _read("get_user", "One user's safe profile summary (no contact details / secrets).",
          ADMIN_ONLY, read_tools.get_user, {"user_id": _INT}, ["user_id"],
          int_params=("user_id",)),
    _read("users_by_role", "Active user counts per role.",
          ADMIN_ONLY, read_tools.users_by_role),
    _read("platform_totals", "All-time request totals.",
          ADMIN_ONLY, read_tools.platform_totals),
]

TOOLS: dict[str, ToolSpec] = {spec.name: spec for spec in _SPECS}


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #

def allowed_tool_names(ctx: UserContext) -> frozenset:
    if not ctx.authenticated:
        return frozenset()
    return frozenset(name for name, spec in TOOLS.items() if ctx.role in spec.roles)


def schemas_for(names) -> list[dict]:
    out = []
    for name in names:
        spec = TOOLS.get(name)
        if spec is None:
            continue
        out.append({
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            },
        })
    return out


def _coerce(spec: ToolSpec, raw_args: dict) -> dict:
    declared = set(spec.parameters.get("properties", {}))
    args = {}
    for key, value in (raw_args or {}).items():
        if key not in declared:
            continue
        if key in spec.int_params and isinstance(value, str):
            try:
                value = int(value.strip())
            except ValueError:
                pass
        if isinstance(value, str):
            value = value.strip()
        args[key] = value
    return args


def dispatch(name: str, raw_args: dict, ctx: UserContext) -> dict:
    """Run a read tool, or validate an action tool. Never executes an action."""
    spec = TOOLS.get(name)
    if spec is None:
        return {"success": False, "error": "unknown_tool"}
    if not ctx.authenticated or ctx.role not in spec.roles:
        logger.info("ai: tool '%s' denied for role '%s'", name, ctx.role)
        return {"success": False, "error": "permission_denied"}

    args = _coerce(spec, raw_args)
    try:
        if spec.kind == "read":
            result = spec.handler(ctx, **args)
            logger.info("ai: tool '%s' ok=%s (role=%s)",
                        name, result.get("success"), ctx.role)
            return result

        # action → validate phase only
        outcome = spec.validate(ctx, **args)
        if not outcome.get("ok"):
            logger.info("ai: action '%s' rejected at validate: %s",
                        name, outcome.get("error"))
            return {"success": False, "error": outcome.get("error", "invalid_args"),
                    **{k: v for k, v in outcome.items() if k not in ("ok", "error")}}
        return {
            "success": True,
            "confirmation": {
                "tool": name,
                "summary": outcome["summary"],
                "clean_args": outcome["clean_args"],
            },
        }
    except TypeError as exc:
        logger.warning("ai: tool '%s' bad arguments: %s", name, exc)
        return {"success": False, "error": "invalid_args"}
    except Exception:  # noqa: BLE001 - never leak a traceback to the model/user
        logger.exception("ai: tool '%s' raised", name)
        return {"success": False, "error": "tool_error"}


def execute_action(name: str, clean_args: dict, ctx: UserContext) -> dict:
    """Run the EXECUTE phase of an action. Called only after a confirmed token."""
    spec = TOOLS.get(name)
    if spec is None or spec.kind != "action":
        return {"success": False, "error": "unknown_tool"}
    if not ctx.authenticated or ctx.role not in spec.roles:
        logger.info("ai: action '%s' denied for role '%s' at execute", name, ctx.role)
        return {"success": False, "error": "permission_denied"}
    try:
        result = spec.execute(ctx, **(clean_args or {}))
        logger.info("ai: action '%s' executed success=%s (user #%s)",
                    name, result.get("success"), ctx.user_id)
        return result
    except Exception:  # noqa: BLE001
        logger.exception("ai: action '%s' raised at execute", name)
        return {"success": False, "error": "action_error"}
