"""Shared per-object permission rules — one copy for HTTP views, the WebSocket
consumer (myapp/realtime/consumers.py), the AI tools and (later) the Telegram
bots. A rule changes here, never in a caller.

Pure functions over already-loaded objects: no queries, no side effects.
"""

from __future__ import annotations


def is_staff_user(user) -> bool:
    """Curator or admin (admin is ``is_superuser``, not a role flag)."""
    return bool(user is not None and getattr(user, "is_authenticated", False)
                and (user.is_superuser or user.is_curator))


def can_view_task(user, task) -> bool:
    """Who may open a task (detail page, ``task_<id>`` WS group, AI reads).

    Staff (admin/curator) always can. A client only ever sees their own
    requests. A volunteer sees a task they are already assigned to, or a
    still-pending task that would actually appear in their task_list (same
    region, or no region filter set) — never an arbitrary other client's
    pending request just because its status happens to be "pending", and
    never another volunteer's already-accepted task.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser or user.is_curator:
        return True
    if user.is_client:
        return task.client_id == user.id
    if user.is_volunteer:
        if task.volunteer_id == user.id:
            return True
        if task.status == "pending":
            if not user.region:
                return True
            return not task.region or task.region == user.region
        return False
    return False


def can_view_emergency(user, report) -> bool:
    """Staff, or the volunteer who raised the report."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return bool(user.is_superuser or user.is_curator or report.volunteer_id == user.id)
