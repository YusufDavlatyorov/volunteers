"""Client reviews — HTTP views (Feature Pack v2, feature 9).

All rules live in ``myapp/services/feedback.py``; these are thin transports.

    GET/POST /myapp/tasks/<pk>/review/   the client's review form (create / edit
                                         within REVIEW_EDIT_HOURS, then read-only);
                                         staff and the reviewed volunteer: read-only
    POST     /myapp/reviews/<id>/hide/   staff hide (action=hide) / unhide (action=unhide)
    POST     /myapp/reviews/<id>/publish/ staff mark "can be published on the site"
                                         (action=publish) / withdraw it (action=unpublish)
    GET      /myapp/reviews/mine/        the volunteer's own reviews (with comments)
    GET      /myapp/crm/reviews/         staff list (?low=1, ?hidden=1|0, ?published=1), paginated
"""

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .models import REVIEW_COMMENT_MAX_LENGTH, REVIEW_TAGS, HelpRequest, TaskReview
from .services import feedback
from .services.access import is_staff_user
from .views import role_required

logger = logging.getLogger(__name__)

CRM_REVIEWS_PAGE_SIZE = 25

_SUBMIT_MESSAGES = {
    "created": "Спасибо! Ваш отзыв сохранён.",
    "edited": "Отзыв обновлён.",
}


def _deny(request):
    messages.error(request, "У вас нет доступа к этой странице")
    return redirect("profile")


@login_required
def task_review_view(request, pk):
    task = get_object_or_404(
        HelpRequest.objects.select_related("client", "volunteer", "volunteer__profile"), pk=pk
    )
    user = request.user
    review = feedback.review_for_task(task)
    is_author_side = feedback.is_reviewable(user, task)

    if review is not None and not feedback.can_view_review(user, review):
        return _deny(request)
    if review is None and not is_author_side:
        if task.client_id == user.pk and user.is_client:
            # Their own task, just not reviewable yet (not completed / no volunteer).
            messages.info(request, "Оставить отзыв можно после завершения запроса.")
            return redirect("task_detail", pk=task.pk)
        return _deny(request)

    can_write = is_author_side and (review is None or feedback.can_edit(user, review))
    error_code = None
    form_values = {
        "stars": review.stars if review else None,
        "comment": review.comment if review else "",
        "tags": list(review.tags or []) if review else [],
    }

    if request.method == "POST":
        if not can_write:
            if review is not None and review.client_id == user.pk:
                messages.error(request, "Срок редактирования отзыва истёк.")
                return redirect("task_review", pk=task.pk)
            return _deny(request)
        stars = request.POST.get("stars")
        comment = request.POST.get("comment", "")
        tags = request.POST.getlist("tags")
        outcome = feedback.submit_review(user, task, stars=stars, comment=comment, tags=tags)
        if outcome.ok:
            if outcome.code != feedback.NOT_CHANGED:
                messages.success(request, _SUBMIT_MESSAGES["created" if outcome.created else "edited"])
            return redirect("task_review", pk=task.pk)
        if outcome.code == feedback.EDIT_CLOSED:
            messages.error(request, "Срок редактирования отзыва истёк.")
            return redirect("task_review", pk=task.pk)
        if outcome.code == feedback.FORBIDDEN:
            return _deny(request)
        error_code = outcome.code
        form_values = {"stars": feedback.clean_stars(stars), "comment": comment,
                       "tags": [t for t in tags if t in REVIEW_TAGS]}

    status = 400 if error_code else 200
    return render(request, "myapp/reviews/task_review.html", {
        "task": task,
        "review": review,
        "can_write": can_write,
        "is_edit": review is not None,
        "edit_deadline": feedback.edit_deadline(review) if review else None,
        "edit_hours": feedback.edit_hours(),
        "error_code": error_code,
        "form_values": form_values,
        "star_choices": range(1, 6),
        "review_tags": REVIEW_TAGS,
        "comment_max": REVIEW_COMMENT_MAX_LENGTH,
        "is_staff_viewer": is_staff_user(user),
    }, status=status)


@login_required
@role_required("admin", "curator")
@require_POST
def review_hide_view(request, pk):
    review = get_object_or_404(TaskReview, pk=pk)
    action = request.POST.get("action", "hide")
    if action == "unhide":
        outcome = feedback.unhide(request.user, review)
        if outcome.code == feedback.OK:
            messages.success(request, "Отзыв снова виден.")
    else:
        outcome = feedback.hide(request.user, review)
        if outcome.code == feedback.OK:
            messages.success(request, "Отзыв скрыт.")
    return _back(request)


def _back(request):
    next_url = request.POST.get("next", "")
    if next_url.startswith("/") and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(next_url)
    return redirect("crm_reviews")


@login_required
@role_required("admin", "curator")
@require_POST
def review_publish_view(request, pk):
    """Curator/admin: the review may (action=publish) / may no longer
    (action=unpublish) appear on the public About page."""
    review = get_object_or_404(TaskReview, pk=pk)
    if request.POST.get("action") == "unpublish":
        outcome = feedback.unpublish(request.user, review)
        if outcome.code == feedback.OK:
            messages.success(request, "Отзыв убран с сайта.")
    else:
        outcome = feedback.publish(request.user, review)
        if outcome.code == feedback.OK:
            messages.success(request, "Отзыв будет показан на странице «О нас».")
        elif outcome.code == feedback.FORBIDDEN:
            messages.error(request, "Опубликовать можно только видимый отзыв с текстом.")
    return _back(request)


@login_required
@role_required("volunteer")
def my_reviews_view(request):
    user = request.user
    page_obj = feedback.reviews_for(user, user, request.GET.get("page"))
    return render(request, "myapp/reviews/my_reviews.html", {
        "page_obj": page_obj,
        "summary_profile": user.profile,
        "querystring": "",
    })


@login_required
@role_required("admin", "curator")
def crm_reviews_view(request):
    low = request.GET.get("low") == "1"
    published = request.GET.get("published") == "1"
    hidden_param = request.GET.get("hidden")
    hidden = {"1": True, "0": False}.get(hidden_param)
    volunteer_param = request.GET.get("volunteer", "")
    volunteer_id = int(volunteer_param) if volunteer_param.isdigit() else None
    qs = feedback.staff_reviews(low=low, hidden=hidden, volunteer_id=volunteer_id, published=published)
    page_obj = Paginator(qs, CRM_REVIEWS_PAGE_SIZE).get_page(request.GET.get("page"))

    params = [f"volunteer={volunteer_id}"] if volunteer_id else []
    if low:
        params.append("low=1")
    if published:
        params.append("published=1")
    if hidden is not None:
        params.append(f"hidden={hidden_param}")
    return render(request, "myapp/reviews/crm_reviews.html", {
        "page_obj": page_obj,
        "low_filter": low,
        "published_filter": published,
        "hidden_filter": hidden_param if hidden is not None else "",
        "querystring": "&".join(params),
        "low_stars": feedback.LOW_STARS,
        "current_url": request.get_full_path(),
    })
