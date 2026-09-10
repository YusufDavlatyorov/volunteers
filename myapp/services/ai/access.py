"""Per-object visibility checks for AI tools.

These mirror the gates in ``myapp/views.py`` (``_can_view_task``,
``_can_view_emergency``, the client object gate). They live in their own module
so both ``read_tools`` and ``actions`` can use them without importing
``views`` (which would be a circular import — ``views`` imports this package).

Kept deliberately identical in behaviour to the view gates; if a view gate
changes, change it here too.
"""

from __future__ import annotations


def can_view_task(user, task) -> bool:
    """Mirror of ``myapp/views.py::_can_view_task``."""
    if user is None:
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
    """Mirror of ``myapp/views.py::_can_view_emergency``: staff, or the reporter."""
    if user is None:
        return False
    return bool(user.is_superuser or user.is_curator or report.volunteer_id == user.id)


def can_route_for_task(user, task) -> bool:
    """Mirror of ``myapp/views.py::_route_permission``."""
    if user is None:
        return False
    return bool(
        user.is_superuser
        or user.is_curator
        or task.client_id == user.id
        or task.volunteer_id == user.id
    )
