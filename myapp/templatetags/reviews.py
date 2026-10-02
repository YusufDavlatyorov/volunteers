"""Template helpers for client reviews (Feature Pack v2, feature 9).

Load with ``{% load reviews %}``.

* ``{% review_summary volunteer %}`` — the public average + count
  (``feedback.public_summary``; accepts a ``Users`` or a ``Profile``; zero
  queries when the profile is loaded). Renders nothing-but-"no reviews yet"
  when there are none. NEVER comments — safe on any public card.
* ``{% review_stars n %}`` — n filled / 5 stars, with an accessible label.
* ``{% review_entry task as entry %}`` — for the task page's call to action:
  ``entry.review`` (the task's review if this user may see it, else None),
  ``entry.can_review`` (may leave a new one), ``entry.can_edit`` (author,
  inside the edit window), ``entry.url`` (the review page). One query.
"""

from django import template
from django.urls import reverse

register = template.Library()


@register.inclusion_tag("myapp/reviews/_summary.html")
def review_summary(volunteer, compact=False):
    from myapp.services.feedback import public_summary

    summary = public_summary(volunteer)
    filled = int(round(summary["avg"])) if summary["avg"] is not None else 0
    return {"summary": summary, "filled": filled, "compact": compact}


@register.inclusion_tag("myapp/reviews/_stars.html")
def review_stars(value):
    try:
        filled = max(0, min(5, int(round(float(value)))))
    except (TypeError, ValueError):
        filled = 0
    return {"filled": filled, "range": range(1, 6)}


@register.simple_tag(takes_context=True)
def review_entry(context, task):
    from myapp.services import feedback

    request = context.get("request")
    user = getattr(request, "user", None)
    entry = {"review": None, "can_review": False, "can_edit": False,
             "url": reverse("task_review", args=[task.pk])}
    if user is None or not user.is_authenticated or task.status != "completed":
        return entry
    review = feedback.review_for_task(task)
    if review is None:
        entry["can_review"] = feedback.is_reviewable(user, task)
        return entry
    if feedback.can_view_review(user, review):
        entry["review"] = review
        entry["can_edit"] = feedback.can_edit(user, review)
    return entry
