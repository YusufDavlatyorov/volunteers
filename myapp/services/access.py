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


def can_see_client_phone(user, task) -> bool:
    """Who may see the client's phone on a task (site + bot share this rule).

    Staff always; the client (it is their own number); the volunteer only once
    the task is theirs. A volunteer browsing a still-pending request sees the
    address but not the phone — it appears after accepting.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser or user.is_curator:
        return True
    if task.client_id == user.id:
        return True
    return bool(task.volunteer_id) and task.volunteer_id == user.id


def can_view_emergency(user, report) -> bool:
    """Staff, or the volunteer who raised the report."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return bool(user.is_superuser or user.is_curator or report.volunteer_id == user.id)


def can_see_location(user, task) -> bool:
    """Who may see a volunteer's live location / trail for ``task`` (feature 7):
    only while the task is ``active``, and only staff, the task's client and the
    assigned volunteer themself. A volunteer who can merely *view* a pending task
    never qualifies. Re-checked before every location delivery (WS + HTTP)."""
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    if task.status != "active" or not task.volunteer_id:
        return False
    if user.is_superuser or user.is_curator:
        return True
    return user.id in (task.client_id, task.volunteer_id)


def can_view_route(user, task) -> bool:
    """Route + navigation for a task: staff, the assigned volunteer, and the
    task's client while it is active (they see the volunteer's way to them)."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser or user.is_curator:
        return True
    if task.volunteer_id and user.id == task.volunteer_id:
        return task.status == "active"
    if user.id == task.client_id:
        return task.status == "active" and bool(task.volunteer_id)
    return False


def chat_ended_at(task):
    """When the task's chat became read-only (task completed/cancelled), or None."""
    if task.status == "completed":
        return task.completed_at or task.updated_at
    if task.status == "cancelled":
        return task.cancelled_at or task.updated_at
    return None


def chat_archived(task, now=None) -> bool:
    """Archived = ended more than CHAT_ARCHIVE_DAYS ago: hidden from the parties'
    lists, still readable by staff."""
    from datetime import timedelta

    from django.conf import settings
    from django.utils import timezone

    ended = chat_ended_at(task)
    if ended is None:
        return False
    days = getattr(settings, "CHAT_ARCHIVE_DAYS", 30)
    return (now or timezone.now()) - ended > timedelta(days=days)


def can_read_chat(user, task) -> bool:
    """Staff always (safety, incl. archived). The client and the assigned
    volunteer once a volunteer is assigned — while active and read-only after
    the end, until the chat is archived. Re-checked before every WS delivery."""
    if user is None or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    if user.is_superuser or user.is_curator:
        return True
    if not task.volunteer_id or task.status == "pending":
        return False
    if user.id not in (task.client_id, task.volunteer_id):
        return False
    return not chat_archived(task)


def can_write_chat(user, task) -> bool:
    """Writing needs an active task: its client, its assigned volunteer, or
    staff (shown as "Куратор")."""
    if not can_read_chat(user, task) or task.status != "active":
        return False
    return True
