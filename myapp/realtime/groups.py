"""Channel-layer group names — the one place they are spelled.

``user_<id>``   every socket of one user (notifications, own task updates)
``task_<id>``   parties of one task + staff, joined only via ``subscribe`` after
                ``services.access.can_view_task``
``staff``       curators + admins (CRM counters, availability changes)
``map_staff``   curators + admins viewing the operations map (markers)
"""

STAFF_GROUP = "staff"
MAP_STAFF_GROUP = "map_staff"


def user_group(user_id):
    return f"user_{int(user_id)}"


def task_group(task_id):
    return f"task_{int(task_id)}"
