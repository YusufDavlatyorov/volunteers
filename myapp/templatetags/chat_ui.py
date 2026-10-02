"""Template helpers for the task chat entry points (Feature Pack v2, feature 8).

Load with ``{% load chat_ui %}``.

* ``task|chat_readable:user`` — ``access.can_read_chat`` (pure, no query): show
  the "Open chat" button only to users who may read that chat right now.
* ``chat_unread|unread_for:task.id`` — the count from a ``{task_id: n}`` dict
  built by ONE ``chat.unread_counts(user, ids)`` call in the view/service, so a
  list of N tasks costs one query, not N.
* ``{% chat_button task unread %}`` — the button itself (partial
  ``myapp/_chat_button.html``); the badge is updated live by live-ui.js.
"""

from django import template

register = template.Library()


@register.filter
def chat_readable(task, user):
    from myapp.services import access

    try:
        return access.can_read_chat(user, task)
    except Exception:  # noqa: BLE001 — an entry point must never break a page
        return False


@register.filter
def unread_for(counts, task_id):
    if not counts:
        return 0
    try:
        return int(counts.get(task_id) or counts.get(int(task_id)) or 0)
    except (AttributeError, TypeError, ValueError):
        return 0


@register.inclusion_tag("myapp/_chat_button.html")
def chat_button(task, unread=0, variant="ghost"):
    return {"task": task, "unread": unread or 0, "variant": variant}
